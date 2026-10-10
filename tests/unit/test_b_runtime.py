"""Runtime routing, fail-closed configuration and bounded outbox shutdown."""

import json
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from interfaces.b_runtime import main, run_outbox
from interfaces.service import main as service_main


def test_api_routes_to_database_without_local_state():
    with (
        patch("sys.argv", ["sky-service", "api", "--host", "127.0.0.1", "--auth-mode", "local"]),
        patch("interfaces.service.require_service_state", side_effect=AssertionError("Local state")),
        patch("interfaces.http.server.serve") as serve,
    ):
        previous = sys.argv
        service_main()
        serve.assert_called_once_with(product_name="Sky")
        assert sys.argv is previous


def test_api_forwards_read_only_and_alb_configuration():
    def serve(**_):
        assert "--read-only-database" in sys.argv
        assert sys.argv[sys.argv.index("--auth-mode") + 1] == "alb"
        assert sys.argv[sys.argv.index("--alb-trusts-file") + 1] == "trusts.json"

    with patch("interfaces.http.server.serve", side_effect=serve):
        main(["api", "--alb-trusts-file", "trusts.json", "--memberships-file", "members.json"])


@pytest.mark.parametrize("args", [["api"], ["api", "--auth-mode", "local"]])
def test_public_api_rejects_missing_alb_identity(args):
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 2


def test_worker_does_not_silently_start_legacy_web_or_claim_deployments():
    with (
        patch("interfaces.service.require_service_state", side_effect=AssertionError("Local state")),
        patch("sys.argv", ["sky-service", "worker"]),
        pytest.raises(SystemExit) as error,
    ):
        service_main()
    assert error.value.code == 2


@pytest.fixture
def worker_environment(monkeypatch, tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("configuration validation only")
    values = {
        "SKY_DATABASE_HOST": "db.example",
        "SKY_DATABASE_NAME": "sky",
        "SKY_DATABASE_SECRET_ARN": "arn:aws:secretsmanager:ap-northeast-2:977889523182:secret:test",
        "SKY_AWS_REGION": "ap-northeast-2",
        "SKY_AWS_ACCOUNT_ID": "977889523182",
        "SKY_DATABASE_SSLROOTCERT": str(ca),
        "SKY_STATE_WORKSPACE": "test",
        "SKY_JOB_QUEUE_URL": "https://sqs.ap-northeast-2.amazonaws.com/977889523182/test.fifo",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_worker_check_config_never_connects_or_sends(worker_environment, capsys):
    with (
        patch("boto3.client", side_effect=AssertionError("AWS call")),
        patch("adapters.state.postgres.RotatingDatabaseConnection", side_effect=AssertionError("DB call")),
        patch(
            "adapters.state.operations.PostgresOperationStore.initialize", side_effect=AssertionError("DDL")
        ),
    ):
        main(["worker", "--mode", "outbox", "--check-config"])
    assert "outbox publisher only" in capsys.readouterr().out


@pytest.mark.parametrize(
    "queue",
    [
        "https://sqs.ap-northeast-2.amazonaws.com/000000000000/test.fifo",
        "https://sqs.ap-northeast-2.amazonaws.com/977889523182/test",
    ],
)
def test_worker_rejects_wrong_account_or_non_fifo(worker_environment, monkeypatch, queue):
    monkeypatch.setenv("SKY_JOB_QUEUE_URL", queue)
    with patch("boto3.client", side_effect=AssertionError("AWS call")), pytest.raises(SystemExit):
        main(["worker", "--mode", "outbox", "--check-config"])


@pytest.mark.parametrize("interval", ["0", "301"])
def test_worker_rejects_invalid_interval(interval):
    with pytest.raises(SystemExit):
        main(["worker", "--mode", "outbox", "--interval", interval])


def test_outbox_once_is_bounded():
    publisher = Mock(dispatch_once=Mock(return_value=SimpleNamespace(confirmed=1, deferred=0)))
    run_outbox(publisher, threading.Event(), once=True)
    publisher.dispatch_once.assert_called_once_with(limit=1)


def test_outbox_shutdown_finishes_current_send_without_new_claim():
    stop = threading.Event()

    def dispatch(**_):
        stop.set()
        return SimpleNamespace(confirmed=1, deferred=0)

    publisher = Mock(dispatch_once=Mock(side_effect=dispatch))
    run_outbox(publisher, stop)
    publisher.dispatch_once.assert_called_once_with(limit=1)


def test_worker_missing_schema_is_not_initialized_or_sent(worker_environment, capsys):
    with (
        patch("adapters.state.postgres.RotatingDatabaseConnection"),
        patch("adapters.state.readiness.check_database_ready", side_effect=ValueError("private diagnostic")),
        patch(
            "adapters.state.operations.PostgresOperationStore.initialize", side_effect=AssertionError("DDL")
        ),
        patch("application.outbox.OutboxPublisher", side_effect=AssertionError("Publish")),
        pytest.raises(SystemExit) as error,
    ):
        main(["worker", "--mode", "outbox", "--once"])
    assert error.value.code == 1
    assert "private diagnostic" not in capsys.readouterr().err


def test_worker_credentials_provider_error_is_redacted(worker_environment, capsys):
    from botocore.exceptions import ProxyConnectionError

    with (
        patch(
            "adapters.state.postgres.RotatingDatabaseConnection",
            side_effect=ProxyConnectionError(proxy_url="private-proxy"),
        ),
        pytest.raises(SystemExit) as error,
    ):
        main(["worker", "--mode", "outbox", "--once"])
    assert error.value.code == 1
    assert "private-proxy" not in capsys.readouterr().err


def test_migrate_routes_without_local_state(worker_environment):
    with (
        patch("sys.argv", ["sky-service", "migrate"]),
        patch("interfaces.service.require_service_state", side_effect=AssertionError("Local state")),
        patch("adapters.state.postgres.RotatingDatabaseConnection") as connection,
        patch("interfaces.b_runtime.run_migrations") as run,
    ):
        service_main()
    run.assert_called_once_with(connection.return_value, "test")


@pytest.mark.parametrize(
    "variable, value",
    [
        ("SKY_DATABASE_HOST", ""),
        ("SKY_DATABASE_SECRET_ARN", "not-an-arn"),
        ("SKY_STATE_WORKSPACE", "bad/space"),
    ],
)
def test_migrate_rejects_invalid_configuration_without_connecting(
    worker_environment, monkeypatch, variable, value
):
    monkeypatch.setenv(variable, value)
    with (
        patch("adapters.state.postgres.RotatingDatabaseConnection", side_effect=AssertionError("DB call")),
        pytest.raises(SystemExit) as error,
    ):
        main(["migrate"])
    assert error.value.code == 2


def test_migrate_rejects_extra_arguments():
    with pytest.raises(SystemExit) as error:
        main(["migrate", "--state-dir", "/.sky"])
    assert error.value.code == 2


@pytest.mark.parametrize(
    "failure",
    [OSError("password=private-secret"), ValueError("CREATE TABLE private_query")],
)
def test_migrate_failure_exits_nonzero_without_diagnostics(worker_environment, capsys, failure):
    with (
        patch("adapters.state.postgres.RotatingDatabaseConnection"),
        patch("interfaces.b_runtime.run_migrations", side_effect=failure),
        pytest.raises(SystemExit) as error,
    ):
        main(["migrate"])
    assert error.value.code == 1
    output = capsys.readouterr()
    assert "private" not in output.out + output.err
    assert "migration failed" in output.err


def test_migrate_credentials_provider_error_is_redacted(worker_environment, capsys):
    from botocore.exceptions import ProxyConnectionError

    with (
        patch(
            "adapters.state.postgres.RotatingDatabaseConnection",
            side_effect=ProxyConnectionError(proxy_url="private-proxy"),
        ),
        pytest.raises(SystemExit) as error,
    ):
        main(["migrate"])
    assert error.value.code == 1
    assert "private-proxy" not in capsys.readouterr().err


def _identity_documents(**member):
    from tests.unit.test_alb_identity import CLIENT, ISSUER, SIGNER

    trusts = {
        "version": 1,
        "trusts": [
            {"signer_arn": SIGNER, "issuer": ISSUER, "client_id": CLIENT, "login_source": "corporate_sso"}
        ],
    }
    members = {
        "version": 1,
        "members": [
            {
                "issuer": ISSUER,
                "subject": "identity-123",
                "user_id": "alice",
                "organization_id": "team_a",
                "role": "viewer",
                "enabled": True,
                **member,
            }
        ],
    }
    return json.dumps(trusts), json.dumps(members)


@pytest.fixture
def api_server():
    fake_server = Mock()
    with (
        patch("adapters.state.postgres.PostgresStateSettings.from_environment", return_value=Mock()),
        patch("adapters.state.postgres.RotatingDatabaseConnection"),
        patch("interfaces.http.server.ThreadingHTTPServer", return_value=fake_server) as server_class,
        patch("interfaces.http.server.App", side_effect=AssertionError("Legacy App constructed")),
    ):
        yield server_class, fake_server


def test_api_reads_alb_identity_from_environment(monkeypatch, api_server):
    from domain.access import LoginSource, Principal, Role
    from tests.unit.test_alb_identity import ISSUER

    trusts, members = _identity_documents()
    monkeypatch.setenv("SKY_ALB_TRUSTS_JSON", trusts)
    monkeypatch.setenv("SKY_MEMBERSHIPS_JSON", members)
    server_class, fake_server = api_server
    with (
        patch(
            "interfaces.http.server.AlbRequestAuthenticator.from_files", side_effect=AssertionError("File")
        ),
        patch("interfaces.http.server.handler_for") as handler_for,
    ):
        main(["api"])
    assert server_class.call_args.args[0] == ("0.0.0.0", 8080)
    fake_server.serve_forever.assert_called_once()
    app = handler_for.call_args.args[0]
    assert app.authenticator.memberships.resolve(
        ISSUER, "identity-123", LoginSource.CORPORATE_SSO
    ) == Principal("alice", "team_a", Role.VIEWER, LoginSource.CORPORATE_SSO)


@pytest.mark.parametrize(
    "args, environment, message",
    [
        (
            ["--alb-trusts-file", "t.json", "--memberships-file", "m.json"],
            ("SKY_ALB_TRUSTS_JSON",),
            "not both",
        ),
        (["--alb-trusts-file", "t.json"], ("SKY_MEMBERSHIPS_JSON",), "not both"),
        ([], ("SKY_ALB_TRUSTS_JSON",), "requires both SKY_ALB_TRUSTS_JSON and SKY_MEMBERSHIPS_JSON"),
        ([], ("SKY_MEMBERSHIPS_JSON",), "requires both SKY_ALB_TRUSTS_JSON and SKY_MEMBERSHIPS_JSON"),
        (
            ["--host", "127.0.0.1", "--auth-mode", "local"],
            ("SKY_ALB_TRUSTS_JSON",),
            "requires --auth-mode alb",
        ),
    ],
)
def test_api_rejects_ambiguous_or_partial_identity_sources(
    monkeypatch, capsys, api_server, args, environment, message
):
    trusts, members = _identity_documents()
    values = {"SKY_ALB_TRUSTS_JSON": trusts, "SKY_MEMBERSHIPS_JSON": members}
    for name in environment:
        monkeypatch.setenv(name, values[name])
    with (
        patch(
            "interfaces.http.server.AlbRequestAuthenticator.from_files", side_effect=AssertionError("File")
        ),
        pytest.raises(SystemExit) as error,
    ):
        main(["api", *args])
    assert error.value.code == 2
    assert message in capsys.readouterr().err
    api_server[1].serve_forever.assert_not_called()


@pytest.mark.parametrize(
    "members",
    [
        "not json",
        _identity_documents(user_id="alice@example.com")[1],
        _identity_documents(issuer="https://unknown.example/")[1],
        _identity_documents(extra="field")[1],
        '{"version": 1, "version": 1, "members": []}',
    ],
)
def test_api_rejects_invalid_environment_identity_without_echoing_it(
    monkeypatch, capsys, api_server, members
):
    monkeypatch.setenv("SKY_ALB_TRUSTS_JSON", _identity_documents()[0])
    monkeypatch.setenv("SKY_MEMBERSHIPS_JSON", members)
    with pytest.raises(SystemExit) as error:
        main(["api"])
    assert error.value.code == 2
    error_output = capsys.readouterr().err
    assert "Invalid hosted identity configuration" in error_output
    assert "identity-123" not in error_output and "alice" not in error_output
    api_server[1].serve_forever.assert_not_called()
