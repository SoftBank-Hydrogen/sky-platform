"""Owned dedicated app Secret and SQL login; no application routing changes."""

import json
import re
import secrets
from dataclasses import asdict

from adapters.aws.dedicated_database import (
    AwsDedicatedDatabasePreparer,
    DedicatedDatabaseSettings,
)
from adapters.database.dedicated_access import PostgresDedicatedAccess, access_identity


class AwsDedicatedApplicationAccess:
    def __init__(self, settings, *, session=None, connect=None):
        if not isinstance(settings, DedicatedDatabaseSettings):
            raise TypeError("Registered dedicated settings required")
        if session is None:
            import boto3

            session = boto3.Session(region_name=settings.source.pool.region)
        from botocore.config import Config

        self.settings = settings
        self.preparer = AwsDedicatedDatabasePreparer(settings, session=session, connect=connect)
        self.client = session.client(
            "secretsmanager",
            region_name=settings.source.pool.region,
            config=Config(retries={"total_max_attempts": 1}),
        )

    def provision(self, plan, *, operation_id, preparation_receipt, guard):
        guard()
        target = self.preparer._preflight(plan, operation_id)
        instance = self.preparer._describe(target.instance_id)
        if instance is None:
            raise ValueError("Prepared dedicated instance is missing; do not recreate")
        tags = {
            "sky-managed": "true",
            "sky-database-role": "dedicated_workload",
            "sky-operation-id": operation_id,
            "sky-plan-id": plan["plan_id"],
            "sky-organization-id": target.organization_id,
            "sky-application-id": target.application_id,
            "sky-owner-ref": target.owner_ref,
            "sky-configuration-digest": self.settings.digest,
        }
        self.preparer._verify(instance, target, tags, available=True)
        if (
            preparation_receipt.get("operation_id") != operation_id
            or preparation_receipt.get("plan_id") != plan["plan_id"]
            or preparation_receipt.get("binding") != asdict(target)
            or preparation_receipt.get("resource_id") != instance["DbiResourceId"]
            or preparation_receipt.get("verified_scope") != "rds_ownership_network_tls"
        ):
            raise ValueError("Preparation evidence differs from owned target")
        self.preparer._verify_tls(instance, target)
        role, marker = access_identity(target)
        region, account = target.region, target.account_id
        name = "sky-dedicated/" + instance["DbiResourceId"] + "/" + role
        prefix = f"arn:aws:secretsmanager:{region}:{account}:secret:{name}-"
        secret_tags = {**tags, "sky-rds-resource-id": instance["DbiResourceId"], "sky-access-marker": marker}
        identity = {
            "username": role,
            "dbname": target.database_name,
            "host": instance["Endpoint"]["Address"],
            "port": 5432,
            "sslmode": "verify-full",
            "binding": asdict(target),
            "resource_id": instance["DbiResourceId"],
        }
        try:
            metadata = self.client.describe_secret(SecretId=name)
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") != "ResourceNotFoundException":
                raise
            guard()
            try:
                self.client.create_secret(
                    Name=name,
                    ClientRequestToken=operation_id,
                    KmsKeyId=self.settings.source.app_secret_kms_arn,
                    SecretString=json.dumps({**identity, "password": secrets.token_urlsafe(36)}),
                    Tags=[{"Key": k, "Value": v} for k, v in secret_tags.items()],
                )
            except Exception as error:
                if getattr(error, "response", {}).get("Error", {}).get("Code") != "ResourceExistsException":
                    raise  # Unknown outcome must reconcile, never rotate or blindly retry.
            metadata = self.client.describe_secret(SecretId=name)
        arn = metadata.get("ARN", "")
        actual_tags = metadata.get("Tags", [])
        if (
            not re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9]{6}", arn)
            or metadata.get("Name") != name
            or metadata.get("KmsKeyId") != self.settings.source.app_secret_kms_arn
            or metadata.get("DeletedDate")
            or metadata.get("OwningService")
            or metadata.get("RotationEnabled")
            or metadata.get("ReplicationStatus")
            or len({t["Key"] for t in actual_tags}) != len(actual_tags)
            or any({t["Key"]: t["Value"] for t in actual_tags}.get(k) != v for k, v in secret_tags.items())
            or self.client.get_resource_policy(SecretId=arn).get("ResourcePolicy")
        ):
            raise ValueError("Existing app Secret is unowned or differs from intent")
        value = self.preparer.audit._secret_value(arn)
        if set(value) != {*identity, "password"} or any(value.get(k) != v for k, v in identity.items()):
            raise ValueError("App Secret identity differs from target")
        master = self.preparer.audit._secret_value(instance["MasterUserSecret"]["SecretArn"])
        host = instance["Endpoint"]["Address"]

        def admin():
            return self.preparer.audit._connection(
                host, 5432, target.database_name, "skyadmin", master["password"]
            )

        def login(user, password):
            return self.preparer.audit._connection(host, 5432, target.database_name, user, password)

        result = PostgresDedicatedAccess(admin, login).provision(
            target, password=value["password"], secret_ref=arn, guard=guard
        )
        return {
            **result,
            "operation_id": operation_id,
            "plan_id": plan["plan_id"],
            "resource_id": instance["DbiResourceId"],
            "tls_verified": True,
        }
