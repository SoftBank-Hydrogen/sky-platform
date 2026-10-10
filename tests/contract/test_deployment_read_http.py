"""Actual HTTP requests to the read-only boundary, backed by disposable PostgreSQL."""

import base64
import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch

import pytest

from interfaces.http.deployment_reads import DatabaseReadApp
from interfaces.http.server import handler_for, serve
from tests.contract.test_deployment_reads import (
    job,
    principal,
)

pytest_plugins = ["tests.contract.test_deployment_reads"]


@pytest.fixture
def http(setup):
    store, _connect, service = setup
    app = DatabaseReadApp(service, workspace=store.workspace)
    app.authenticator = Mock(authenticate=lambda token: principal() if token == app.token else None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(path, method="GET", token=True):
        client = HTTPConnection(*server.server_address, timeout=5)
        try:
            client.request(method, path, headers={"X-Sky-Token": app.token} if token else {})
            response = client.getresponse()
            data = response.read()
            return response.status, json.loads(data) if response.getheader("Content-Type", "").startswith(
                "application/json"
            ) else data.decode()
        finally:
            client.close()

    yield store, app, request
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def test_http_list_detail_history_certificate_and_releases(http):
    store, _app, request = http
    identifier = "a" * 16
    store.save_job(identifier, job(identifier, application_id="app1", events=[]))
    store.save_health(identifier, [{"healthy": True, "checked_at": "2026-10-10T00:00:00Z", "reason": "ok"}])
    status, page = request("/api/jobs")
    assert status == 200 and page["items"][0]["id"] == identifier and page["next_cursor"] is None
    assert request("/api/jobs/" + identifier)[1]["last_health"]["healthy"] is True
    assert request("/api/jobs/" + identifier + "/history")[1][0]["healthy"] is True
    assert request("/api/jobs/" + identifier + "/certificate")[0] == 200
    assert request("/api/applications/app1/releases")[1]["items"][0]["id"] == identifier
    assert request("/")[0] == 200
    assert "__TOKEN__" not in request("/")[1]
    assert request("/api/config")[1]["read_only"] is True
    assert store.load_job(identifier).revision == 1


def test_cursor_crosses_independent_http_instances_without_secret(http):
    store, app, request = http
    for letter in ("a", "b", "c"):
        store.save_job(letter * 16, job(letter * 16))
    first = request("/api/jobs?limit=2")[1]
    token = first["next_cursor"]
    from urllib.parse import quote

    second = request("/api/jobs?limit=2&cursor=" + quote(token))[1]
    assert [x["id"] for x in first["items"] + second["items"]] == ["c" * 16, "b" * 16, "a" * 16]
    replica = DatabaseReadApp(app.service, workspace=app.workspace)
    assert replica.cursor(token, principal(), None).record_id == "b" * 16
    with pytest.raises(ValueError):
        replica.cursor(token, principal("org2"), None)
    with pytest.raises(ValueError):
        DatabaseReadApp(app.service, workspace="different").cursor(token, principal(), None)


def test_other_organization_and_missing_records_are_indistinguishable(http):
    store, _, request = http
    store.save_job("a" * 16, job("a" * 16, organization_id="org2"))
    assert request("/api/jobs")[1]["items"] == []
    assert (
        request("/api/jobs/" + "a" * 16) == request("/api/jobs/" + "b" * 16) == (404, {"error": "Not found"})
    )


@pytest.mark.parametrize(
    "path",
    [
        "/api/jobs?limit=0",
        "/api/jobs?limit=101",
        "/api/jobs?limit=1&limit=2",
        "/api/jobs?cursor=invalid",
        "/api/jobs?other=value",
        "/api/jobs?limit=1&cursor=x&extra=x",
        "/api/jobs?limit=",
        "/api/jobs?cursor=" + base64.b64encode(b"[]").decode(),
    ],
)
def test_bad_pagination_returns_400(http, path):
    assert http[2](path)[0] == 400


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_all_mutations_are_denied_without_calling_service(http, method):
    _, app, request = http
    app.service = Mock()
    assert request("/api/deployments", method)[0] == 405
    app.service.summaries.assert_not_called()
    assert request("/api/deployments", method, token=False)[0] == 403


def test_health_probe_is_not_triggered_by_read_requests(http):
    assert http[2]("/api/jobs/" + "a" * 16 + "/health")[0] == 404
    assert http[2]("/health", token=False) == (200, {"status": "ok"})
    assert http[2]("/api/jobs", token=False)[0] == 403


def test_database_errors_are_redacted_and_never_fall_back_to_local(http):
    _, app, request = http
    app.service = Mock()
    app.service.summaries.side_effect = OSError("private credentials and SQL")
    assert request("/api/jobs") == (503, {"error": "Deployment records temporarily unavailable"})
    app.service.summaries.side_effect = ValueError("private stored document")
    assert request("/api/jobs") == (500, {"error": "Invalid persisted deployment record"})


def test_read_only_cli_bypasses_local_app_lock_and_background_workers():
    fake_server = Mock()
    with (
        patch("sys.argv", ["sky-platform", "--read-only-database"]),
        patch("adapters.state.postgres.PostgresStateSettings.from_environment", return_value=Mock()),
        patch("adapters.state.postgres.RotatingDatabaseConnection"),
        patch("interfaces.http.server.ThreadingHTTPServer", return_value=fake_server),
        patch("interfaces.http.server.App", side_effect=AssertionError("Legacy App constructed")),
        patch("interfaces.http.server.StateDirectoryLock", side_effect=AssertionError("State lock acquired")),
    ):
        serve()
    fake_server.serve_forever.assert_called_once()
    fake_server.server_close.assert_called_once()


def test_read_only_cli_rejects_public_bind():
    with (
        patch("sys.argv", ["sky-platform", "--read-only-database", "--host", "0.0.0.0"]),
        pytest.raises(SystemExit),
    ):
        serve()


def test_service_read_only_does_not_require_local_state():
    from interfaces.service import main

    with (
        patch("sys.argv", ["sky-service", "--read-only-database"]),
        patch("interfaces.service.require_service_state", side_effect=AssertionError("Local state required")),
        patch("interfaces.cli.main") as run,
    ):
        main()
        run.assert_called_once()
        import sys

        assert "--read-only-database" in sys.argv
