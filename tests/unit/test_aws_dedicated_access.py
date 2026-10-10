import json
from unittest.mock import Mock

import pytest

from adapters.aws.dedicated_access import AwsDedicatedApplicationAccess
from adapters.database.dedicated_access import access_identity
from domain.database import DatabaseBinding
from tests.unit.test_aws_dedicated_database import prepared as prepared  # noqa: PLC0414


def fixture_access(prepared, monkeypatch):
    preparer, plan, operation, _, instance, _ = prepared
    target = DatabaseBinding(**plan["target"])
    preparer._preflight = Mock(return_value=target)
    preparer._verify_tls = Mock()
    role, marker = access_identity(target)
    name = "sky-dedicated/" + instance["DbiResourceId"] + "/" + role
    arn = f"arn:aws:secretsmanager:{target.region}:{target.account_id}:secret:{name}-test01"
    tags = {
        "sky-managed": "true",
        "sky-database-role": "dedicated_workload",
        "sky-operation-id": operation,
        "sky-plan-id": plan["plan_id"],
        "sky-organization-id": target.organization_id,
        "sky-application-id": target.application_id,
        "sky-owner-ref": target.owner_ref,
        "sky-configuration-digest": preparer.settings.digest,
        "sky-rds-resource-id": instance["DbiResourceId"],
        "sky-access-marker": marker,
    }
    metadata = {
        "ARN": arn,
        "Name": name,
        "KmsKeyId": preparer.settings.source.app_secret_kms_arn,
        "Tags": [{"Key": k, "Value": v} for k, v in tags.items()],
    }
    value = {
        "username": role,
        "password": "test-app-password",
        "dbname": target.database_name,
        "host": instance["Endpoint"]["Address"],
        "port": 5432,
        "sslmode": "verify-full",
        "binding": plan["target"],
        "resource_id": instance["DbiResourceId"],
    }
    preparer.audit._secret_value = Mock(
        side_effect=lambda ref: value if ref == arn else {"password": "test-master"}
    )
    adapter = object.__new__(AwsDedicatedApplicationAccess)
    adapter.settings, adapter.preparer, adapter.client = preparer.settings, preparer, Mock()
    adapter.client.describe_secret.return_value = metadata
    adapter.client.get_resource_policy.return_value = {}
    sql = Mock()
    sql.provision.return_value = {"secret_ref": arn, "routing_changed": False}
    monkeypatch.setattr("adapters.aws.dedicated_access.PostgresDedicatedAccess", Mock(return_value=sql))
    receipt = {
        "operation_id": operation,
        "plan_id": plan["plan_id"],
        "binding": plan["target"],
        "resource_id": instance["DbiResourceId"],
        "verified_scope": "rds_ownership_network_tls",
    }
    return adapter, plan, operation, receipt, metadata, value, sql


def test_owned_secret_reused_without_password_rotation(prepared, monkeypatch):
    adapter, plan, operation, receipt, _, _, sql = fixture_access(prepared, monkeypatch)
    result = adapter.provision(plan, operation_id=operation, preparation_receipt=receipt, guard=Mock())
    adapter.client.create_secret.assert_not_called()
    assert sql.provision.call_args.kwargs["password"] == "test-app-password"
    assert "test-app-password" not in json.dumps(result)
    assert result["tls_verified"] is True


@pytest.mark.parametrize(
    "field,value", [("KmsKeyId", "foreign"), ("DeletedDate", "removed"), ("Name", "foreign")]
)
def test_foreign_secret_never_reaches_sql(prepared, monkeypatch, field, value):
    adapter, plan, operation, receipt, metadata, _, sql = fixture_access(prepared, monkeypatch)
    metadata[field] = value
    with pytest.raises(ValueError):
        adapter.provision(plan, operation_id=operation, preparation_receipt=receipt, guard=Mock())
    sql.provision.assert_not_called()


def test_changed_resource_receipt_never_reads_secret(prepared, monkeypatch):
    adapter, plan, operation, receipt, _, _, sql = fixture_access(prepared, monkeypatch)
    receipt["resource_id"] = "db-FOREIGN"
    with pytest.raises(ValueError):
        adapter.provision(plan, operation_id=operation, preparation_receipt=receipt, guard=Mock())
    adapter.client.describe_secret.assert_not_called()
    sql.provision.assert_not_called()


def test_unknown_secret_create_result_does_not_repeat_or_reach_sql(prepared, monkeypatch):
    from tests.unit.test_aws_shared_database import ServiceError

    adapter, plan, operation, receipt, _, _, sql = fixture_access(prepared, monkeypatch)
    adapter.client.describe_secret.side_effect = ServiceError("ResourceNotFoundException")
    adapter.client.create_secret.side_effect = RuntimeError("uncertain transport result")
    with pytest.raises(RuntimeError):
        adapter.provision(plan, operation_id=operation, preparation_receipt=receipt, guard=Mock())
    adapter.client.create_secret.assert_called_once()
    assert adapter.client.create_secret.call_args.kwargs["ClientRequestToken"] == operation
    sql.provision.assert_not_called()


def test_wrong_secret_identity_never_reaches_sql(prepared, monkeypatch):
    adapter, plan, operation, receipt, _, value, sql = fixture_access(prepared, monkeypatch)
    value["username"] = "skyadmin"
    with pytest.raises(ValueError):
        adapter.provision(plan, operation_id=operation, preparation_receipt=receipt, guard=Mock())
    sql.provision.assert_not_called()
