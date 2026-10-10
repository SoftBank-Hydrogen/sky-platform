"""Prepare a pinned private RDS target; never copy data or change app routing."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, replace
from uuid import UUID

from adapters.aws.shared_database import AwsSharedDatabaseAllocator, AwsSharedPoolSettings
from domain.database import DatabaseBinding


@dataclass(frozen=True)
class DedicatedDatabaseSettings:
    source: AwsSharedPoolSettings
    database_security_group: str
    subnet_group: str
    parameter_group: str
    instance_class: str
    engine_version: str
    storage_gb: int
    multi_az: bool

    def __post_init__(self):
        if not isinstance(self.source, AwsSharedPoolSettings):
            raise TypeError("Registered source pool required")
        replace(self.source, database_security_group=self.database_security_group)
        if (any(not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{2,62}", value)
                for value in (self.subnet_group, self.parameter_group))
                or not isinstance(self.instance_class, str)
                or self.instance_class not in {"db.t4g.small", "db.t4g.medium", "db.t4g.large",
                                                "db.m6g.large", "db.m7g.large"}
                or not isinstance(self.engine_version, str)
                or not re.fullmatch(r"17\.[0-9]+", self.engine_version)
                or type(self.storage_gb) is not int or not 20 <= self.storage_gb <= 100
                or type(self.multi_az) is not bool):
            raise ValueError("Dedicated capacity and network configuration must be explicit and bounded")

    @property
    def digest(self):
        document = asdict(self)
        # CA bytes, rather than a machine-specific file name, bind the reviewed trust.
        from pathlib import Path

        document["source"]["sslrootcert"] = hashlib.sha256(Path(self.source.sslrootcert).read_bytes()).hexdigest()
        return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class AwsDedicatedDatabasePreparer:
    def __init__(self, settings, *, session=None, connect=None, sleep=time.sleep, monotonic=time.monotonic):
        if not isinstance(settings, DedicatedDatabaseSettings):
            raise TypeError("Dedicated settings required")
        if session is None:
            import boto3

            session = boto3.Session(region_name=settings.source.pool.region)
        from botocore.config import Config

        self.settings, self.sleep, self.monotonic = settings, sleep, monotonic
        self.audit = AwsSharedDatabaseAllocator(settings.source, session=session, connect=connect)
        self.network = AwsSharedDatabaseAllocator(replace(settings.source,
            database_security_group=settings.database_security_group), session=session, connect=connect)
        # Creation has no client token: don't let SDK retries repeat an uncertain mutation.
        self.rds = session.client("rds", region_name=settings.source.pool.region,
                                  config=Config(retries={"total_max_attempts": 1}))

    def _preflight(self, plan, operation_id):
        if str(UUID(operation_id)) != operation_id:
            raise ValueError("Canonical operation identity required")
        source, target = DatabaseBinding(**plan["source"]), DatabaseBinding(**plan["target"])
        pool = self.settings.source.pool
        if (self.rds.meta.region_name != pool.region
                or plan.get("target_configuration_digest") != self.settings.digest
                or source.role != "shared_workload" or target.role != "dedicated_workload"
                or source.instance_id != pool.instance_id
                or (source.account_id, source.region) != (pool.account_id, pool.region)
                or (target.account_id, target.region) != (pool.account_id, pool.region)
                or (target.organization_id, target.application_id) != (source.organization_id, source.application_id)
                or target.instance_id == source.instance_id
                or not re.fullmatch(r"sky-[a-z][a-z0-9-]{2,55}", target.instance_id)
                or "--" in target.instance_id or target.instance_id.endswith("-")
                or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", target.database_name)
                or not re.fullmatch(r"DBP-[a-f0-9]{16}", plan.get("plan_id", ""))):
            raise ValueError("Dedicated intent differs from the approved source/configuration")
        self.audit._audit()
        self.network._audit_network(5432)
        group = self.rds.describe_db_subnet_groups(DBSubnetGroupName=self.settings.subnet_group)["DBSubnetGroups"]
        if (len(group) != 1 or group[0].get("VpcId") != self.settings.source.vpc_id
                or group[0].get("DBSubnetGroupName") != self.settings.subnet_group
                or group[0].get("SubnetGroupStatus") != "Complete"
                or len({s.get("SubnetAvailabilityZone", {}).get("Name") for s in group[0].get("Subnets", [])
                        if s.get("SubnetStatus") == "Active"}) < 2):
            raise ValueError("Dedicated subnet group differs from registered VPC")
        parameters = self.rds.describe_db_parameters(DBParameterGroupName=self.settings.parameter_group, Source="user")
        if parameters.get("Marker") or not any(p.get("ParameterName") == "rds.force_ssl"
                and p.get("ParameterValue") == "1" for p in parameters.get("Parameters", [])):
            raise ValueError("Registered dedicated parameter group must force TLS")
        if source.database_name == pool.control_database or target.database_name == pool.control_database:
            raise ValueError("Workload pool control database cannot be promoted")
        return target

    def _describe(self, identifier):
        try:
            values = self.rds.describe_db_instances(DBInstanceIdentifier=identifier)["DBInstances"]
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") == "DBInstanceNotFound":
                return None
            raise
        if len(values) != 1:
            raise ValueError("Expected one dedicated instance")
        return values[0]

    def _verify(self, instance, target, tags, *, available=False):
        settings, pool = self.settings, self.settings.source.pool
        arn = f"arn:aws:rds:{pool.region}:{pool.account_id}:db:{target.instance_id}"
        actual_tags = self.rds.list_tags_for_resource(ResourceName=arn)["TagList"]
        if len({tag["Key"] for tag in actual_tags}) != len(actual_tags):
            raise ValueError("Duplicate ownership tags")
        actual_tags = {tag["Key"]: tag["Value"] for tag in actual_tags}
        if (instance.get("DBInstanceArn") != arn or instance.get("DBInstanceIdentifier") != target.instance_id
                or instance.get("Engine") != "postgres" or instance.get("EngineVersion") != settings.engine_version
                or instance.get("DBName") != target.database_name or instance.get("MasterUsername") != "skyadmin"
                or instance.get("DBInstanceClass") != settings.instance_class
                or instance.get("AllocatedStorage") != settings.storage_gb
                or instance.get("StorageType") != "gp3" or instance.get("StorageEncrypted") is not True
                or instance.get("PubliclyAccessible") is not False or instance.get("MultiAZ") is not settings.multi_az
                or instance.get("DeletionProtection") is not True or instance.get("BackupRetentionPeriod") != 7
                or instance.get("DBSubnetGroup", {}).get("DBSubnetGroupName") != settings.subnet_group
                or instance.get("DBSubnetGroup", {}).get("VpcId") != settings.source.vpc_id
                or [s.get("VpcSecurityGroupId") for s in instance.get("VpcSecurityGroups", [])] != [settings.database_security_group]
                or [p.get("DBParameterGroupName") for p in instance.get("DBParameterGroups", [])] != [settings.parameter_group]
                or any(actual_tags.get(key) != value for key, value in tags.items())
                or (available and (instance.get("DBInstanceStatus") != "available"
                    or instance.get("PendingModifiedValues")
                    or instance["DBParameterGroups"][0].get("ParameterApplyStatus") != "in-sync"
                    or instance["VpcSecurityGroups"][0].get("Status") != "active"))):
            raise ValueError("Existing target is unowned or differs from approved configuration")

    def prepare(self, plan, *, operation_id, guard):
        guard()
        target = self._preflight(plan, operation_id)
        tags = {"sky-managed": "true", "sky-database-role": "dedicated_workload",
                "sky-operation-id": operation_id, "sky-plan-id": plan["plan_id"],
                "sky-organization-id": target.organization_id, "sky-application-id": target.application_id,
                "sky-owner-ref": target.owner_ref, "sky-configuration-digest": self.settings.digest}
        instance = self._describe(target.instance_id)
        if instance is None:
            guard()
            settings = self.settings
            self.rds.create_db_instance(DBInstanceIdentifier=target.instance_id, DBName=target.database_name,
                Engine="postgres", EngineVersion=settings.engine_version, MasterUsername="skyadmin",
                ManageMasterUserPassword=True, DBInstanceClass=settings.instance_class,
                AllocatedStorage=settings.storage_gb, StorageType="gp3", StorageEncrypted=True,
                PubliclyAccessible=False, MultiAZ=settings.multi_az, DeletionProtection=True,
                BackupRetentionPeriod=7, CopyTagsToSnapshot=True, AutoMinorVersionUpgrade=False,
                DBSubnetGroupName=settings.subnet_group, DBParameterGroupName=settings.parameter_group,
                VpcSecurityGroupIds=[settings.database_security_group],
                Tags=[{"Key": key, "Value": value} for key, value in tags.items()])
        deadline = self.monotonic() + 1200
        while self.monotonic() < deadline:
            guard()
            instance = self._describe(target.instance_id)
            if instance is not None:
                self._verify(instance, target, tags)
                if instance.get("DBInstanceStatus") == "available":
                    self._verify(instance, target, tags, available=True)
                    break
                if instance.get("DBInstanceStatus") not in {"creating", "backing-up", "configuring-enhanced-monitoring"}:
                    raise ValueError("Dedicated instance reached an unexpected state")
            self.sleep(10)
        else:
            raise TimeoutError("Dedicated preparation needs reconciliation")
        guard()
        self._verify_tls(instance, target)
        guard()
        return {"operation_id": operation_id, "plan_id": plan["plan_id"], "binding": asdict(target),
                "status": "available", "verified_scope": "rds_ownership_network_tls",
                "encrypted": True, "publicly_accessible": False, "resource_id": instance["DbiResourceId"]}

    def _verify_tls(self, instance, target):
        pool = self.settings.source.pool
        endpoint, secret = instance.get("Endpoint", {}), instance.get("MasterUserSecret", {})
        host, arn = endpoint.get("Address"), secret.get("SecretArn")
        if (not isinstance(host, str) or not re.fullmatch(rf"[a-z0-9.-]+\.{re.escape(pool.region)}\.rds\.amazonaws\.com", host)
                or endpoint.get("Port") != 5432 or secret.get("SecretStatus") != "active"
                or not isinstance(arn, str) or not arn.startswith(f"arn:aws:secretsmanager:{pool.region}:{pool.account_id}:secret:")):
            raise ValueError("Dedicated endpoint or managed secret is not ready")
        credentials = self.audit._secret_value(arn)
        if credentials.get("username") != "skyadmin" or not isinstance(credentials.get("password"), str) or not credentials["password"]:
            raise ValueError("Managed target credentials differ from expected master identity")
        with self.audit._connection(host, 5432, target.database_name, "skyadmin", credentials["password"]) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT current_database(),current_user,(SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid())")
            if cursor.fetchone() != (target.database_name, "skyadmin", True):
                raise ValueError("Dedicated target database or TLS verification failed")
