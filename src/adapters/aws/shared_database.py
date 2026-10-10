"""Worker-only AWS binding for an explicitly registered PostgreSQL workload pool.

Does not provision RDS, edit networking, rotate app credentials, or migrate data.
A failed allocation retains its secret and database for reconciliation.
"""

import json
import re
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path

from adapters.database.shared_pool import PostgresSharedPool
from domain.shared_database import PoolAllocationRequest, SharedDatabasePool
from ports.shared_database import AllocationCredentials, PoolAllocationError


def _tags(items):
    result = {}
    for item in items:
        key, value = item["Key"], item["Value"]
        if key in result or not isinstance(key, str) or not isinstance(value, str):
            raise PoolAllocationError("Ambiguous pool ownership tags")
        result[key] = value
    return result


def _error_code(error):
    return getattr(error, "response", {}).get("Error", {}).get("Code")


@dataclass(frozen=True)
class AwsSharedPoolSettings:
    pool: SharedDatabasePool
    resource_id: str
    vpc_id: str
    database_security_group: str
    allowed_client_groups: tuple[str, ...]
    admin_secret_arn: str
    app_secret_kms_arn: str
    sslrootcert: str

    def __post_init__(self):
        if not isinstance(self.pool, SharedDatabasePool):
            raise ValueError("A registered workload pool is required")
        if not re.fullmatch(r"db-[A-Za-z0-9]{1,64}", self.resource_id):
            raise ValueError("Immutable RDS resource ID is required")
        if not re.fullmatch(r"vpc-[a-f0-9]{8,17}", self.vpc_id):
            raise ValueError("Invalid pool VPC")
        groups = (self.database_security_group, *self.allowed_client_groups)
        if (
            not self.allowed_client_groups
            or not isinstance(self.allowed_client_groups, tuple)
            or len(set(groups)) != len(groups)
            or any(not re.fullmatch(r"sg-[a-f0-9]{8,17}", group) for group in groups)
        ):
            raise ValueError("Pinned, separate database and client security groups are required")
        prefix = f"arn:aws:secretsmanager:{self.pool.region}:{self.pool.account_id}:secret:"
        if not re.fullmatch(
            re.escape(prefix) + r"[A-Za-z0-9/_+=.!@-]+-[A-Za-z0-9]{6}", self.admin_secret_arn
        ):
            raise ValueError("Pool admin secret must be in the registered account and region")
        prefix = f"arn:aws:kms:{self.pool.region}:{self.pool.account_id}:key/"
        if not re.fullmatch(re.escape(prefix) + r"[a-f0-9-]{36}", self.app_secret_kms_arn):
            raise ValueError("A pinned same-account app secret KMS key ARN is required")
        if not Path(self.sslrootcert).is_absolute() or not Path(self.sslrootcert).is_file():
            raise ValueError("Pool TLS CA bundle is missing")


class AwsSharedDatabaseAllocator:
    def __init__(self, settings: AwsSharedPoolSettings, *, session=None, connect=None):
        if session is None:
            import boto3

            session = boto3.Session(region_name=settings.pool.region)
        if connect is None:
            import psycopg

            connect = psycopg.connect
        self.settings = settings
        self.connect = connect
        self.clients = {
            name: session.client(name, region_name=settings.pool.region)
            for name in ("sts", "rds", "ec2", "secretsmanager")
        }

    def _audit(self):
        settings, pool = self.settings, self.settings.pool
        if any(client.meta.region_name != pool.region for client in self.clients.values()):
            raise PoolAllocationError("Pool client region differs from registration")
        if self.clients["sts"].get_caller_identity()["Account"] != pool.account_id:
            raise PoolAllocationError("Pool caller account differs from registration")
        instances = self.clients["rds"].describe_db_instances(DBInstanceIdentifier=pool.instance_id)[
            "DBInstances"
        ]
        if len(instances) != 1:
            raise PoolAllocationError("Exactly one registered RDS instance is required")
        instance = instances[0]
        arn = f"arn:aws:rds:{pool.region}:{pool.account_id}:db:{pool.instance_id}"
        if (
            instance.get("DBInstanceArn") != arn
            or instance.get("DBInstanceIdentifier") != pool.instance_id
            or instance.get("DbiResourceId") != settings.resource_id
            or instance.get("Engine") != "postgres"
            or instance.get("DBInstanceStatus") != "available"
            or instance.get("DBName") != pool.control_database
            or instance.get("PubliclyAccessible") is not False
            or instance.get("StorageEncrypted") is not True
            or instance.get("DBSubnetGroup", {}).get("VpcId") != settings.vpc_id
            or instance.get("VpcSecurityGroups")
            != [{"VpcSecurityGroupId": settings.database_security_group, "Status": "active"}]
            or instance.get("MasterUserSecret", {}).get("SecretArn") != settings.admin_secret_arn
            or instance.get("MasterUserSecret", {}).get("SecretStatus") != "active"
        ):
            raise PoolAllocationError(
                "RDS pool identity, availability or isolation differs from registration"
            )
        tags = _tags(self.clients["rds"].list_tags_for_resource(ResourceName=arn)["TagList"])
        expected = {"sky-managed": "true", "sky-database-role": "shared_workload", "sky-pool-id": pool.id}
        if any(tags.get(key) != value for key, value in expected.items()):
            raise PoolAllocationError("RDS is not owned by this workload pool")
        endpoint = instance["Endpoint"]
        host, port = endpoint["Address"], endpoint["Port"]
        if (
            not isinstance(host, str)
            or not re.fullmatch(rf"[a-z0-9.-]+\.{re.escape(pool.region)}\.rds\.amazonaws\.com", host)
            or type(port) is not int
            or not 1 <= port <= 65535
        ):
            raise PoolAllocationError("Invalid registered RDS endpoint")
        self._audit_network(port)
        return host, port, instance["MasterUsername"]

    def _audit_network(self, port):
        settings, pool = self.settings, self.settings.pool
        ids = [settings.database_security_group, *settings.allowed_client_groups]
        response = self.clients["ec2"].describe_security_groups(GroupIds=ids)
        groups = {group["GroupId"]: group for group in response["SecurityGroups"]}
        if (
            len(response["SecurityGroups"]) != len(ids)
            or set(groups) != set(ids)
            or response.get("NextToken")
        ):
            raise PoolAllocationError("Incomplete pool security group inventory")
        if any(
            group.get("OwnerId") != pool.account_id or group.get("VpcId") != settings.vpc_id
            for group in groups.values()
        ):
            raise PoolAllocationError("Pool network ownership differs from registration")
        allowed = set(settings.allowed_client_groups)
        found = set()
        for rule in groups[settings.database_security_group].get("IpPermissions", []):
            if (
                rule.get("IpProtocol") != "tcp"
                or rule.get("FromPort") != port
                or rule.get("ToPort") != port
                or rule.get("IpRanges")
                or rule.get("Ipv6Ranges")
                or rule.get("PrefixListIds")
                or not rule.get("UserIdGroupPairs")
            ):
                raise PoolAllocationError("Pool ingress must be restricted to pinned client groups")
            for source in rule["UserIdGroupPairs"]:
                if source.get("GroupId") not in allowed or source.get("UserId") != pool.account_id:
                    raise PoolAllocationError("Unregistered client can reach the workload pool")
                found.add(source["GroupId"])
        if found != allowed:
            raise PoolAllocationError("Registered pool clients cannot reach the database")

    def _secret_value(self, arn):
        response = self.clients["secretsmanager"].get_secret_value(SecretId=arn, VersionStage="AWSCURRENT")
        if response.get("ARN") != arn or "AWSCURRENT" not in response.get("VersionStages", []):
            raise PoolAllocationError("Secret response identity or version differs from request")
        value = json.loads(response["SecretString"])
        if not isinstance(value, dict):
            raise PoolAllocationError("Invalid database credentials")
        return value

    def _admin_credentials(self, username):
        arn = self.settings.admin_secret_arn
        metadata = self.clients["secretsmanager"].describe_secret(SecretId=arn)
        if (
            metadata.get("ARN") != arn
            or metadata.get("DeletedDate")
            or metadata.get("OwningService") != "rds"
        ):
            raise PoolAllocationError("Pool admin secret is not managed by RDS")
        value = self._secret_value(arn)
        if (
            value.get("username") != username
            or not isinstance(username, str)
            or not re.fullmatch(r"[A-Za-z0-9_]+", username)
        ):
            raise PoolAllocationError("Pool admin credentials do not match RDS")
        password = value.get("password")
        if not isinstance(password, str) or not password or "\0" in password:
            raise PoolAllocationError("Invalid pool admin credentials")
        return username, password

    def _app_credentials(self, request):
        settings, pool = self.settings, self.settings.pool
        client = self.clients["secretsmanager"]
        name = f"sky-pool/{pool.id}/{request.id}"
        tags = {
            "sky-managed": "true",
            "sky-database-role": "shared_workload",
            "sky-pool-id": pool.id,
            "sky-allocation-id": request.id,
            "sky-organization-id": request.organization_id,
            "sky-application-id": request.application_id,
            "sky-rds-resource-id": settings.resource_id,
        }
        try:
            metadata = client.describe_secret(SecretId=name)
        except Exception as error:
            if _error_code(error) != "ResourceNotFoundException":
                raise
            value = {
                "username": request.login_role,
                "password": secrets.token_urlsafe(36),
                "dbname": request.database_name,
                "pool_id": pool.id,
                "allocation_id": request.id,
                "resource_id": settings.resource_id,
            }
            try:
                client.create_secret(
                    Name=name,
                    ClientRequestToken=str(uuid.uuid4()),
                    KmsKeyId=settings.app_secret_kms_arn,
                    SecretString=json.dumps(value),
                    Tags=[{"Key": key, "Value": value} for key, value in tags.items()],
                )
            except Exception as error:
                if _error_code(error) != "ResourceExistsException":
                    raise
            # Read the winner after a concurrent create. Never overwrite or rotate it.
            metadata = client.describe_secret(SecretId=name)
        arn = metadata.get("ARN", "")
        prefix = f"arn:aws:secretsmanager:{pool.region}:{pool.account_id}:secret:{name}-"
        owned = _tags(metadata.get("Tags", []))
        if (
            not re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9]{6}", arn)
            or metadata.get("Name") != name
            or metadata.get("KmsKeyId") != settings.app_secret_kms_arn
            or metadata.get("DeletedDate")
            or metadata.get("RotationEnabled", False)
            or metadata.get("OwningService")
            or metadata.get("ReplicationStatus")
            or any(owned.get(key) != value for key, value in tags.items())
        ):
            raise PoolAllocationError("App secret ownership or settings differ from the allocation")
        if client.get_resource_policy(SecretId=arn).get("ResourcePolicy"):
            raise PoolAllocationError("Shared app secrets require a review of their resource policy")
        value = self._secret_value(arn)
        if any(
            value.get(key) != expected
            for key, expected in {
                "username": request.login_role,
                "dbname": request.database_name,
                "pool_id": pool.id,
                "allocation_id": request.id,
                "resource_id": settings.resource_id,
            }.items()
        ):
            raise PoolAllocationError("App credential identity differs from its allocation")
        return AllocationCredentials(arn, value["password"])

    def _connection(self, host, port, database, username, password):
        connection = self.connect(
            host=host,
            port=port,
            dbname=database,
            user=username,
            password=password,
            sslmode="verify-full",
            sslrootcert=self.settings.sslrootcert,
            connect_timeout=10,
            application_name="sky-workload-pool",
            options="-c statement_timeout=30000 -c lock_timeout=10000",
        )
        try:
            with connection.transaction():
                if connection.execute(
                    "SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()"
                ).fetchone() != (True,):
                    raise PoolAllocationError("Pool connection is not using TLS")
            return connection
        except Exception:
            connection.close()
            raise

    def _allocator(self, endpoint, credentials):
        host, port, _ = endpoint
        username, password = credentials
        return PostgresSharedPool(
            self.settings.pool,
            lambda database: self._connection(host, port, database, username, password),
            lambda database, user, secret: self._connection(host, port, database, user, secret),
            rds_managed=True,
        )

    def initialize(self):
        try:
            endpoint = self._audit()
            self._allocator(endpoint, self._admin_credentials(endpoint[2])).initialize()
        except PoolAllocationError:
            raise
        except Exception:
            raise PoolAllocationError(
                "Unable to register AWS workload pool; outcome requires review"
            ) from None

    def allocate(self, request: PoolAllocationRequest) -> dict:
        if request.pool != self.settings.pool:
            raise PoolAllocationError("Allocation pool differs from the registered AWS adapter")
        try:
            endpoint = self._audit()
            admin = self._admin_credentials(endpoint[2])
            allocator = self._allocator(endpoint, admin)
            # Require prior registration before creating an app secret.
            allocator.check_registration()
            credentials = self._app_credentials(request)
            receipt = allocator.allocate(request, credentials)
            return {
                **receipt,
                "aws_checks": [
                    "caller_account",
                    "rds_resource_identity",
                    "pool_tags",
                    "private_group_ingress",
                    "secret_ownership",
                    "tls_verify_full",
                ],
            }
        except PoolAllocationError:
            raise
        except Exception:
            raise PoolAllocationError(
                "AWS pool allocation failed; retained resources require review"
            ) from None
