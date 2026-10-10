"""AWS binding must prove identity before SQL or credential mutation."""

import json
from copy import deepcopy
from dataclasses import replace
from unittest.mock import MagicMock, Mock, patch

import pytest

from adapters.aws.shared_database import AwsSharedDatabaseAllocator, AwsSharedPoolSettings
from application.shared_database import ManagedSharedDatabaseService
from domain.access import LoginSource, Principal, Role
from domain.shared_database import PoolAllocationRequest, SharedDatabasePool
from ports.shared_database import PoolAllocationError


class ServiceError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}
        super().__init__("private-error-diagnostic")


@pytest.fixture
def aws(tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("test-only-ca")
    pool = SharedDatabasePool("team", "111111111111", "ap-northeast-2", "sky-pool-team", "sky_pool_team", 30)
    prefix = f"arn:aws:secretsmanager:{pool.region}:{pool.account_id}:secret:"
    settings = AwsSharedPoolSettings(
        pool,
        "db-IMMUTABLE",
        "vpc-11111111",
        "sg-11111111",
        ("sg-22222222",),
        prefix + "rds!cluster-admin-test01",
        f"arn:aws:kms:{pool.region}:{pool.account_id}:key/11111111-1111-1111-1111-111111111111",
        str(ca),
    )
    clients = {name: Mock() for name in ("sts", "rds", "ec2", "secretsmanager")}
    for client in clients.values():
        client.meta.region_name = pool.region
    clients["sts"].get_caller_identity.return_value = {"Account": pool.account_id}
    instance = {
        "DBInstanceArn": f"arn:aws:rds:{pool.region}:{pool.account_id}:db:{pool.instance_id}",
        "DBInstanceIdentifier": pool.instance_id,
        "DbiResourceId": settings.resource_id,
        "Engine": "postgres",
        "DBInstanceStatus": "available",
        "DBName": pool.control_database,
        "PubliclyAccessible": False,
        "StorageEncrypted": True,
        "DBSubnetGroup": {"VpcId": settings.vpc_id},
        "VpcSecurityGroups": [{"VpcSecurityGroupId": settings.database_security_group, "Status": "active"}],
        "MasterUserSecret": {"SecretArn": settings.admin_secret_arn, "SecretStatus": "active"},
        "MasterUsername": "pool_admin",
        "Endpoint": {"Address": "sky-pool.abc.ap-northeast-2.rds.amazonaws.com", "Port": 5432},
    }
    clients["rds"].describe_db_instances.return_value = {"DBInstances": [instance]}
    clients["rds"].list_tags_for_resource.return_value = {
        "TagList": [
            {"Key": "sky-managed", "Value": "true"},
            {"Key": "sky-database-role", "Value": "shared_workload"},
            {"Key": "sky-pool-id", "Value": pool.id},
        ]
    }
    groups = [
        {"GroupId": group, "VpcId": settings.vpc_id, "OwnerId": pool.account_id}
        for group in (settings.database_security_group, *settings.allowed_client_groups)
    ]
    groups[0]["IpPermissions"] = [
        {
            "IpProtocol": "tcp",
            "FromPort": 5432,
            "ToPort": 5432,
            "UserIdGroupPairs": [{"GroupId": "sg-22222222", "UserId": pool.account_id}],
        }
    ]
    clients["ec2"].describe_security_groups.return_value = {"SecurityGroups": groups}
    records = {
        settings.admin_secret_arn: (
            {"ARN": settings.admin_secret_arn, "OwningService": "rds"},
            {"username": "pool_admin", "password": "disposable-admin-password"},
        )
    }

    def describe(SecretId):
        if SecretId not in records:
            raise ServiceError("ResourceNotFoundException")
        return deepcopy(records[SecretId][0])

    def create(**kwargs):
        name = kwargs["Name"]
        if name in records:
            raise ServiceError("ResourceExistsException")
        arn = prefix + name + "-test01"
        record = (
            {"ARN": arn, "Name": name, "KmsKeyId": kwargs["KmsKeyId"], "Tags": kwargs["Tags"]},
            json.loads(kwargs["SecretString"]),
        )
        records[name] = records[arn] = record
        return {"ARN": arn, "Name": name}

    def get(SecretId, VersionStage):
        assert VersionStage == "AWSCURRENT"
        return {
            "ARN": SecretId,
            "VersionStages": ["AWSCURRENT"],
            "SecretString": json.dumps(records[SecretId][1]),
        }

    clients["secretsmanager"].describe_secret.side_effect = describe
    clients["secretsmanager"].create_secret.side_effect = create
    clients["secretsmanager"].get_secret_value.side_effect = get
    clients["secretsmanager"].get_resource_policy.return_value = {}
    session = Mock()
    session.client.side_effect = lambda name, **_: clients[name]
    connect = Mock()
    adapter = AwsSharedDatabaseAllocator(settings, session=session, connect=connect)
    request = PoolAllocationRequest(pool, "team-a", "game")
    return adapter, request, clients, records, instance, groups, connect


def test_valid_allocation_creates_and_reuses_app_secret_and_returns_only_reference(aws):
    adapter, request, clients, records, _, _, connect = aws
    allocator = Mock()
    allocator.allocate.side_effect = lambda req, credentials: {
        "status": "ready",
        "secret_ref": credentials.secret_ref,
    }
    with patch.object(adapter, "_allocator", return_value=allocator):
        first = adapter.allocate(request)
        second = adapter.allocate(request)
    assert first == second
    assert clients["secretsmanager"].create_secret.call_count == 1
    assert "password" not in json.dumps(first)
    password = records[first["secret_ref"]][1]["password"]
    assert len(password) >= 24 and password not in json.dumps(first)
    assert allocator.allocate.call_args_list[0].args[1].password == password
    assert "tls_verify_full" in first["aws_checks"]  # SQL/TLS allocator is mocked in this unit test.
    connect.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        "account",
        "region",
        "resource",
        "public",
        "encryption",
        "engine",
        "state",
        "tags",
        "cidr",
        "other_client",
        "other_vpc",
        "extra_group",
    ],
)
def test_identity_or_network_drift_blocks_sql_and_secret_creation(aws, change):
    adapter, request, clients, _, instance, groups, connect = aws
    if change == "account":
        clients["sts"].get_caller_identity.return_value["Account"] = "222222222222"
    elif change == "region":
        clients["rds"].meta.region_name = "us-east-1"
    elif change == "resource":
        instance["DbiResourceId"] = "db-REPLACED"
    elif change == "public":
        instance["PubliclyAccessible"] = True
    elif change == "encryption":
        instance["StorageEncrypted"] = False
    elif change == "engine":
        instance["Engine"] = "mysql"
    elif change == "state":
        instance["DBName"] = "sky_state"
    elif change == "tags":
        clients["rds"].list_tags_for_resource.return_value["TagList"] = []
    elif change == "cidr":
        groups[0]["IpPermissions"][0]["IpRanges"] = [{"CidrIp": "0.0.0.0/0"}]
    elif change == "other_client":
        groups[0]["IpPermissions"][0]["UserIdGroupPairs"][0]["GroupId"] = "sg-33333333"
    elif change == "other_vpc":
        groups[1]["VpcId"] = "vpc-33333333"
    else:
        instance["VpcSecurityGroups"].append({"VpcSecurityGroupId": "sg-33333333", "Status": "active"})
    with pytest.raises(PoolAllocationError):
        adapter.allocate(request)
    connect.assert_not_called()
    clients["secretsmanager"].create_secret.assert_not_called()
    clients["secretsmanager"].get_secret_value.assert_not_called()


def test_unregistered_pool_never_creates_an_app_secret(aws):
    adapter, request, clients, _, _, _, _ = aws
    allocator = Mock()
    allocator.check_registration.side_effect = PoolAllocationError("Not registered")
    with patch.object(adapter, "_allocator", return_value=allocator), pytest.raises(PoolAllocationError):
        adapter.allocate(request)
    clients["secretsmanager"].create_secret.assert_not_called()
    allocator.allocate.assert_not_called()


@pytest.mark.parametrize(
    "change", ["owner", "kms", "deleted", "rotation", "identity", "policy", "version", "foreign_arn"]
)
def test_existing_secret_drift_blocks_allocation_without_overwrite(aws, change):
    adapter, request, clients, records, _, _, _ = aws
    credentials = adapter._app_credentials(request)
    metadata, value = records[credentials.secret_ref]
    if change == "owner":
        metadata["Tags"] = []
    elif change == "kms":
        metadata["KmsKeyId"] = "other-key"
    elif change == "deleted":
        metadata["DeletedDate"] = "now"
    elif change == "rotation":
        metadata["RotationEnabled"] = True
    elif change == "identity":
        value["dbname"] = "other-app"
    elif change == "policy":
        clients["secretsmanager"].get_resource_policy.return_value = {"ResourcePolicy": '{"Statement": []}'}
    elif change == "version":
        clients["secretsmanager"].get_secret_value.side_effect = lambda **_: {
            "ARN": credentials.secret_ref,
            "VersionStages": ["AWSPENDING"],
        }
    else:
        metadata["ARN"] = credentials.secret_ref.replace("111111111111", "222222222222")
    allocator = Mock()
    with patch.object(adapter, "_allocator", return_value=allocator), pytest.raises(PoolAllocationError):
        adapter.allocate(request)
    assert clients["secretsmanager"].create_secret.call_count == 1
    allocator.allocate.assert_not_called()
    clients["secretsmanager"].put_secret_value.assert_not_called()
    clients["secretsmanager"].delete_secret.assert_not_called()


def test_concurrent_secret_create_uses_winning_credentials(aws):
    adapter, request, clients, records, _, _, _ = aws
    winner = adapter._app_credentials(request)
    name = f"sky-pool/{request.pool.id}/{request.id}"
    describe = clients["secretsmanager"].describe_secret.side_effect
    clients["secretsmanager"].describe_secret.side_effect = [
        ServiceError("ResourceNotFoundException"),
        describe(name),
    ]
    clients["secretsmanager"].create_secret.side_effect = ServiceError("ResourceExistsException")
    credentials = adapter._app_credentials(request)
    assert credentials.password == records[winner.secret_ref][1]["password"]
    assert credentials.secret_ref == winner.secret_ref


def test_uncertain_secret_creation_is_sanitized_and_not_retried_or_deleted(aws):
    adapter, request, clients, _, _, _, _ = aws
    clients["secretsmanager"].create_secret.side_effect = RuntimeError("private-error-diagnostic")
    with (
        patch.object(adapter, "_allocator", return_value=Mock()),
        pytest.raises(PoolAllocationError) as error,
    ):
        adapter.allocate(request)
    assert "private-error-diagnostic" not in str(error.value) and error.value.__suppress_context__
    clients["secretsmanager"].create_secret.assert_called_once()
    clients["secretsmanager"].delete_secret.assert_not_called()


def test_tls_is_verified_and_missing_tls_closes_connection(aws):
    adapter, _, _, _, _, _, connect = aws
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = (True,)
    connect.return_value = connection
    assert adapter._connection("host", 5432, "database", "user", "password") is connection
    assert connect.call_args.kwargs["sslmode"] == "verify-full"
    assert connect.call_args.kwargs["sslrootcert"] == adapter.settings.sslrootcert
    assert connect.call_args.kwargs["connect_timeout"] == 10
    connection.execute.return_value.fetchone.return_value = (False,)
    with pytest.raises(PoolAllocationError, match="TLS"):
        adapter._connection("host", 5432, "database", "user", "password")
    connection.close.assert_called_once()


def test_authorization_happens_before_aws_calls_and_initialization_is_explicit(aws):
    adapter, request, clients, _, _, _, connect = aws
    viewer = Principal("viewer", "team-a", Role.VIEWER, LoginSource.EXTERNAL_IDP)
    application = {"application_id": "game", "organization_id": "team-a", "created_by": "creator"}
    with pytest.raises(PermissionError):
        ManagedSharedDatabaseService(adapter).allocate(viewer, application, request)
    clients["sts"].get_caller_identity.assert_not_called()
    allocator = Mock()
    with patch.object(adapter, "_allocator", return_value=allocator):
        adapter.initialize()
    allocator.initialize.assert_called_once()
    clients["secretsmanager"].create_secret.assert_not_called()
    connect.assert_not_called()


def test_missing_ca_or_foreign_admin_secret_rejected_before_clients(aws, tmp_path):
    adapter, *_ = aws
    with pytest.raises(ValueError, match="CA"):
        replace(adapter.settings, sslrootcert=str(tmp_path / "missing.pem"))
    with pytest.raises(ValueError, match="account"):
        replace(
            adapter.settings,
            admin_secret_arn=adapter.settings.admin_secret_arn.replace("111111111111", "222222222222"),
        )


@pytest.mark.parametrize("change", ["service", "username", "version", "deleted"])
def test_invalid_master_credentials_never_create_app_secret_or_allocate(aws, change):
    adapter, request, clients, records, _, _, connect = aws
    metadata, value = records[adapter.settings.admin_secret_arn]
    if change == "service":
        metadata["OwningService"] = "other"
    elif change == "username":
        value["username"] = "another_admin"
    elif change == "version":
        clients["secretsmanager"].get_secret_value.side_effect = lambda **_: {
            "ARN": adapter.settings.admin_secret_arn,
            "VersionStages": ["AWSPENDING"],
        }
    else:
        metadata["DeletedDate"] = "now"
    with pytest.raises(PoolAllocationError):
        adapter.allocate(request)
    connect.assert_not_called()
    clients["secretsmanager"].create_secret.assert_not_called()


def test_allocation_failure_retains_secret_and_does_not_retry_sql(aws):
    adapter, request, clients, records, _, _, _ = aws
    allocator = Mock()
    allocator.allocate.side_effect = PoolAllocationError("uncertain SQL outcome")
    with patch.object(adapter, "_allocator", return_value=allocator), pytest.raises(PoolAllocationError):
        adapter.allocate(request)
    allocator.allocate.assert_called_once()
    assert len(records) == 3  # master, plus one app secret indexed by name and ARN
    clients["secretsmanager"].delete_secret.assert_not_called()
    clients["secretsmanager"].put_secret_value.assert_not_called()
