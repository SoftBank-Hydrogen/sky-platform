"""SDK fixtures: target preparation cannot adopt a foreign resource or retry creation."""

from dataclasses import replace
from unittest.mock import MagicMock, Mock
from uuid import uuid4

import pytest

from adapters.aws.dedicated_database import AwsDedicatedDatabasePreparer, DedicatedDatabaseSettings
from adapters.aws.shared_database import AwsSharedPoolSettings
from domain.database import DatabaseBinding
from domain.shared_database import SharedDatabasePool
from engine.database_promotion import PromotionTrigger, plan_database_promotion
from tests.unit.test_aws_shared_database import ServiceError


@pytest.fixture
def prepared(tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("fixture-ca")
    pool = SharedDatabasePool("pool", "111111111111", "ap-northeast-2", "sky-pool", "sky_pool_test", 30)
    source = AwsSharedPoolSettings(
        pool,
        "db-SOURCE",
        "vpc-11111111",
        "sg-11111111",
        ("sg-22222222",),
        "arn:aws:secretsmanager:ap-northeast-2:111111111111:secret:source-test01",
        "arn:aws:kms:ap-northeast-2:111111111111:key/11111111-1111-1111-1111-111111111111",
        str(ca),
    )
    settings = DedicatedDatabaseSettings(
        source, "sg-33333333", "sky-db-subnets", "sky-db-tls", "db.t4g.small", "17.6", 20, False
    )
    clients = {name: Mock() for name in ("sts", "rds", "ec2", "secretsmanager")}
    for client in clients.values():
        client.meta.region_name = pool.region
    session = Mock(client=Mock(side_effect=lambda name, **_: clients[name]))
    adapter = AwsDedicatedDatabasePreparer(settings, session=session, sleep=Mock(), connect=Mock())
    adapter.audit._audit = Mock()
    adapter.network._audit_network = Mock()
    source_binding = DatabaseBinding(
        "org", "game", "shared_workload", pool.account_id, pool.region, pool.instance_id, "game", "app-game"
    )
    target = replace(source_binding, role="dedicated_workload", instance_id="sky-game-dedicated")
    plan = plan_database_promotion(
        source_binding,
        target,
        PromotionTrigger("scheduled_review", "policy", "evidence", "2026-10-11T00:00:00Z"),
        source_revision="a" * 64,
    )
    plan["target_configuration_digest"] = settings.digest
    operation = str(uuid4())
    rds = clients["rds"]
    rds.describe_db_subnet_groups.return_value = {
        "DBSubnetGroups": [
            {
                "DBSubnetGroupName": settings.subnet_group,
                "VpcId": source.vpc_id,
                "SubnetGroupStatus": "Complete",
                "Subnets": [
                    {"SubnetAvailabilityZone": {"Name": zone}, "SubnetStatus": "Active"}
                    for zone in ("zone-a", "zone-b")
                ],
            }
        ]
    }
    rds.describe_db_parameters.return_value = {
        "Parameters": [{"ParameterName": "rds.force_ssl", "ParameterValue": "1"}]
    }
    tags = {
        "sky-managed": "true",
        "sky-database-role": "dedicated_workload",
        "sky-operation-id": operation,
        "sky-plan-id": plan["plan_id"],
        "sky-organization-id": target.organization_id,
        "sky-application-id": target.application_id,
        "sky-owner-ref": target.owner_ref,
        "sky-configuration-digest": settings.digest,
    }
    rds.list_tags_for_resource.return_value = {
        "TagList": [{"Key": key, "Value": value} for key, value in tags.items()]
    }
    instance = {
        "DBInstanceArn": f"arn:aws:rds:{pool.region}:{pool.account_id}:db:{target.instance_id}",
        "DBInstanceIdentifier": target.instance_id,
        "Engine": "postgres",
        "EngineVersion": settings.engine_version,
        "DBName": target.database_name,
        "MasterUsername": "skyadmin",
        "DBInstanceClass": settings.instance_class,
        "AllocatedStorage": 20,
        "StorageType": "gp3",
        "StorageEncrypted": True,
        "PubliclyAccessible": False,
        "MultiAZ": False,
        "DeletionProtection": True,
        "BackupRetentionPeriod": 7,
        "DBInstanceStatus": "available",
        "DbiResourceId": "db-" + "A" * 26,
        "DBSubnetGroup": {"DBSubnetGroupName": settings.subnet_group, "VpcId": source.vpc_id},
        "VpcSecurityGroups": [{"VpcSecurityGroupId": settings.database_security_group, "Status": "active"}],
        "DBParameterGroups": [
            {"DBParameterGroupName": settings.parameter_group, "ParameterApplyStatus": "in-sync"}
        ],
        "Endpoint": {"Address": "sky-test.xyz.ap-northeast-2.rds.amazonaws.com", "Port": 5432},
        "MasterUserSecret": {
            "SecretArn": "arn:aws:secretsmanager:ap-northeast-2:111111111111:secret:target-test01",
            "SecretStatus": "active",
        },
    }
    rds.describe_db_instances.return_value = {"DBInstances": [instance]}
    adapter.audit._secret_value = Mock(return_value={"username": "skyadmin", "password": "fixture-secret"})
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.cursor.return_value.__enter__.return_value.fetchone.return_value = (
        target.database_name,
        "skyadmin",
        True,
    )
    adapter.audit._connection = Mock(return_value=connection)
    return adapter, plan, operation, clients, instance, connection


def test_owned_existing_target_is_verified_without_creation(prepared):
    adapter, plan, operation, clients, _, _ = prepared
    receipt = adapter.prepare(plan, operation_id=operation, guard=Mock())
    assert receipt["binding"] == plan["target"]
    assert receipt["verified_scope"] == "rds_ownership_network_tls"
    clients["rds"].create_db_instance.assert_not_called()
    adapter.audit._connection.assert_called_once()
    assert "fixture-secret" not in str(receipt)


def test_missing_target_created_once_with_managed_credentials_and_pinned_capacity(prepared):
    adapter, plan, operation, clients, instance, _ = prepared
    rds = clients["rds"]
    rds.describe_db_instances.side_effect = [ServiceError("DBInstanceNotFound"), {"DBInstances": [instance]}]
    adapter.prepare(plan, operation_id=operation, guard=Mock())
    args = rds.create_db_instance.call_args.kwargs
    assert rds.create_db_instance.call_count == 1
    assert args["ManageMasterUserPassword"] is True and "MasterUserPassword" not in args
    assert args["DBInstanceIdentifier"] == plan["target"]["instance_id"]
    assert args["DeletionProtection"] is True and args["PubliclyAccessible"] is False
    assert args["AllocatedStorage"] == 20


@pytest.mark.parametrize(
    "field,value",
    [
        ("PubliclyAccessible", True),
        ("AllocatedStorage", 100),
        ("StorageEncrypted", False),
        ("DBName", "foreign"),
        ("EngineVersion", "17.7"),
        ("DeletionProtection", False),
        ("DBInstanceArn", "foreign"),
        ("MasterUsername", "other"),
    ],
)
def test_foreign_or_changed_target_is_never_adopted(prepared, field, value):
    adapter, plan, operation, clients, instance, _ = prepared
    instance[field] = value
    with pytest.raises(ValueError):
        adapter.prepare(plan, operation_id=operation, guard=Mock())
    clients["rds"].create_db_instance.assert_not_called()
    adapter.audit._secret_value.assert_not_called()


def test_configuration_change_and_other_operation_ownership_are_rejected(prepared):
    adapter, plan, operation, clients, _, _ = prepared
    plan["target_configuration_digest"] = "b" * 64
    with pytest.raises(ValueError):
        adapter.prepare(plan, operation_id=operation, guard=Mock())
    clients["rds"].create_db_instance.assert_not_called()
    plan["target_configuration_digest"] = adapter.settings.digest
    with pytest.raises(ValueError):
        adapter.prepare(plan, operation_id=str(uuid4()), guard=Mock())
    clients["rds"].create_db_instance.assert_not_called()


def test_uncertain_creation_is_not_retried(prepared):
    adapter, plan, operation, clients, _, _ = prepared
    rds = clients["rds"]
    rds.describe_db_instances.side_effect = ServiceError("DBInstanceNotFound")
    rds.create_db_instance.side_effect = OSError("response lost")
    with pytest.raises(OSError):
        adapter.prepare(plan, operation_id=operation, guard=Mock())
    assert rds.create_db_instance.call_count == 1


def test_permission_denied_is_not_treated_as_absent(prepared):
    adapter, plan, operation, clients, _, _ = prepared
    clients["rds"].describe_db_instances.side_effect = ServiceError("AccessDenied")
    with pytest.raises(ServiceError):
        adapter.prepare(plan, operation_id=operation, guard=Mock())
    clients["rds"].create_db_instance.assert_not_called()


@pytest.mark.parametrize("change", ["network", "ssl", "lease", "tls"])
def test_network_tls_and_execution_guards_block_preparation(prepared, change):
    adapter, plan, operation, clients, _, connection = prepared
    guard = Mock()
    if change == "network":
        adapter.network._audit_network.side_effect = ValueError("foreign network")
    elif change == "ssl":
        clients["rds"].describe_db_parameters.return_value = {"Parameters": []}
    elif change == "lease":
        guard.side_effect = OSError("lease lost")
    else:
        connection.cursor.return_value.__enter__.return_value.fetchone.return_value = (
            "game",
            "skyadmin",
            False,
        )
    with pytest.raises((ValueError, OSError)):
        adapter.prepare(plan, operation_id=operation, guard=guard)
    clients["rds"].create_db_instance.assert_not_called()


def test_ca_bytes_and_capacity_change_configuration_digest(prepared):
    adapter, *_ = prepared
    assert replace(adapter.settings, storage_gb=30).digest != adapter.settings.digest
    assert replace(adapter.settings, multi_az=True).digest != adapter.settings.digest


def test_wait_timeout_does_not_retry_creation(prepared):
    adapter, plan, operation, clients, _, _ = prepared
    clients["rds"].describe_db_instances.side_effect = ServiceError("DBInstanceNotFound")
    adapter.monotonic = Mock(side_effect=[0, 1201])
    with pytest.raises(TimeoutError):
        adapter.prepare(plan, operation_id=operation, guard=Mock())
    assert clients["rds"].create_db_instance.call_count == 1


def test_sdk_create_retry_is_disabled(prepared):
    adapter, *_ = prepared
    session = Mock(client=Mock(return_value=Mock()))
    AwsDedicatedDatabasePreparer(adapter.settings, session=session, connect=Mock())
    configured = [call.kwargs["config"] for call in session.client.call_args_list if "config" in call.kwargs]
    assert len(configured) == 1 and configured[0].retries == {"total_max_attempts": 1}


def test_tls_engine_default_on_later_page_is_valid(prepared):
    adapter, plan, operation, clients, _, _ = prepared
    clients["rds"].describe_db_parameters.side_effect = [
        {"Parameters": [], "Marker": "next"},
        {
            "Parameters": [
                {"ParameterName": "rds.force_ssl", "ParameterValue": "1", "Source": "engine-default"}
            ]
        },
    ]
    adapter.prepare(plan, operation_id=operation, guard=Mock())
    assert "Source" not in clients["rds"].describe_db_parameters.call_args.kwargs


def test_repeated_parameter_page_cannot_prove_tls(prepared):
    adapter, plan, operation, clients, _, _ = prepared
    clients["rds"].describe_db_parameters.return_value = {"Parameters": [], "Marker": "same"}
    with pytest.raises(ValueError, match="Ambiguous"):
        adapter.prepare(plan, operation_id=operation, guard=Mock())
    clients["rds"].create_db_instance.assert_not_called()


def test_runtime_clients_preserve_provisioning_digest_and_require_ownership_guard(prepared):
    old, plan, operation, clients, _, _ = prepared
    session = Mock(client=Mock(side_effect=lambda name, **_: clients[name]))
    guard = Mock(side_effect=ValueError("Foreign runtime client"))
    adapter = AwsDedicatedDatabasePreparer(
        old.settings,
        session=session,
        source_client_groups=("sg-22222222", "sg-44444444"),
        target_client_groups=("sg-22222222", "sg-55555555"),
        network_guard=guard,
    )
    assert adapter.settings.digest == plan["target_configuration_digest"]
    assert adapter.audit.settings.allowed_client_groups == ("sg-22222222", "sg-44444444")
    assert adapter.network.settings.allowed_client_groups == ("sg-22222222", "sg-55555555")
    with pytest.raises(ValueError, match="Foreign runtime client"):
        adapter.prepare(plan, operation_id=operation, guard=Mock())
    guard.assert_called_once()
    clients["rds"].create_db_instance.assert_not_called()
    clients["ec2"].describe_security_groups.assert_not_called()


@pytest.mark.parametrize(
    "groups,guard",
    [
        (("sg-22222222", "sg-44444444"), None),
        (("sg-44444444",), Mock()),
        (("sg-22222222", "sg-22222222"), Mock()),
    ],
)
def test_runtime_clients_cannot_be_implicit_remove_existing_or_duplicate(prepared, groups, guard):
    old, _, _, clients, _, _ = prepared
    session = Mock(client=Mock(side_effect=lambda name, **_: clients[name]))
    with pytest.raises(ValueError):
        AwsDedicatedDatabasePreparer(
            old.settings, session=session, source_client_groups=groups, network_guard=guard
        )
    clients["rds"].create_db_instance.assert_not_called()
