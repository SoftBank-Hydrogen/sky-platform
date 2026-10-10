"""Persisted approval and HTTP intake against disposable PostgreSQL."""

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

psycopg = pytest.importorskip("psycopg")

from adapters.state.deployment_admission import PostgresDeploymentAdmission
from adapters.state.deployment_approvals import PostgresDeploymentApprovals
from adapters.state.deployment_reads import PostgresDeploymentReads
from adapters.state.operations import PostgresOperationStore
from application.deployment_reads import DeploymentReadService
from domain.access import LoginSource, Principal, Role
from interfaces.http.deployment_approvals import DatabaseApprovalApp, handler_for_approvals
from ports.artifacts import SourceArtifact
from ports.deployment_approvals import ApprovalUnavailable
from ports.operations import ApplicationBusy, IdempotencyConflict


@pytest.fixture(scope="module")
def database():
    dsn = os.environ.get("SKY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Disposable PostgreSQL DSN required")
    if psycopg.conninfo.conninfo_to_dict(dsn).get("host") not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("Only loopback databases are accepted")
    connect = lambda: psycopg.connect(dsn, options="-c statement_timeout=30000 -c lock_timeout=10000")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda _: PostgresDeploymentApprovals(
                    PostgresDeploymentAdmission(
                        PostgresOperationStore(connect), account_id="123456789012", region="ap-northeast-2"
                    )
                ).initialize(),
                range(4),
            )
        )
    return connect


@pytest.fixture
def store(database):
    return PostgresDeploymentApprovals(
        PostgresDeploymentAdmission(
            PostgresOperationStore(database, workspace=uuid4().hex),
            account_id="123456789012",
            region="ap-northeast-2",
        )
    )


def principal(org="team", user="alice", role=Role.DEPLOYER):
    return Principal(user, org, role, LoginSource.CORPORATE_SSO)


def artifact(org="team", app="game"):
    return SourceArtifact(org, app, "a" * 32, "prepared", "b" * 64, 10, "c" * 64)


def approve(store, **kwargs):
    return store.approve(principal(), artifact(), {"runtime": "node"}, **kwargs)


def counts(store, database):
    with database() as connection:
        return [
            connection.execute(
                f"SELECT count(*) FROM sky_state.{table} WHERE workspace=%s", (store.operations.workspace,)
            ).fetchone()[0]
            for table in ("operations", "mutation_scopes", "outbox_events", "metadata_records")
        ]


def expire(store, database, identity):
    with database() as connection:
        connection.execute(
            """UPDATE sky_state.deployment_approvals
            SET created_at=clock_timestamp()-interval '2 minutes',expires_at=clock_timestamp()-interval '1 minute'
            WHERE workspace=%s AND id=%s""",
            (store.operations.workspace, identity),
        )


def test_approval_is_persisted_and_consumed_with_matching_deployment(store, database):
    approval = approve(store)
    assert counts(store, database) == [0] * 4
    result = store.submit(principal(), approval.id, "request")
    command = store.operations.get(result.operation_id).command
    assert command["source_ref"] == artifact().record()
    assert command["plan"] == {"runtime": "node"}
    assert command["approval_id"] == approval.id
    assert store.operations.records.load_job(result.job_id).record["approval_id"] == approval.id
    with database() as connection:
        row = connection.execute(
            "SELECT approved_by,operation_id,job_id FROM sky_state.deployment_approvals WHERE workspace=%s AND id=%s",
            (store.operations.workspace, approval.id),
        ).fetchone()
    assert row == ("alice", UUID(result.operation_id), result.job_id)
    assert counts(store, database) == [1] * 4
    store.check_ready()


def test_same_request_concurrent_submit_creates_one_operation(store, database):
    approval = approve(store)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.submit(principal(), approval.id, "request"), range(16)))
    assert len(set(results)) == 1
    assert counts(store, database) == [1] * 4


def test_different_requests_cannot_consume_one_approval_twice(store, database):
    approval = approve(store)

    def submit(index):
        try:
            return store.submit(principal(), approval.id, f"request{index}")
        except IdempotencyConflict:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(8)))
    assert sum(result is not None for result in results) == 1
    assert counts(store, database) == [1] * 4


@pytest.mark.parametrize("state", ["expired", "revoked"])
def test_unavailable_approval_cannot_create_job(store, database, state):
    approval = approve(store)
    if state == "expired":
        expire(store, database, approval.id)
    else:
        assert store.revoke(principal(), approval.id)
        assert not store.revoke(principal(), approval.id)
    with pytest.raises(ApprovalUnavailable):
        store.submit(principal(), approval.id, "request")
    assert counts(store, database) == [0] * 4


def test_consumed_receipt_replays_after_expiry_and_completion(store, database):
    approval = approve(store)
    result = store.submit(principal(), approval.id, "request")
    operation = store.operations.get(result.operation_id)
    lease = store.operations.claim(operation.id, operation.attempt_id, "worker")
    store.operations.complete(lease, {})
    expire(store, database, approval.id)
    assert store.submit(principal(), approval.id, "request") == result
    with pytest.raises(ApprovalUnavailable, match="cancellation"):
        store.revoke(principal(), approval.id)
    assert counts(store, database) == [1, 0, 1, 1]


@pytest.mark.parametrize(
    "who", [principal(org="foreign", role=Role.ADMIN), principal(user="bob"), principal(role=Role.VIEWER)]
)
def test_foreign_user_or_viewer_cannot_submit(store, database, who):
    approval = approve(store)
    with pytest.raises((FileNotFoundError, PermissionError)):
        store.submit(who, approval.id, "request")
    assert counts(store, database) == [0] * 4


def test_same_org_admin_can_revoke_but_cannot_impersonate_approver(store):
    approval = approve(store)
    admin = principal(user="admin", role=Role.ADMIN)
    with pytest.raises(FileNotFoundError):
        store.submit(admin, approval.id, "request")
    assert store.revoke(admin, approval.id)


def test_failed_consumption_rolls_back_entire_admission_and_can_retry(store, database):
    approval = approve(store)
    with patch.object(store, "_consume", side_effect=RuntimeError("injected")), pytest.raises(RuntimeError):
        store.submit(principal(), approval.id, "request")
    assert counts(store, database) == [0] * 4
    store.submit(principal(), approval.id, "request")
    assert counts(store, database) == [1] * 4


def test_expiry_during_admission_rolls_back_all_writes(store, database):
    approval = approve(store)
    original = store._consume

    def expired(connection, identity, key, result):
        connection.execute(
            """UPDATE sky_state.deployment_approvals
            SET created_at=clock_timestamp()-interval '2 minutes',expires_at=clock_timestamp()-interval '1 minute'
            WHERE workspace=%s AND id=%s""",
            (store.operations.workspace, identity),
        )
        return original(connection, identity, key, result)

    with patch.object(store, "_consume", side_effect=expired), pytest.raises(ApprovalUnavailable):
        store.submit(principal(), approval.id, "request")
    assert counts(store, database) == [0] * 4


def test_busy_app_leaves_approval_unconsumed_for_retry(store, database):
    first, second = approve(store), approve(store)
    result = store.submit(principal(), first.id, "first")
    with pytest.raises(ApplicationBusy):
        store.submit(principal(), second.id, "second")
    operation = store.operations.get(result.operation_id)
    lease = store.operations.claim(operation.id, operation.attempt_id, "worker")
    store.operations.complete(lease, {})
    assert store.submit(principal(), second.id, "second") != result


@pytest.mark.parametrize("change", ["account_id", "region"])
def test_execution_configuration_change_requires_new_approval(store, database, change):
    approval = approve(store)
    setattr(store.admission, change, "987654321098" if change == "account_id" else "us-east-1")
    with pytest.raises(ApprovalUnavailable, match="scope"):
        store.submit(principal(), approval.id, "request")
    assert counts(store, database) == [0] * 4


@pytest.mark.parametrize("field,value", [("plan", {"runtime": "changed"}), ("source_digest", "d" * 64)])
def test_corrupt_approval_digest_is_rejected(store, database, field, value):
    approval = approve(store)
    with database() as connection:
        connection.execute(
            f"UPDATE sky_state.deployment_approvals SET {field}=%s WHERE workspace=%s AND id=%s",
            (
                store.operations._json(value) if field == "plan" else value,
                store.operations.workspace,
                approval.id,
            ),
        )
    with pytest.raises(ValueError):
        store.submit(principal(), approval.id, "request")
    assert counts(store, database) == [0] * 4


def test_commit_ack_loss_replays_consumed_receipt(store, database):
    approval = approve(store)
    original = store.operations.records.connection_factory

    @contextmanager
    def lost_reply():
        with original() as connection:
            yield connection
        raise psycopg.OperationalError("lost acknowledgement")

    with patch.object(store.operations.records, "connection_factory", lost_reply), pytest.raises(OSError):
        store.submit(principal(), approval.id, "request")
    assert counts(store, database) == [1] * 4
    store.submit(principal(), approval.id, "request")
    assert counts(store, database) == [1] * 4


def test_submit_and_revoke_race_has_one_consistent_outcome(store, database):
    approval = approve(store)
    barrier = threading.Barrier(2)

    def submit():
        barrier.wait()
        try:
            return store.submit(principal(), approval.id, "request")
        except ApprovalUnavailable:
            return None

    def revoke():
        barrier.wait()
        try:
            return store.revoke(principal(), approval.id)
        except ApprovalUnavailable:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        submitted, revoked = pool.submit(submit), pool.submit(revoke)
        result, was_revoked = submitted.result(), revoked.result()
    assert (result is not None) != was_revoked
    assert counts(store, database) == ([1] * 4 if result else [0] * 4)


@pytest.mark.parametrize("seconds", [0, -1, 86401, True, "900"])
def test_invalid_approval_lifetime_is_rejected(store, seconds):
    with pytest.raises(ValueError):
        approve(store, seconds=seconds)


def test_original_source_cannot_be_approved(store):
    with pytest.raises(ValueError):
        store.approve(principal(), replace(artifact(), kind="original"), {})


class Authenticator:
    def authenticate_request(self, headers):
        users = {
            "Bearer deployer": principal(),
            "Bearer viewer": principal(role=Role.VIEWER),
            "Bearer foreign": principal(org="foreign", role=Role.ADMIN),
        }
        return users.get(headers.get("Authorization"))


@contextmanager
def http_app(store, database):
    app = DatabaseApprovalApp(
        DeploymentReadService(PostgresDeploymentReads(database, workspace=store.operations.workspace)),
        store,
        authenticator=Authenticator(),
        origin="http://127.0.0.1",
        readiness=store.check_ready,
        workspace=store.operations.workspace,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for_approvals(app))
    app.origin = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, app.origin
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(port, origin, approval, *, action="submit", body=None, headers=None, method="POST"):
    connection = HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        defaults = {"Origin": origin, "Authorization": "Bearer deployer", "Content-Type": "application/json"}
        defaults.update(headers or {})
        connection.request(
            method,
            f"/api/deployment-approvals/{approval.id}/{action}",
            body if body is not None else json.dumps({"request_key": "request"}),
            defaults,
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_http_submit_replay_and_read_detail(store, database):
    approval = approve(store)
    with http_app(store, database) as (port, origin):
        status, result = request(port, origin, approval)
        assert status == 202
        assert request(port, origin, approval) == (202, result)
        connection = HTTPConnection("127.0.0.1", port)
        connection.request(
            "GET", "/api/jobs/" + result["job_id"], headers={"Authorization": "Bearer deployer"}
        )
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["operation_id"] == result["operation_id"]
        connection.close()
    assert counts(store, database) == [1] * 4


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"Authorization": "bad"}, 403),
        ({"Authorization": "Bearer viewer"}, 403),
        ({"Authorization": "Bearer foreign"}, 404),
        ({"Origin": "https://evil.example"}, 403),
        ({"Content-Type": "text/plain"}, 400),
        ({"X-Sky-Token": "legacy"}, 403),
    ],
)
def test_http_rejects_unauthorized_or_cross_origin_requests(store, database, headers, status):
    approval = approve(store)
    with http_app(store, database) as (port, origin):
        assert request(port, origin, approval, headers=headers)[0] == status
    assert counts(store, database) == [0] * 4


@pytest.mark.parametrize(
    "body",
    [
        '{"request_key":"one","request_key":"two"}',
        '{"request_key":"request","source_ref":{}}',
        '{"request_key":"request","organization_id":"team"}',
        "[]",
        '{"request_key":true}',
        '{"request_key":""}',
        "invalid",
        '"' + "x" * 1100 + '"',
    ],
)
def test_http_request_cannot_supply_approval_claims_or_unbounded_body(store, database, body):
    approval = approve(store)
    with http_app(store, database) as (port, origin):
        assert request(port, origin, approval, body=body)[0] == 400
    assert counts(store, database) == [0] * 4


def test_http_revoke_and_conflicts(store, database):
    approval = approve(store)
    with http_app(store, database) as (port, origin):
        assert request(port, origin, approval, action="revoke", body="{}")[0] == 200
        assert request(port, origin, approval)[0] == 409
        assert request(port, origin, approval, method="DELETE")[0] == 405
    assert counts(store, database) == [0] * 4


def test_approved_plan_is_detached_from_callers_mutable_data(store):
    plan = {"runtime": "node", "ports": [8080]}
    approval = store.approve(principal(), artifact(), plan)
    plan["ports"].append(9000)
    result = store.submit(principal(), approval.id, "request")
    assert store.operations.get(result.operation_id).command["plan"] == {"runtime": "node", "ports": [8080]}


@pytest.mark.parametrize(
    "origin",
    [
        "https://example.com/path",
        "http://example.com",
        "https://user@example.com",
        "https://example.com?x=1",
        "https://example.com\n",
        None,
    ],
)
def test_http_composition_rejects_unsafe_origin(store, database, origin):
    with pytest.raises(ValueError):
        DatabaseApprovalApp(
            None, store, authenticator=Authenticator(), origin=origin, readiness=store.check_ready
        )


def test_http_composition_requires_hosted_auth_and_readiness(store):
    with pytest.raises(ValueError):
        DatabaseApprovalApp(
            None, store, authenticator=None, origin="https://example.com", readiness=store.check_ready
        )
    with pytest.raises(ValueError):
        DatabaseApprovalApp(
            None, store, authenticator=Authenticator(), origin="https://example.com", readiness=None
        )


def test_http_readiness_failure_is_sanitized_without_admission(store, database):
    approval = approve(store)
    with (
        patch.object(store, "check_ready", side_effect=OSError("database password private")),
        http_app(store, database) as (port, origin),
    ):
        status, payload = request(port, origin, approval)
    assert status == 503
    assert "private" not in json.dumps(payload)
    assert counts(store, database) == [0] * 4


def test_request_key_is_bound_to_exact_approval(store, database):
    first, second = approve(store), approve(store)
    store.submit(principal(), first.id, "request")
    with pytest.raises(IdempotencyConflict):
        store.submit(principal(), second.id, "request")
    assert counts(store, database) == [1] * 4


@pytest.mark.parametrize("number", [1e20, -0.0])
def test_plan_numeric_representation_survives_postgres_roundtrip(store, number):
    approval = store.approve(principal(), artifact(), {"budget": number})
    result = store.submit(principal(), approval.id, "request")
    command = store.operations.get(result.operation_id).command
    assert command["plan"]["budget"] == number
    canonical = command["approved_plan_json"]
    assert canonical == json.dumps({"budget": number}, separators=(",", ":"))
    assert store.admission._digest(json.loads(canonical)) == command["plan_digest"]
