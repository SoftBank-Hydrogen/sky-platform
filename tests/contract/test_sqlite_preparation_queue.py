"""Actual PostgreSQL admission/outbox/ownership with fake SQS and immutable objects."""

import json
import threading
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest

from adapters.state.operations import PostgresOperationStore
from application.outbox import OutboxPublisher
from application.sqlite_preparation import SqliteSourcePreparation
from application.sqlite_preparation_consumer import SqlitePreparationConsumer
from application.sqlite_preparation_jobs import SqlitePreparationJobs
from ports.deployment_previews import PreviewUnavailable
from ports.queue import ReceivedOperation
from tests.contract.test_deployment_preparation import http, prepare, principal, request

pytest_plugins = ["tests.contract.test_deployment_preparation"]


class Queue:
    def __init__(self):
        self.messages, self.deleted, self.extended = [], [], []
        self.renewed = threading.Event()

    def publish(self, delivery):
        self.messages.append(ReceivedOperation(uuid4().hex, delivery.message()))

    def receive(self):
        return self.messages.pop(0) if self.messages else None

    def delete(self, delivery):
        self.deleted.append(delivery)

    def extend(self, delivery, *, seconds):
        self.extended.append(seconds)
        self.renewed.set()


@pytest.fixture
def pipeline(service, database, upload):
    store = PostgresOperationStore(database, workspace="prepare_" + uuid4().hex)
    jobs = SqlitePreparationJobs(service.previews, store)
    view = prepare(service, upload(sqlite=True))
    queue, protection = Queue(), Mock()
    stage = SqliteSourcePreparation(service.sources)
    consumer = SqlitePreparationConsumer(
        store, queue, stage, protection, "test-worker", heartbeat_seconds=0.01
    )
    publisher = OutboxPublisher(store, queue, "publisher")
    return jobs, view, queue, consumer, publisher


def submit(pipeline):
    jobs, view, _, _, _ = pipeline
    return jobs.submit(principal(), view["id"], view["preview_digest"], "prepare1")


def test_api_admission_outbox_consumption_preserves_original_and_requires_new_review(
    pipeline, service, database
):
    jobs, view, queue, consumer, publisher = pipeline
    first = submit(pipeline)
    assert submit(pipeline) == first
    assert publisher.dispatch_once().confirmed == 1
    delivery = queue.messages[0]
    assert set(delivery.message) == {"version", "workspace", "operation_id", "attempt_id", "application_id"}
    assert consumer.consume_once() == "prepared"
    final = jobs.detail(principal(), first["id"])
    assert final["status"] == "succeeded"
    assert final["result"]["status"] == "application_conversion_required"
    assert final["result"]["conversion"]["row_counts"] == {"rounds": 13}
    assert final["result"]["requires_new_review"] is True
    assert service.detail(principal(), view["id"]) == view
    with pytest.raises(PreviewUnavailable):
        service.approve(principal(), view["id"], view["preview_digest"])
    queue.messages.append(delivery)
    with patch.object(consumer.preparation, "prepare", side_effect=AssertionError("Duplicate executed")):
        assert consumer.consume_once() == "duplicate"
    assert jobs.operations.admit("tug-test", "prepare_sqlite", "next", {}).status == "queued"


@pytest.mark.parametrize("change", ["digest", "expiry", "source"])
def test_changed_or_expired_preview_never_admitted(pipeline, change):
    jobs, view, _, _, _ = pipeline
    edited = json.loads(json.dumps(view))
    if change == "digest":
        edited["preview_digest"] = "0" * 64
    elif change == "expiry":
        edited["expires_at"] = "2000-01-01T00:00:00+00:00"
    else:
        edited["source_ref"]["source_digest"] = "0" * 64
    with patch.object(jobs.previews, "get", return_value=edited), pytest.raises(PreviewUnavailable):
        submit(pipeline)
    assert jobs.operations.claim_outbox("publisher") == ()


def test_viewer_foreign_owner_and_same_workspace_denied(pipeline):
    from domain.access import Role

    jobs, view, _, _, _ = pipeline
    with pytest.raises(PermissionError):
        jobs.submit(principal(role=Role.VIEWER), view["id"], view["preview_digest"], "key")
    with pytest.raises(FileNotFoundError):
        jobs.submit(principal(org="other"), view["id"], view["preview_digest"], "key")
    receipt = submit(pipeline)
    with pytest.raises(FileNotFoundError):
        jobs.detail(principal(user="other"), receipt["id"])
    with pytest.raises(ValueError, match="separate"):
        SqlitePreparationJobs(jobs.previews, jobs.previews.operations)


def test_lost_s3_ack_replays_identical_source_without_second_operation(pipeline):
    jobs, _, _queue, consumer, publisher = pipeline
    receipt = submit(pipeline)
    publisher.dispatch_once()
    real = consumer.preparation.prepare
    observed = []

    def lost_ack(*args, **kwargs):
        observed.append(real(*args, **kwargs))
        raise OSError("S3 acknowledgement lost")

    with patch.object(consumer.preparation, "prepare", side_effect=lost_ack):
        assert consumer.consume_once() == "retry"
    assert jobs.operations.get(receipt["id"]).status == "queued"
    publisher.dispatch_once()
    assert consumer.consume_once() == "prepared"
    assert jobs.detail(principal(), receipt["id"])["result"] == observed[0]


def test_three_transient_failures_stop_retrying(pipeline):
    jobs, _, _, consumer, publisher = pipeline
    receipt = submit(pipeline)
    with patch.object(consumer.preparation, "prepare", side_effect=OSError("unavailable")):
        for expected in ["retry", "retry", "failed"]:
            assert publisher.dispatch_once().confirmed == 1
            assert consumer.consume_once() == expected
    result = jobs.detail(principal(), receipt["id"])
    assert result["status"] == "failed" and result["result"]["reason"] == "preparation_retry_exhausted"
    assert publisher.dispatch_once().confirmed == 0


def test_owned_checkpoint_survives_worker_loss_and_requeues(pipeline, database):
    jobs, _, queue, consumer, publisher = pipeline
    receipt = submit(pipeline)
    publisher.dispatch_once()
    with patch.object(jobs.operations, "heartbeat", return_value=False):
        assert consumer.consume_once() == "ownership_lost"
    assert queue.deleted == []
    with database() as connection:
        connection.execute(
            "UPDATE sky_state.operations SET lease_until=clock_timestamp()-interval '1 second' WHERE workspace=%s AND id=%s",
            (jobs.operations.workspace, receipt["id"]),
        )
    assert jobs.operations.recover_expired() == (receipt["id"],)
    publisher.dispatch_once()
    assert consumer.consume_once() == "prepared"


def test_heartbeat_extends_queue_and_stop_requeues_without_completing(pipeline):
    jobs, _, queue, consumer, publisher = pipeline
    receipt = submit(pipeline)
    publisher.dispatch_once()
    stop = threading.Event()
    real = consumer.preparation.prepare

    def stop_after_preparation(*args, **kwargs):
        assert queue.renewed.wait(3)
        result = real(*args, **kwargs)
        stop.set()
        return result

    with patch.object(consumer.preparation, "prepare", side_effect=stop_after_preparation):
        assert consumer.consume_once(stop) == "stopped"
    assert 300 in queue.extended
    assert jobs.operations.get(receipt["id"]).status == "queued"
    publisher.dispatch_once()
    assert consumer.consume_once() == "prepared"
    consumer.protection.set.assert_called_with(False)


def test_foreign_queue_scope_not_executed_or_deleted(pipeline):
    _, _, queue, consumer, publisher = pipeline
    submit(pipeline)
    publisher.dispatch_once()
    queue.messages[0].message["workspace"] = "foreign"
    assert consumer.consume_once() == "invalid"
    assert queue.deleted == [] and queue.extended == [60]


def test_invalid_review_fails_without_exposing_details(pipeline):
    jobs, _, _, consumer, publisher = pipeline
    receipt = submit(pipeline)
    publisher.dispatch_once()
    with patch.object(consumer.preparation, "prepare", side_effect=ValueError("private data")):
        assert consumer.consume_once() == "failed"
    result = jobs.detail(principal(), receipt["id"])
    assert result["status"] == "failed" and "private" not in str(result)


def test_http_opt_in_submit_and_result_polling(pipeline, service, database):
    jobs, view, _, consumer, publisher = pipeline
    path = "/api/deployment-previews/" + view["id"] + "/prepare-sqlite"
    body = json.dumps({"preview_digest": view["preview_digest"]})
    with http(service, database) as (port, origin, app):
        assert request(port, origin, path, body, {"Idempotency-Key": "key"})[0] == 503
        app.sqlite_jobs = jobs
        status, receipt = request(port, origin, path, body, {"Idempotency-Key": "key"})
        assert status == 202
        publisher.dispatch_once()
        assert consumer.consume_once() == "prepared"
        status, detail = request(port, origin, "/api/sqlite-preparation-jobs/" + receipt["id"], method="GET")
        assert status == 200 and detail["result"]["requires_new_review"] is True
        assert (
            request(port, origin, path, body, {"Idempotency-Key": "key", "Origin": "https://evil.test"})[0]
            == 403
        )
        assert (
            request(port, origin, path, body, {"Idempotency-Key": "key", "Authorization": "Bearer viewer"})[0]
            == 403
        )
        assert request(port, origin, path, body)[0] == 400


def test_completion_commit_ack_loss_is_not_reexecuted(pipeline):
    jobs, _, queue, consumer, publisher = pipeline
    receipt = submit(pipeline)
    publisher.dispatch_once()
    delivery = queue.messages[0]
    complete = jobs.operations.complete

    def lost_commit_ack(*args, **kwargs):
        assert complete(*args, **kwargs)
        raise OSError("Commit acknowledgement lost")

    with patch.object(jobs.operations, "complete", side_effect=lost_commit_ack):
        assert consumer.consume_once() == "ownership_lost"
    assert jobs.detail(principal(), receipt["id"])["status"] == "succeeded"
    assert queue.deleted == []
    queue.messages.append(delivery)
    with patch.object(consumer.preparation, "prepare", side_effect=AssertionError("Already complete")):
        assert consumer.consume_once() == "duplicate"


def test_saved_prepared_result_requires_identical_fresh_artifact(pipeline):
    from ports.remote_builds import digest

    jobs, _, _, consumer, publisher = pipeline
    receipt = submit(pipeline)
    operation = jobs.operations.get(receipt["id"])
    lease = jobs.operations.claim(operation.id, operation.attempt_id, "dead-worker")
    assert jobs.operations.interrupt(
        lease,
        {
            "stage": "sqlite_prepared",
            "command_digest": digest(operation.command),
            "retry_count": 0,
            "result": {"forged": True},
        },
    )
    publisher.dispatch_once()
    # A published old attempt is discarded before the recovered attempt runs.
    assert consumer.consume_once() == "duplicate"
    assert consumer.consume_once() == "failed"
    assert jobs.detail(principal(), receipt["id"])["result"]["reason"] == "invalid_source_or_review"


def test_actual_tug_zip_through_preparation_job_when_supplied(pipeline, service):
    import os
    from pathlib import Path

    from ports.artifacts import SourceArtifact

    path = os.environ.get("SKY_TEST_GAME_ZIP")
    if not path:
        pytest.skip("Pinned demo-game ZIP supplied only for local rehearsal")
    jobs, _, _, consumer, publisher = pipeline
    view = prepare(service, Path(path).read_bytes(), key="actual-tug")
    receipt = jobs.submit(principal(), view["id"], view["preview_digest"], "actual-tug-preparation")
    assert publisher.dispatch_once().confirmed == 1
    assert consumer.consume_once() == "prepared"
    result = jobs.detail(principal(), receipt["id"])["result"]
    assert result["conversion"]["row_counts"] == {"rounds": 13}
    assert result["requires_new_review"] is True
    original = SourceArtifact.from_record(result["original_ref"])
    prepared = SourceArtifact.from_record(result["prepared_ref"])
    with service.sources.restore(principal(), original) as project:
        original_db = (project / "data/scores.db").read_bytes()
        original_code = (project / "db.js").read_bytes()
    with service.sources.restore(principal(), prepared) as project:
        assert not (project / "data/scores.db").exists()
        assert (project / "db.js").read_bytes() == original_code
        assert (project / "migrations/0000_sky_sqlite_import.sql").is_file()
    assert original_db.startswith(b"SQLite format 3")
    with pytest.raises(PreviewUnavailable):
        service.approve(principal(), view["id"], view["preview_digest"])


def test_actual_tug_postgres_profile_through_http_when_supplied(pipeline, service, database):
    import os
    from pathlib import Path

    from ports.artifacts import SourceArtifact

    path = os.environ.get("SKY_TEST_GAME_ZIP")
    if not path:
        pytest.skip("Pinned demo-game ZIP supplied only for local rehearsal")
    jobs, _, _, consumer, publisher = pipeline
    view = prepare(service, Path(path).read_bytes(), key="actual-tug-pg")
    with http(service, database) as (port, origin, app):
        app.sqlite_jobs = jobs
        status, receipt = request(
            port,
            origin,
            "/api/deployment-previews/" + view["id"] + "/prepare-sqlite",
            json.dumps({"preview_digest": view["preview_digest"], "conversion_profile": "tug-postgres-v1"}),
            {"Idempotency-Key": "tug-pg"},
        )
        assert status == 202
    assert publisher.dispatch_once().confirmed == 1
    assert consumer.consume_once() == "prepared"
    result = jobs.detail(principal(), receipt["id"])["result"]
    assert result["status"] == "database_binding_required"
    assert result["transformation"]["profile"] == "tug-postgres-v1"
    prepared = SourceArtifact.from_record(result["prepared_ref"])
    with service.sources.restore(principal(), prepared) as project:
        assert "await scores.saveRound" in (project / "server.js").read_text()
        assert "rejectUnauthorized: true" in (project / "db.js").read_text()
        assert (project / "rds-global-bundle.pem").is_file()
        assert (
            json.loads((project / "package-lock.json").read_text())["packages"]["node_modules/pg"]["version"]
            == "8.23.0"
        )
    with pytest.raises(PreviewUnavailable):
        service.approve(principal(), view["id"], view["preview_digest"])


def test_tug_profile_rejects_unreviewed_source_before_admission(pipeline):
    jobs, view, _, _, publisher = pipeline
    with pytest.raises(ValueError, match="Unsupported TUG"):
        jobs.submit(
            principal(), view["id"], view["preview_digest"], "wrong-tug", conversion_profile="tug-postgres-v1"
        )
    assert publisher.dispatch_once().confirmed == 0


@pytest.mark.parametrize("profile", [None, [], {}, "unsupported"])
def test_invalid_profile_returns_400_without_queued_work(pipeline, service, database, profile):
    jobs, view, _, _, publisher = pipeline
    with http(service, database) as (port, origin, app):
        app.sqlite_jobs = jobs
        assert (
            request(
                port,
                origin,
                "/api/deployment-previews/" + view["id"] + "/prepare-sqlite",
                json.dumps({"preview_digest": view["preview_digest"], "conversion_profile": profile}),
                {"Idempotency-Key": "invalid-profile"},
            )[0]
            == 400
        )
    assert publisher.dispatch_once().confirmed == 0
