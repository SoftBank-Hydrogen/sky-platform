"""Upload, durable preview, approval and admission with PostgreSQL and fake S3."""

import io
import json
import os
import sqlite3
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")

from adapters.state.deployment_admission import PostgresDeploymentAdmission
from adapters.state.deployment_approvals import PostgresDeploymentApprovals
from adapters.state.deployment_previews import PostgresDeploymentPreviews
from adapters.state.deployment_reads import PostgresDeploymentReads
from adapters.state.operations import PostgresOperationStore
from application.deployment_preparation import DeploymentPreparationService
from application.deployment_reads import DeploymentReadService
from application.source_artifacts import SourceArtifactService
from domain.access import LoginSource, Principal, Role
from interfaces.http.deployment_preparation import DatabasePreparationApp, handler_for_preparation
from ports.deployment_previews import PreviewBusy, PreviewUnavailable
from ports.operations import IdempotencyConflict


@pytest.fixture(scope="module")
def database():
    dsn = os.environ.get("SKY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Disposable PostgreSQL DSN required")
    if psycopg.conninfo.conninfo_to_dict(dsn).get("host") not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("Only loopback databases are accepted")
    connect = lambda: psycopg.connect(dsn, options="-c statement_timeout=30000 -c lock_timeout=10000")

    def initialize(_):
        PostgresDeploymentPreviews(
            PostgresDeploymentApprovals(
                PostgresDeploymentAdmission(
                    PostgresOperationStore(connect), account_id="123456789012", region="ap-northeast-2"
                )
            )
        ).initialize()

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(initialize, range(4)))
    return connect


class Objects:
    def __init__(self):
        self.data = {}

    def put(self, artifact, data):
        if artifact.key in self.data:
            assert self.data[artifact.key] == data
        self.data[artifact.key] = data

    def get(self, artifact):
        return self.data[artifact.key]


def principal(org="team", user="alice", role=Role.DEPLOYER):
    return Principal(user, org, role, LoginSource.CORPORATE_SSO)


@pytest.fixture
def service(database):
    operations = PostgresOperationStore(database, workspace=uuid4().hex)
    previews = PostgresDeploymentPreviews(
        PostgresDeploymentApprovals(
            PostgresDeploymentAdmission(operations, account_id="123456789012", region="ap-northeast-2")
        )
    )
    return DeploymentPreparationService(SourceArtifactService(Objects()), previews)


@pytest.fixture
def upload(tmp_path):
    def archive(sqlite=False):
        files = {
            "package.json": json.dumps(
                {"scripts": {"start": "node server.js"}, "dependencies": {"ws": "^8.22.0"}}
            ),
            "server.js": "require('node:http').createServer((q,r)=>r.end('ok')).listen(process.env.PORT || 8080,'0.0.0.0');",
            "Dockerfile": 'FROM node:22-bookworm-slim\nWORKDIR /app\nCOPY . .\nENV PORT=8080\nUSER node\nEXPOSE 8080\nCMD ["npm","start"]\n',
        }
        if sqlite:
            path = tmp_path / "scores.db"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE IF NOT EXISTS rounds(id INTEGER PRIMARY KEY, winner TEXT NOT NULL)")
                db.executemany("INSERT INTO rounds VALUES(?,?)", [(i, "A") for i in range(1, 14)])
            files["data/scores.db"] = path.read_bytes()
            files["db.js"] = (
                "const {DatabaseSync}=require('node:sqlite'); const db=new DatabaseSync('data/scores.db');"
            )
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as bundle:
            for name, content in files.items():
                bundle.writestr(name, content)
        return output.getvalue()

    return archive


def prepare(service, upload, key="upload1"):
    return service.prepare(principal(), "tug-test", key, upload, port=8080, health_path="/health")


def counts(service, database):
    with database() as connection:
        return [
            connection.execute(
                f"SELECT count(*) FROM sky_state.{table} WHERE workspace=%s",
                (service.previews.operations.workspace,),
            ).fetchone()[0]
            for table in [
                "deployment_previews",
                "deployment_approvals",
                "operations",
                "outbox_events",
                "metadata_records",
            ]
        ]


def test_full_source_preview_approval_admission_and_readback(service, database, upload):
    raw = upload()
    view = prepare(service, raw)
    assert view["status"] == "ready" and view["blockers"] == []
    assert view["plan"]["port"] == 8080 and view["plan"]["health_path"] == "/health"
    assert view["plan"]["replicas"] == 1
    assert counts(service, database) == [1, 0, 0, 0, 0]
    assert prepare(service, raw) == view
    restarted = DeploymentPreparationService(
        service.sources, PostgresDeploymentPreviews(service.previews.approvals)
    )
    assert restarted.detail(principal(), view["id"]) == view
    approval = restarted.approve(principal(), view["id"], view["preview_digest"])
    result = service.previews.approvals.submit(principal(), approval.id, "deployment1")
    reads = DeploymentReadService(
        PostgresDeploymentReads(database, workspace=service.previews.operations.workspace)
    )
    assert reads.detail(principal(), result.job_id)["source_ref"] == view["prepared_ref"]
    assert counts(service, database) == [1, 1, 1, 1, 1]
    assert service.approve(principal(), view["id"], view["preview_digest"]) == approval
    service.previews.check_ready()


def test_sqlite_game_preview_preserves_data_and_blocks_unconverted_deployment(service, database, upload):
    view = prepare(service, upload(sqlite=True))
    assert view["inspection"]["sqlite_conversion"]["row_counts"] == {"rounds": 13}
    assert any(blocker["code"] == "sqlite_conversion_required" for blocker in view["blockers"])
    data = service.sources.store.data[next(key for key in service.sources.store.data if "/prepared/" in key)]
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert "data/scores.db" in z.namelist()
    with pytest.raises(PreviewUnavailable):
        service.approve(principal(), view["id"], view["preview_digest"])
    assert counts(service, database) == [1, 0, 0, 0, 0]


def test_actual_game_zip_when_supplied(service):
    path = os.environ.get("SKY_TEST_GAME_ZIP")
    if not path:
        pytest.skip("Full demo-game ZIP is supplied only for the local pinned-artifact rehearsal")
    view = prepare(service, Path(path).read_bytes())
    assert view["inspection"]["sqlite_conversion"]["row_counts"] == {"rounds": 13}
    assert any(blocker["code"] == "sqlite_conversion_required" for blocker in view["blockers"])
    assert view["plan"]["port"] == 8080 and view["plan"]["health_path"] == "/health"
    with pytest.raises(PreviewUnavailable):
        service.approve(principal(), view["id"], view["preview_digest"])
    print(
        "Full TUG ZIP: durable preview ready; 13 rounds preserved; approval blocked until SQLite/application conversion"
    )


@pytest.mark.parametrize("change", ["bytes", "port", "app"])
def test_request_key_cannot_change_upload_or_options(service, upload, change):
    raw = upload()
    prepare(service, raw)
    args = {"application_id": "tug-test", "port": 8080, "health_path": "/health", "upload": raw}
    args.update(
        {"upload": raw + b"changed"}
        if change == "bytes"
        else {"port": 9000}
        if change == "port"
        else {"application_id": "other-app"}
    )
    with pytest.raises(IdempotencyConflict):
        service.prepare(principal(), request_key="upload1", **args)


def test_failed_upload_releases_lease_and_same_request_can_retry(service, database, upload):
    raw = upload()
    with (
        patch.object(service.sources.store, "put", side_effect=OSError("uncertain S3 response")),
        pytest.raises(OSError),
    ):
        prepare(service, raw)
    assert counts(service, database) == [1, 0, 0, 0, 0]
    assert prepare(service, raw)["status"] == "ready"


def test_preview_approval_link_failure_rolls_back_approval(service, database, upload):
    view = prepare(service, upload())
    original = service.previews.approvals._approve_in_transaction

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected linkage failure")

    with (
        patch.object(service.previews.approvals, "_approve_in_transaction", side_effect=fail),
        pytest.raises(RuntimeError),
    ):
        service.approve(principal(), view["id"], view["preview_digest"])
    assert counts(service, database) == [1, 0, 0, 0, 0]
    assert service.detail(principal(), view["id"])["approval_id"] is None
    service.approve(principal(), view["id"], view["preview_digest"])
    assert counts(service, database) == [1, 1, 0, 0, 0]


def test_simultaneous_approval_creates_one_recoverable_approval(service, database, upload):
    view = prepare(service, upload())
    with ThreadPoolExecutor(max_workers=8) as pool:
        approvals = list(
            pool.map(lambda _: service.approve(principal(), view["id"], view["preview_digest"]), range(12))
        )
    assert len({approval.id for approval in approvals}) == 1
    assert counts(service, database) == [1, 1, 0, 0, 0]


def test_wrong_digest_or_tampered_preview_cannot_be_approved(service, database, upload):
    view = prepare(service, upload())
    with pytest.raises(PreviewUnavailable):
        service.approve(principal(), view["id"], "a" * 64)
    with database() as connection:
        connection.execute(
            "UPDATE sky_state.deployment_previews SET plan=%s WHERE workspace=%s AND id=%s",
            (json.dumps({**view["plan"], "port": 9000}), service.previews.operations.workspace, view["id"]),
        )
    with pytest.raises(PreviewUnavailable):
        service.approve(principal(), view["id"], view["preview_digest"])
    assert counts(service, database) == [1, 0, 0, 0, 0]


@pytest.mark.parametrize("who", [principal(org="foreign", role=Role.ADMIN), principal(user="bob")])
def test_foreign_preview_hidden_even_from_admin(service, upload, who):
    view = prepare(service, upload())
    with pytest.raises(FileNotFoundError):
        service.detail(who, view["id"])
    with pytest.raises(FileNotFoundError):
        service.approve(who, view["id"], view["preview_digest"])


def test_viewer_cannot_upload_or_approve(service, database, upload):
    viewer = principal(role=Role.VIEWER)
    with pytest.raises(PermissionError):
        service.prepare(viewer, "game", "key", upload())
    view = prepare(service, upload())
    assert service.detail(viewer, view["id"])["id"] == view["id"]
    with pytest.raises(PermissionError):
        service.approve(viewer, view["id"], view["preview_digest"])
    assert counts(service, database) == [1, 0, 0, 0, 0]


def test_expired_preparation_owner_cannot_replace_new_owner(service, database):
    previews = service.previews
    lease, view = previews.reserve(
        principal(), "game", "key", "a" * 64, {"port": 8080, "health_path": "/health"}
    )
    with pytest.raises(PreviewBusy):
        previews.reserve(principal(), "game", "key", "a" * 64, view["options"])
    with database() as connection:
        connection.execute(
            "UPDATE sky_state.deployment_previews SET lease_until=clock_timestamp()-interval '1 second' WHERE workspace=%s AND id=%s",
            (previews.operations.workspace, lease.id),
        )
    replacement, _ = previews.reserve(principal(), "game", "key", "a" * 64, view["options"])
    previews.release(lease)
    with pytest.raises(PreviewBusy):
        previews.reserve(principal(), "game", "key", "a" * 64, view["options"])
    assert replacement.epoch == lease.epoch + 1


def test_upload_preview_is_recoverable_after_commit_ack_loss(service, database, upload):
    view = prepare(service, upload())
    original = service.previews.operations.records.connection_factory

    @contextmanager
    def lost_reply():
        with original() as connection:
            yield connection
        raise psycopg.OperationalError("lost acknowledgement")

    with (
        patch.object(service.previews.operations.records, "connection_factory", lost_reply),
        pytest.raises(OSError),
    ):
        service.approve(principal(), view["id"], view["preview_digest"])
    receipt = service.approve(principal(), view["id"], view["preview_digest"])
    assert receipt.id == service.detail(principal(), view["id"])["approval_id"]
    assert counts(service, database) == [1, 1, 0, 0, 0]


class Authenticator:
    def authenticate_request(self, headers):
        return {
            "Bearer deployer": principal(),
            "Bearer viewer": principal(role=Role.VIEWER),
            "Bearer foreign": principal(org="foreign", role=Role.ADMIN),
        }.get(headers.get("Authorization"))


@contextmanager
def http(service, database):
    app = DatabasePreparationApp(
        DeploymentReadService(
            PostgresDeploymentReads(database, workspace=service.previews.operations.workspace)
        ),
        service.previews.approvals,
        service,
        authenticator=Authenticator(),
        origin="http://127.0.0.1",
        workspace=service.previews.operations.workspace,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for_preparation(app))
    app.origin = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, app.origin, app
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(port, origin, path, body=b"", headers=None, method="POST"):
    connection = HTTPConnection("127.0.0.1", port, timeout=10)
    values = {"Authorization": "Bearer deployer", "Origin": origin, "Content-Type": "application/json"}
    values.update(headers or {})
    try:
        connection.request(method, path, body, values)
        response = connection.getresponse()
        raw = response.read()
        return response.status, json.loads(raw) if response.getheader("Content-Type", "").startswith(
            "application/json"
        ) else raw.decode()
    finally:
        connection.close()


def upload_headers():
    return {
        "Content-Type": "application/zip",
        "X-Application-Id": "tug-test",
        "Idempotency-Key": "upload1",
        "X-Container-Port": "8080",
        "X-Health-Path": "/health",
    }


def test_http_upload_preview_approve_submit_and_read(service, database, upload):
    with http(service, database) as (port, origin, _):
        config = request(port, origin, "/api/config", method="GET")[1]
        assert config["upload_enabled"] and not config["deployment_consumer_enabled"]
        assert request(port, origin, "/ready", method="GET")[1]["mode"] == "deployment_preparation"
        status, view = request(port, origin, "/api/deployment-previews", upload(), upload_headers())
        assert status == 200
        assert request(port, origin, "/api/deployment-previews/" + view["id"], method="GET")[1] == view
        status, approval = request(
            port,
            origin,
            "/api/deployment-previews/" + view["id"] + "/approve",
            json.dumps({"preview_digest": view["preview_digest"]}),
        )
        assert status == 200
        status, receipt = request(
            port,
            origin,
            "/api/deployment-approvals/" + approval["id"] + "/submit",
            json.dumps({"request_key": "deploy1"}),
        )
        assert status == 202
        assert request(port, origin, "/api/jobs/" + receipt["job_id"], method="GET")[0] == 200
        status, page = request(port, origin, "/prepare", method="GET")
        assert status == 200 and "배포 계획 확인" in page and "textContent" in page
    assert counts(service, database) == [1, 1, 1, 1, 1]


def test_http_sqlite_upload_shows_blockers_and_rejects_approval(service, database, upload):
    with http(service, database) as (port, origin, _):
        status, view = request(
            port, origin, "/api/deployment-previews", upload(sqlite=True), upload_headers()
        )
        assert status == 200 and view["inspection"]["sqlite_conversion"]["row_counts"] == {"rounds": 13}
        assert (
            request(
                port,
                origin,
                "/api/deployment-previews/" + view["id"] + "/approve",
                json.dumps({"preview_digest": view["preview_digest"]}),
            )[0]
            == 409
        )
    assert counts(service, database) == [1, 0, 0, 0, 0]


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"Authorization": "bad"}, 403),
        ({"Authorization": "Bearer viewer"}, 403),
        ({"Origin": "https://evil.example"}, 403),
        ({"X-Container-Port": "bad"}, 400),
        ({"X-Application-Id": "../bad"}, 400),
        ({"Content-Type": "text/plain"}, 400),
    ],
)
def test_http_upload_rejects_invalid_inputs_without_writes(service, database, upload, headers, status):
    with http(service, database) as (port, origin, _):
        assert (
            request(port, origin, "/api/deployment-previews", upload(), {**upload_headers(), **headers})[0]
            == status
        )
    assert counts(service, database) == [0] * 5


def test_http_limits_concurrent_preparation(service, database, upload):
    with http(service, database) as (port, origin, app):
        app.upload_slot.acquire()
        try:
            assert request(port, origin, "/api/deployment-previews", upload(), upload_headers())[0] == 429
        finally:
            app.upload_slot.release()
    assert counts(service, database) == [0] * 5


def test_http_client_cannot_replace_plan_during_approval(service, database, upload):
    view = prepare(service, upload())
    with http(service, database) as (port, origin, _):
        assert (
            request(
                port,
                origin,
                "/api/deployment-previews/" + view["id"] + "/approve",
                json.dumps({"preview_digest": view["preview_digest"], "plan": {"port": 9000}}),
            )[0]
            == 400
        )
    assert counts(service, database) == [1, 0, 0, 0, 0]


def test_expired_preview_cannot_be_approved(service, database, upload):
    view = prepare(service, upload())
    with database() as connection:
        connection.execute(
            "UPDATE sky_state.deployment_previews SET expires_at=clock_timestamp()-interval '1 second' WHERE workspace=%s AND id=%s",
            (service.previews.operations.workspace, view["id"]),
        )
    with pytest.raises(PreviewUnavailable):
        service.approve(principal(), view["id"], view["preview_digest"])
    assert counts(service, database) == [1, 0, 0, 0, 0]


def test_preview_expiry_during_approval_rolls_back_approval_link(service, database, upload):
    view = prepare(service, upload())
    original = service.previews.approvals._approve_in_transaction

    def expire_before_link(connection, *args):
        receipt = original(connection, *args)
        connection.execute(
            "UPDATE sky_state.deployment_previews SET expires_at=clock_timestamp()-interval '1 second' WHERE workspace=%s AND id=%s",
            (service.previews.operations.workspace, view["id"]),
        )
        return receipt

    with (
        patch.object(service.previews.approvals, "_approve_in_transaction", side_effect=expire_before_link),
        pytest.raises(PreviewUnavailable),
    ):
        service.approve(principal(), view["id"], view["preview_digest"])
    assert counts(service, database) == [1, 0, 0, 0, 0]


def test_stale_preparation_cannot_finish_or_release_replacement_owner(service, database, upload):
    import hashlib
    from uuid import UUID

    raw = upload()
    replacement = None
    original = service.sources.capture_prepared

    def replace_owner(*args, **kwargs):
        nonlocal replacement
        artifact = original(*args, **kwargs)
        identity = str(UUID(artifact.upload_id))
        with database() as connection:
            connection.execute(
                "UPDATE sky_state.deployment_previews SET lease_until=clock_timestamp()-interval '1 second' WHERE workspace=%s AND id=%s",
                (service.previews.operations.workspace, identity),
            )
        replacement, _ = service.previews.reserve(
            principal(),
            "tug-test",
            "upload1",
            hashlib.sha256(raw).hexdigest(),
            {"port": 8080, "health_path": "/health", "analyzer": "static"},
        )
        return artifact

    with (
        patch.object(service.sources, "capture_prepared", side_effect=replace_owner),
        pytest.raises(PreviewUnavailable),
    ):
        prepare(service, raw)
    with pytest.raises(PreviewBusy):
        prepare(service, raw)
    service.previews.release(replacement)
    assert prepare(service, raw)["status"] == "ready"
