"""Runtime routing, fail-closed configuration and bounded outbox shutdown."""

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
