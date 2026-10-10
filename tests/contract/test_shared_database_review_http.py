"""Authenticated DB review consent and status reads over disposable PostgreSQL."""

import json
import threading
from contextlib import contextmanager
from dataclasses import replace
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import pytest

from adapters.state.deployment_reads import PostgresDeploymentReads
from application.deployment_reads import DeploymentReadService
from domain.access import Role
from interfaces.http.shared_database import SharedDatabaseApp, handler_for_shared_database
from tests.contract.test_shared_database_reviews import ACTOR, JOB, counts

pytest_plugins = ["tests.contract.test_shared_database_reviews"]


class Authenticator:
    def authenticate_request(self, headers):
        return {
            "deployer": ACTOR,
            "viewer": replace(ACTOR, role=Role.VIEWER),
            "foreign": replace(ACTOR, organization_id="foreign"),
        }.get(headers.get("Authorization"))


@contextmanager
def http_app(store, database):
    app = SharedDatabaseApp(
        DeploymentReadService(PostgresDeploymentReads(database, workspace=store.operations.workspace)),
        store,
        authenticator=Authenticator(),
        origin="http://127.0.0.1",
        workspace=store.operations.workspace,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for_shared_database(app))
    app.origin = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, app.origin
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def request(port, origin, path, body=None, *, headers=None, method="POST"):
    connection = HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        defaults = {"Origin": origin, "Authorization": "deployer", "Content-Type": "application/json"}
        defaults.update(headers or {})
        connection.request(method, path, json.dumps(body) if isinstance(body, dict) else body, defaults)
        response = connection.getresponse()
        content = response.read()
        return response.status, json.loads(content) if response.getheader("Content-Type").startswith(
            "application/json"
        ) else content
    finally:
        connection.close()


def test_http_review_consent_poll_and_browser_page(store, database):
    with http_app(store, database) as (port, origin):
        page_status, page = request(port, origin, "/shared-database", method="GET")
        assert page_status == 200 and b"confirm_allocation:true" in page
        status, review = request(
            port, origin, f"/api/jobs/{JOB}/shared-database/review", {"connection_limit": 5}
        )
        assert status == 201 and counts(store, database) == [0, 0, 0]
        route = "/api/shared-database-reviews/" + review["id"]
        body = {"request_key": "one", "confirm_allocation": True}
        status, accepted = request(port, origin, route + "/submit", body)
        assert status == 202 and accepted["deployment_ready"] is False
        assert request(port, origin, route + "/submit", body) == (202, accepted)
        assert request(port, origin, route, method="GET")[1]["status"] == "queued"
        assert request(port, origin, route + "/revoke", {})[0] == 409
        assert request(port, origin, "/api/config", method="GET")[1]["shared_database_review"] is True
        assert request(port, origin, "/api/deployment-approvals/" + review["id"] + "/submit", body)[0] == 404
    assert counts(store, database) == [1, 1, 1]


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"Authorization": "unknown"}, 403),
        ({"Authorization": "viewer"}, 403),
        ({"Authorization": "foreign"}, 404),
        ({"Origin": "https://other.example"}, 403),
        ({"X-Sky-Token": "legacy"}, 403),
        ({"Content-Type": "text/plain"}, 400),
    ],
)
def test_http_invalid_identity_origin_or_framing_has_no_admission(store, database, headers, status):
    with http_app(store, database) as (port, origin):
        assert (
            request(
                port,
                origin,
                f"/api/jobs/{JOB}/shared-database/review",
                {"connection_limit": 5},
                headers=headers,
            )[0]
            == status
        )
    assert counts(store, database) == [0, 0, 0]


@pytest.mark.parametrize(
    "body",
    [
        '{"connection_limit":true}',
        '{"connection_limit":0}',
        '{"connection_limit":51}',
        '{"connection_limit":5,"connection_limit":6}',
        '{"connection_limit":NaN}',
        '{"connection_limit":5,"organization_id":"other"}',
        "[]",
        '"' + "x" * 1100 + '"',
    ],
)
def test_http_review_does_not_trust_caller_identity_or_duplicate_fields(store, database, body):
    with http_app(store, database) as (port, origin):
        assert request(port, origin, f"/api/jobs/{JOB}/shared-database/review", body)[0] == 400
    assert counts(store, database) == [0, 0, 0]


@pytest.mark.parametrize(
    "body",
    [
        {"request_key": "one"},
        {"request_key": "one", "confirm_allocation": False},
        {"request_key": "one", "confirm_allocation": 1},
        {"request_key": "one", "confirm_allocation": True, "selection": {}},
    ],
)
def test_http_submit_requires_explicit_consent_for_stored_choice(store, database, body):
    review = store.review(ACTOR, JOB)
    with http_app(store, database) as (port, origin):
        assert request(port, origin, "/api/shared-database-reviews/" + review.id + "/submit", body)[0] == 400
    assert counts(store, database) == [0, 0, 0]


def test_readiness_failure_is_sanitized_and_no_operation_is_written(store, database):
    with (
        patch.object(store, "check_ready", side_effect=OSError("private password")),
        http_app(store, database) as (port, origin),
    ):
        status, value = request(
            port, origin, f"/api/jobs/{JOB}/shared-database/review", {"connection_limit": 5}
        )
        assert status == 503 and "password" not in str(value)
    assert counts(store, database) == [0, 0, 0]


def test_http_admitted_review_executes_through_worker_and_reports_only_allocation(store, database):
    from dataclasses import asdict
    from unittest.mock import Mock

    from application.shared_database_workflow import SharedDatabaseWorker
    from domain.shared_database import PoolAllocationRequest

    request_value = PoolAllocationRequest(store.pool, ACTOR.organization_id, "game")
    allocator = Mock(
        allocate=Mock(
            return_value={
                "status": "ready",
                "allocation_id": request_value.id,
                "binding": asdict(request_value.binding()),
                "verified_scope": "postgresql_role_and_database_acl",
                "secret_ref": f"arn:aws:secretsmanager:{store.pool.region}:{store.pool.account_id}:secret:sky-pool/{store.pool.id}/{request_value.id}-test01",
                "password": "must-not-appear",
            }
        )
    )
    worker = SharedDatabaseWorker(
        store.operations.records,
        store.operations,
        store.pool,
        store.config_digest,
        allocator,
        lambda *_: ACTOR,
        owner="test-worker",
    )
    with http_app(store, database) as (port, origin):
        _, review = request(port, origin, f"/api/jobs/{JOB}/shared-database/review", {"connection_limit": 5})
        route = "/api/shared-database-reviews/" + review["id"]
        _, receipt = request(
            port, origin, route + "/submit", {"request_key": "worker", "confirm_allocation": True}
        )
        assert worker.execute(receipt["operation_id"], receipt["attempt_id"])["status"] == "succeeded"
        status, projection = request(port, origin, route, method="GET")
    assert status == 200 and projection["allocation_status"] == "allocated"
    assert projection["deployment_ready"] is False and len(projection["remaining_gates"]) == 4
    assert "must-not-appear" not in json.dumps(projection)
    assert "secret_ref" not in json.dumps(projection)
    allocator.allocate.assert_called_once()
    assert store.operations.records.load_job(JOB).record["status"] == "planned"


@pytest.mark.parametrize(
    "extra,status",
    [
        (("Origin", "https://other.example"), 403),
        (("Content-Type", "application/json"), 400),
        (("Content-Length", "22"), 400),
        (("Transfer-Encoding", "chunked"), 400),
    ],
)
def test_http_ambiguous_framing_and_duplicate_origin_are_rejected(store, database, extra, status):
    with http_app(store, database) as (port, origin):
        connection = HTTPConnection("127.0.0.1", port, timeout=10)
        payload = b'{"connection_limit":5}'
        connection.putrequest("POST", f"/api/jobs/{JOB}/shared-database/review")
        for key, value in (
            ("Authorization", "deployer"),
            ("Origin", origin),
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(payload))),
            extra,
        ):
            connection.putheader(key, value)
        connection.endheaders(payload)
        response = connection.getresponse()
        assert response.status == status
        response.read()
        connection.close()
    assert counts(store, database) == [0, 0, 0]


def test_opt_in_migration_command_is_repeatable_without_changing_existing_review(
    store, database, monkeypatch
):
    from interfaces.b_runtime import main

    review = store.review(ACTOR, JOB)
    monkeypatch.setenv("SKY_STATE_WORKSPACE", store.operations.workspace)
    with (
        patch("adapters.state.postgres.PostgresStateSettings.from_environment"),
        patch("adapters.state.postgres.RotatingDatabaseConnection", return_value=database),
    ):
        main(["migrate", "--shared-database-reviews"])
        main(["migrate", "--shared-database-reviews"])
    assert store.detail(ACTOR, review.id)["status"] == "reviewed"
    store.check_ready()
    assert counts(store, database) == [0, 0, 0]
