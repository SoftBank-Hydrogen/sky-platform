"""Credential rotation must not retry a write or expose a secret."""

from unittest.mock import Mock, patch

import pytest

psycopg = pytest.importorskip("psycopg")

from adapters.state.postgres import (
    DatabaseCredentials,
    PostgresDeploymentRecordStore,
    PostgresStateSettings,
    RotatingDatabaseConnection,
)


def settings():
    return PostgresStateSettings("state.example", 5432, "sky", "arn", "ap-northeast-2", "/rds-ca.pem")


def test_connection_rotates_credentials_before_retry_and_verifies_tls():
    secrets = Mock()
    secrets.get_secret_value.side_effect = [
        {"SecretString": '{"username":"sky","password":"old-test-password"}'},
        {"SecretString": '{"username":"sky","password":"new-test-password"}'},
    ]
    connection = Mock()
    connect = Mock(side_effect=[psycopg.OperationalError("auth failed"), connection])
    factory = RotatingDatabaseConnection(settings(), secrets_client=secrets, connect=connect)
    assert factory() is connection
    assert [call.kwargs["password"] for call in connect.call_args_list] == [
        "old-test-password",
        "new-test-password",
    ]
    assert all(call.kwargs["sslmode"] == "verify-full" for call in connect.call_args_list)
    assert all(call.kwargs["sslrootcert"] == "/rds-ca.pem" for call in connect.call_args_list)
    assert secrets.get_secret_value.call_count == 2


def test_connection_failure_is_bounded_and_has_no_secret_in_message():
    secrets = Mock()
    secrets.get_secret_value.return_value = {"SecretString": '{"username":"sky","password":"private"}'}
    connect = Mock(side_effect=psycopg.OperationalError("private"))
    factory = RotatingDatabaseConnection(settings(), secrets_client=secrets, connect=connect)
    with pytest.raises(OSError) as error:
        factory()
    assert connect.call_count == 2
    assert "private" not in str(error.value)
    assert error.value.__suppress_context__
    assert "private" not in repr(DatabaseCredentials("sky", "private"))


@pytest.mark.parametrize("secret", ["null", "{}", '{"username":"sky","password":null}', "not-json"])
def test_invalid_secret_never_connects(secret):
    secrets = Mock()
    secrets.get_secret_value.return_value = {"SecretString": secret}
    connect = Mock()
    with pytest.raises(OSError, match="credentials"):
        RotatingDatabaseConnection(settings(), secrets_client=secrets, connect=connect)()
    connect.assert_not_called()


def test_config_requires_ca_and_rejects_endpoint_connection_options(tmp_path):
    values = {
        "SKY_DATABASE_HOST": "state.example",
        "SKY_DATABASE_NAME": "sky",
        "SKY_DATABASE_SECRET_ARN": "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:rds-test",
        "SKY_AWS_REGION": "ap-northeast-2",
        "SKY_DATABASE_SSLROOTCERT": str(tmp_path / "ca.pem"),
    }
    with pytest.raises(ValueError, match="CA bundle"):
        PostgresStateSettings.from_environment(values)
    (tmp_path / "ca.pem").write_text("test-only")
    assert PostgresStateSettings.from_environment(values).port == 5432
    values["SKY_DATABASE_HOST"] = "state.example sslmode=disable"
    with pytest.raises(ValueError, match="endpoint"):
        PostgresStateSettings.from_environment(values)


def test_write_failure_is_not_retried():
    connection = Mock()
    connection.execute.side_effect = psycopg.OperationalError("sensitive document")

    class Context:
        def __enter__(self):
            return connection

        def __exit__(self, *args):
            return False

    factory = Mock(return_value=Context())
    with pytest.raises(OSError, match="uncertain") as error:
        PostgresDeploymentRecordStore(factory).save_job("job1", {"value": "sensitive document"})
    assert factory.call_count == connection.execute.call_count == 1
    assert "sensitive document" not in str(error.value)


def test_non_json_write_does_not_open_connection():
    factory = Mock()
    with pytest.raises(ValueError):
        PostgresDeploymentRecordStore(factory).save_job("job1", {"bad": float("nan")})
    factory.assert_not_called()


def test_credentials_are_cached_and_expire_without_logging_secret():
    secrets = Mock()
    secrets.get_secret_value.return_value = {"SecretString": '{"username":"sky","password":"private"}'}
    factory = RotatingDatabaseConnection(settings(), secrets_client=secrets, connect=Mock())
    with patch("adapters.state.postgres.time.monotonic", return_value=10):
        factory()
        factory()
    assert secrets.get_secret_value.call_count == 1
    with patch("adapters.state.postgres.time.monotonic", return_value=311):
        factory()
    assert secrets.get_secret_value.call_count == 2


def test_secrets_manager_failure_does_not_fall_back_to_local_credentials():
    secrets = Mock()
    secrets.get_secret_value.side_effect = RuntimeError("private-token")
    connect = Mock()
    with pytest.raises(OSError) as error:
        RotatingDatabaseConnection(settings(), secrets_client=secrets, connect=connect)()
    assert "private-token" not in str(error.value)
    connect.assert_not_called()
