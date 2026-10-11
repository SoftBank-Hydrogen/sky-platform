"""Read-only RDS/Secret metadata to propose environment bindings; no credentials or apply."""
import re

from ports.application_database import ApplicationDatabaseReference


class AwsApplicationDatabaseEnvironment:
    def __init__(self, reference, *, clients=None):
        if not isinstance(reference, ApplicationDatabaseReference):
            raise ValueError("Validated application database reference required")
        self.reference = reference
        if clients is None:
            import boto3
            from botocore.config import Config
            clients = {name: boto3.client(name, region_name=reference.region,
                config=Config(connect_timeout=5, read_timeout=10, retries={"total_max_attempts": 1}))
                for name in ("sts", "rds", "secretsmanager")}
        self.clients = clients

    def plan(self):
        r = self.reference
        try:
            if self.clients["sts"].get_caller_identity().get("Account") != r.account_id:
                raise ValueError("Application database account differs")
            instances = self.clients["rds"].describe_db_instances(DBInstanceIdentifier=r.instance_id).get("DBInstances", [])
            if len(instances) != 1:
                raise ValueError("Allocated RDS instance is unavailable")
            db = instances[0]
            arn = f"arn:aws:rds:{r.region}:{r.account_id}:db:{r.instance_id}"
            tags = {t["Key"]: t["Value"] for t in self.clients["rds"].list_tags_for_resource(ResourceName=arn).get("TagList", [])}
            resource_id = db.get("DbiResourceId")
            if (db.get("DBInstanceArn") != arn or db.get("DBInstanceIdentifier") != r.instance_id
                    or db.get("DBInstanceStatus") != "available" or db.get("Engine") != "postgres"
                    or db.get("PubliclyAccessible") is not False or db.get("StorageEncrypted") is not True
                    or not isinstance(resource_id, str) or not resource_id
                    or tags.get("sky-managed") != "true" or tags.get("sky-database-role") != "shared_workload"
                    or not tags.get("sky-pool-id")):
                raise ValueError("RDS metadata does not match workload ownership")
            secret = self.clients["secretsmanager"].describe_secret(SecretId=r.secret_ref)
            st = {t["Key"]: t["Value"] for t in secret.get("Tags", [])}
            expected = {"sky-managed": "true", "sky-database-role": "shared_workload",
                        "sky-pool-id": tags["sky-pool-id"], "sky-allocation-id": r.allocation_id,
                        "sky-organization-id": r.organization_id, "sky-application-id": r.application_id,
                        "sky-rds-resource-id": resource_id}
            if secret.get("ARN") != r.secret_ref or secret.get("DeletedDate") or any(st.get(k) != v for k,v in expected.items()):
                raise ValueError("Secret metadata does not match allocation ownership")
            endpoint = db.get("Endpoint", {})
            host, port = endpoint.get("Address"), endpoint.get("Port")
            if (not isinstance(host, str) or not re.fullmatch(r"[a-z0-9.-]+", host)
                    or not host.endswith("." + r.region + ".rds.amazonaws.com")
                    or type(port) is not int or not 1 <= port <= 65535):
                raise ValueError("Invalid allocated RDS endpoint")
            return {"source_digest": r.source_digest, "reference": r.record(),
                    "environment": {"PGHOST": host, "PGPORT": str(port), "PGDATABASE": r.database_name,
                        "PGUSER": r.login_role, "PGSSLROOTCERT": "/app/rds-global-bundle.pem",
                        "SKY_APP_DB_CONNECTION_LIMIT": str(r.connection_limit)},
                    "secrets": {"PGPASSWORD": r.secret_ref + ":password::"},
                    "requires_new_review": True, "network_and_migration_verification_required": True}
        except ValueError:
            raise
        except Exception:
            raise OSError("Application database metadata unavailable") from None
