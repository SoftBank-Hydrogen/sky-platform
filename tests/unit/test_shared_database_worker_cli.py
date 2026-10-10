"""Worker configuration is opt-in, digest-bound and checked without cloud calls."""

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from interfaces.b_runtime import main
from interfaces.shared_database_worker import current_membership, load_pool_configuration


@pytest.fixture
def configuration(tmp_path, monkeypatch):
    ca = tmp_path / "ca.pem"
    ca.write_text("test-only-ca")
    account, region = "111111111111", "ap-northeast-2"
    settings = {
        "pool": {
            "id": "team",
            "account_id": account,
            "region": region,
            "instance_id": "sky-team",
            "control_database": "sky_pool_team",
            "connection_budget": 30,
        },
        "resource_id": "db-IMMUTABLE",
        "vpc_id": "vpc-11111111",
        "database_security_group": "sg-11111111",
        "allowed_client_groups": ["sg-22222222"],
        "admin_secret_arn": f"arn:aws:secretsmanager:{region}:{account}:secret:rds!admin-test01",
        "app_secret_kms_arn": f"arn:aws:kms:{region}:{account}:key/11111111-1111-1111-1111-111111111111",
        "sslrootcert": str(ca),
    }
    path = tmp_path / "pool.json"
    path.write_text(json.dumps({"version": 1, "settings": settings}))
    for key, value in {
        "SKY_DATABASE_HOST": "state.example",
        "SKY_DATABASE_NAME": "sky_state",
        "SKY_DATABASE_SECRET_ARN": f"arn:aws:secretsmanager:{region}:{account}:secret:state-test01",
        "SKY_DATABASE_SSLROOTCERT": str(ca),
        "SKY_AWS_REGION": region,
        "SKY_AWS_ACCOUNT_ID": account,
    }.items():
        monkeypatch.setenv(key, value)
    return path, ca


def arguments(path):
    return [
        "worker",
        "--mode",
        "shared-database",
        "--pool-config",
        str(path),
        "--alb-trusts-file",
        "trusts.json",
        "--memberships-file",
        "members.json",
    ]


def test_check_config_never_constructs_cloud_clients_or_touches_databases(configuration, capsys):
    path, _ = configuration
    with (
        patch("interfaces.shared_database_worker.AlbRequestAuthenticator.from_files", return_value=Mock()),
        patch(
            "interfaces.shared_database_worker.AwsSharedDatabaseAllocator",
            side_effect=AssertionError("AWS call"),
        ),
        patch(
            "interfaces.shared_database_worker.RotatingDatabaseConnection",
            side_effect=AssertionError("DB call"),
        ),
    ):
        main([*arguments(path), "--check-config"])
    assert "no AWS/DB calls" in capsys.readouterr().out


def test_registration_digest_binds_ca_contents_and_pool_settings(configuration):
    path, ca = configuration
    _, first = load_pool_configuration(path)
    ca.write_text("changed-ca")
    _, second = load_pool_configuration(path)
    assert first != second
    value = json.loads(path.read_text())
    value["settings"]["resource_id"] = "db-REPLACED"
    path.write_text(json.dumps(value))
    assert load_pool_configuration(path)[1] != second


@pytest.mark.parametrize("change", ["account", "state", "missing_ids"])
def test_invalid_runtime_scope_or_missing_ids_fails_without_aws(configuration, monkeypatch, change):
    path, _ = configuration
    if change == "account":
        monkeypatch.setenv("SKY_AWS_ACCOUNT_ID", "222222222222")
    elif change == "state":
        monkeypatch.setenv("SKY_DATABASE_NAME", "sky_pool_team")
    with (
        patch("interfaces.shared_database_worker.AlbRequestAuthenticator.from_files", return_value=Mock()),
        patch(
            "interfaces.shared_database_worker.AwsSharedDatabaseAllocator",
            side_effect=AssertionError("AWS call"),
        ),
        pytest.raises(SystemExit) as error,
    ):
        main(arguments(path))
    assert error.value.code == 2


def test_outbox_does_not_accept_or_activate_shared_pool_options():
    with pytest.raises(SystemExit) as error:
        main(["worker", "--mode", "outbox", "--pool-config", "pool.json"])
    assert error.value.code == 2


def test_current_membership_is_required_and_ambiguous_grants_fail_closed():
    from domain.access import LoginSource, Role

    authenticator = SimpleNamespace(
        memberships=SimpleNamespace(records={("issuer", "subject"): ("user", "team-a", Role.DEPLOYER)}),
        trusts=[SimpleNamespace(issuer="issuer", login_source=LoginSource.EXTERNAL_IDP)],
    )
    assert current_membership(authenticator, "user", "team-a").role is Role.DEPLOYER
    with pytest.raises(PermissionError):
        current_membership(authenticator, "user", "other")
    authenticator.memberships.records[("issuer", "other-subject")] = ("user", "team-a", Role.VIEWER)
    with pytest.raises(PermissionError):
        current_membership(authenticator, "user", "team-a")
