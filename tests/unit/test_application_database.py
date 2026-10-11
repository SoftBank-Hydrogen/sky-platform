"""Trusted allocation metadata and secret-free application environment proposals."""

import copy
from unittest.mock import Mock

import pytest

from adapters.aws.application_database import AwsApplicationDatabaseEnvironment
from ports.application_database import reference_from_allocation
from ports.artifacts import SourceArtifact

ID = "a" * 24
ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
ARN = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:sky-pool/pool/{ID}-AbCd12"


@pytest.fixture
def documents():
    original = SourceArtifact("team", "tug", "a" * 32, "original", "b" * 64, 100, "c" * 64)
    prepared = SourceArtifact("team", "tug", "a" * 32, "prepared", "d" * 64, 120, "e" * 64)
    result = {
        "status": "database_binding_required",
        "original_ref": original.record(),
        "prepared_ref": prepared.record(),
        "transformation": {"profile": "tug-postgres-v1", "source_digest": prepared.source_digest},
    }
    receipt = {
        "status": "ready",
        "allocation_id": ID,
        "verified_scope": "postgresql_role_and_database_acl",
        "binding": {
            "organization_id": "team",
            "application_id": "tug",
            "role": "shared_workload",
            "account_id": ACCOUNT,
            "region": REGION,
            "instance_id": "workload-pool",
            "database_name": "sky_app_" + ID,
            "owner_ref": "allocation-" + ID,
        },
        "login_role": "sky_login_" + ID,
        "secret_ref": ARN,
        "connection_limit": 5,
    }
    return result, receipt


def reference(documents):
    result, receipt = documents
    return reference_from_allocation(result, receipt, account_id=ACCOUNT, region=REGION, allocation_id=ID)


def test_receipt_is_source_and_application_bound_without_credentials(documents):
    ref = reference(documents)
    assert ref.source_digest == documents[0]["prepared_ref"]["source_digest"]
    assert ref.secret_ref == ARN and "password" not in ref.record()


@pytest.mark.parametrize(
    "field,value",
    [
        ("role", "sky_state"),
        ("role", "dedicated_workload"),
        ("organization_id", "other"),
        ("application_id", "other"),
        ("account_id", "999999999999"),
        ("region", "us-east-1"),
        ("owner_ref", "other"),
        ("database_name", "sky_state"),
    ],
)
def test_foreign_or_control_database_receipts_rejected(documents, field, value):
    result, receipt = copy.deepcopy(documents)
    receipt["binding"][field] = value
    with pytest.raises(ValueError):
        reference_from_allocation(result, receipt, account_id=ACCOUNT, region=REGION, allocation_id=ID)


@pytest.mark.parametrize(
    "field,value",
    [
        ("connection_limit", True),
        ("connection_limit", 51),
        ("login_role", "admin"),
        ("secret_ref", "plaintext-password"),
        ("status", "pending"),
        ("verified_scope", "unverified"),
        ("allocation_id", "b" * 24),
    ],
)
def test_invalid_allocation_settings_rejected(documents, field, value):
    result, receipt = copy.deepcopy(documents)
    receipt[field] = value
    with pytest.raises(ValueError):
        reference_from_allocation(result, receipt, account_id=ACCOUNT, region=REGION, allocation_id=ID)


def clients():
    values = {name: Mock() for name in ("sts", "rds", "secretsmanager")}
    values["sts"].get_caller_identity.return_value = {"Account": ACCOUNT}
    values["rds"].describe_db_instances.return_value = {
        "DBInstances": [
            {
                "DBInstanceIdentifier": "workload-pool",
                "DBInstanceArn": f"arn:aws:rds:{REGION}:{ACCOUNT}:db:workload-pool",
                "DbiResourceId": "db-physical-id",
                "DBInstanceStatus": "available",
                "Engine": "postgres",
                "PubliclyAccessible": False,
                "StorageEncrypted": True,
                "Endpoint": {"Address": f"workload.example.{REGION}.rds.amazonaws.com", "Port": 5432},
            }
        ]
    }
    values["rds"].list_tags_for_resource.return_value = {
        "TagList": [
            {"Key": k, "Value": v}
            for k, v in {
                "sky-managed": "true",
                "sky-database-role": "shared_workload",
                "sky-pool-id": "pool",
            }.items()
        ]
    }
    values["secretsmanager"].describe_secret.return_value = {
        "ARN": ARN,
        "Tags": [
            {"Key": k, "Value": v}
            for k, v in {
                "sky-managed": "true",
                "sky-database-role": "shared_workload",
                "sky-pool-id": "pool",
                "sky-allocation-id": ID,
                "sky-organization-id": "team",
                "sky-application-id": "tug",
                "sky-rds-resource-id": "db-physical-id",
            }.items()
        ],
    }
    return values


def test_plan_uses_metadata_only_and_ecs_secret_reference(documents):
    mocked = clients()
    plan = AwsApplicationDatabaseEnvironment(reference(documents), clients=mocked).plan()
    assert plan["environment"]["PGDATABASE"] == "sky_app_" + ID
    assert "PGPASSWORD" not in plan["environment"]
    assert plan["secrets"] == {"PGPASSWORD": ARN + ":password::"}
    assert plan["requires_new_review"] and plan["network_and_migration_verification_required"]
    mocked["secretsmanager"].get_secret_value.assert_not_called()


@pytest.mark.parametrize(
    "change", ["caller", "instance", "public", "secret_owner", "physical_id", "endpoint"]
)
def test_environment_rejects_changed_live_resource_identity(documents, change):
    mocked = clients()
    db = mocked["rds"].describe_db_instances.return_value["DBInstances"][0]
    secret = mocked["secretsmanager"].describe_secret.return_value
    if change == "caller":
        mocked["sts"].get_caller_identity.return_value["Account"] = "999999999999"
    elif change == "instance":
        db["DBInstanceArn"] = "wrong"
    elif change == "public":
        db["PubliclyAccessible"] = True
    elif change == "secret_owner":
        secret["Tags"] = []
    elif change == "physical_id":
        db["DbiResourceId"] = "replaced-db"
    else:
        db["Endpoint"]["Address"] = "localhost"
    with pytest.raises(ValueError):
        AwsApplicationDatabaseEnvironment(reference(documents), clients=mocked).plan()


def test_metadata_failure_is_redacted(documents):
    mocked = clients()
    mocked["rds"].describe_db_instances.side_effect = OSError("private diagnostics")
    with pytest.raises(OSError, match="metadata unavailable") as error:
        AwsApplicationDatabaseEnvironment(reference(documents), clients=mocked).plan()
    assert "private" not in str(error.value)
