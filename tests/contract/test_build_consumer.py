"""Real PostgreSQL build consumption with isolated queue/GitHub/S3/ECR doubles."""

import os
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")
from adapters.state.build_execution import PostgresBuildExecutionStore
from adapters.state.deployment_admission import PostgresDeploymentAdmission
from adapters.state.deployment_approvals import PostgresDeploymentApprovals
from application.build_consumer import BuildConsumer
from domain.access import LoginSource, Principal, Role
from ports.artifacts import SourceArtifact
from ports.queue import ReceivedOperation
from ports.remote_builds import BuildSettings, digest, request_for


@pytest.fixture(scope="module")
def database():
    dsn = os.environ.get("SKY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Disposable PostgreSQL required")
    if psycopg.conninfo.conninfo_to_dict(dsn).get("host") not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("Only disposable loopback databases accepted")
    return lambda: psycopg.connect(dsn, options="-c statement_timeout=30000 -c lock_timeout=10000")


class Queue:
    def __init__(self, message):
        self.delivery = ReceivedOperation("receipt", message)
        self.deleted, self.extended = [], []

    def receive(self):
        return self.delivery

    def delete(self, delivery):
        self.deleted.append(delivery)

    def extend(self, delivery, *, seconds=300):
        self.extended.append(seconds)


class Protection:
    def __init__(self):
        self.calls = []

    def set(self, enabled):
        self.calls.append(enabled)


class Builder:
    def __init__(self):
        self.requests = []
        self.run = {"run_id": 42, "succeeded": True}
        self.failure = None

    def dispatch(self, request):
        self.requests.append(request)
        if self.failure:
            raise self.failure

    def observe(self, request):
        return self.run


class Objects:
    def __init__(self):
        self.requests = []
        self.change = {}

    def put_request(self, request):
        self.requests.append(request)

    def result(self, request):
        return {
            "version": 1,
            "build_id": request["build_id"],
            "request_digest": digest(request),
            "source_digest": request["source_digest"],
            "plan_digest": request["plan_digest"],
            "repository": request["repository"],
            "workflow_sha": request["workflow_sha"],
            "platform_code_sha": request["platform_code_sha"],
            "run_id": 42,
            "platform": "linux/amd64",
            "image_digest": "sha256:" + "d" * 64,
            "image": f"{request['account_id']}.dkr.ecr.{request['region']}.amazonaws.com/sky-managed@sha256:"
            + "d" * 64,
            **self.change,
        }


class Verifier:
    def verify(self, request, result):
        pass


@pytest.fixture
def setup(database):
    settings = BuildSettings(
        "SoftBank-Hydrogen/sky-builder",
        "main",
        "a" * 40,
        "b" * 40,
        "sky-test-artifacts",
        "123456789012",
        "ap-northeast-2",
    )
    store = PostgresBuildExecutionStore(database, workspace=uuid4().hex)
    approvals = PostgresDeploymentApprovals(
        PostgresDeploymentAdmission(store, account_id=settings.account_id, region=settings.region)
    )
    approvals.initialize()
    actor = Principal("alice", "team", Role.DEPLOYER, LoginSource.CORPORATE_SSO)
    source = SourceArtifact("team", "game", "a" * 32, "prepared", "b" * 64, 100, "c" * 64)
    plan = {
        "target": "aws",
        "replicas": 1,
        "runtime": "nodejs",
        "dockerfile_source": "generated",
        "dockerfile": "FROM node:22\n",
        "source_digest": source.source_digest,
        "start_command": "npm start",
        "port": 8080,
    }
    approval = approvals.approve(actor, source, plan)
    admission = approvals.submit(actor, approval.id, "request1")
    operation = store.get(admission.operation_id)
    queue = Queue(
        {
            "version": 1,
            "workspace": store.workspace,
            "operation_id": operation.id,
            "attempt_id": operation.attempt_id,
            "application_id": operation.application_id,
        }
    )
    builder, objects, protection = Builder(), Objects(), Protection()
    consumer = BuildConsumer(
        store, queue, builder, objects, Verifier(), settings, protection, "worker", poll_seconds=0
    )
    return consumer, operation, database


def job(setup):
    consumer, operation, database = setup
    with database() as connection:
        return connection.execute(
            "SELECT document,revision FROM sky_state.metadata_records WHERE workspace=%s AND kind='job' AND record_id=%s",
            (consumer.store.workspace, operation.command["job_id"]),
        ).fetchone()


def test_consume_build_and_park_without_claiming_deployment_success(setup):
    consumer, operation, _ = setup
    assert consumer.consume_once() == "build_ready"
    current = consumer.store.get(operation.id)
    assert current.status == "needs_attention" and not current.external_pending
    record, revision = job(setup)
    assert record["status"] == "build_ready" and record["deployment_state"] == "awaiting_deployment"
    assert record["result"] is None and "build_result" in record and revision == 4
    assert len(consumer.builder.requests) == len(consumer.queue.deleted) == 1
    assert consumer.protection.calls == [True, False]
    assert consumer.consume_once() == "duplicate"
    assert len(consumer.builder.requests) == 1
    with consumer.store.records._connection() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM sky_state.mutation_scopes WHERE workspace=%s",
                (consumer.store.workspace,),
            ).fetchone()[0]
            == 1
        )


def test_uncertain_dispatch_never_redispatches_and_retains_lock(setup):
    consumer, operation, _ = setup
    consumer.builder.failure = OSError("response lost")
    assert consumer.consume_once() == "needs_attention"
    current = consumer.store.get(operation.id)
    assert current.external_pending and current.status == "needs_attention"
    assert job(setup)[0]["status"] == "needs_attention"
    assert consumer.consume_once() == "duplicate"
    assert len(consumer.builder.requests) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"source_digest": "f" * 64},
        {"run_id": 43},
        {"image": "evil.example/image"},
        {"platform": "linux/arm64"},
    ],
)
def test_result_scope_changes_never_become_build_success(setup, change):
    consumer, operation, _ = setup
    consumer.objects.change = change
    assert consumer.consume_once() == "needs_attention"
    assert consumer.store.get(operation.id).external_pending
    assert "build_result" not in job(setup)[0]


def test_verified_remote_failure_is_terminal_without_deployment(setup):
    consumer, operation, database = setup
    consumer.builder.run["succeeded"] = False
    assert consumer.consume_once() == "failed"
    assert consumer.store.get(operation.id).status == "failed"
    assert job(setup)[0]["status"] == "failed"
    with database() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM sky_state.mutation_scopes WHERE workspace=%s",
                (consumer.store.workspace,),
            ).fetchone()[0]
            == 0
        )


def test_wrong_workspace_or_application_never_claims_or_dispatches(setup):
    consumer, operation, _ = setup
    for change in ({"workspace": "foreign"}, {"application_id": "foreign"}):
        consumer.queue.delivery = replace(
            consumer.queue.delivery, message={**consumer.queue.delivery.message, **change}
        )
        assert consumer.consume_once() == "invalid"
    assert consumer.store.get(operation.id).status == "queued"
    assert not consumer.builder.requests and not consumer.queue.deleted


def test_active_duplicate_is_deferred_not_deleted(setup):
    consumer, operation, _ = setup
    assert consumer.store.claim(operation.id, operation.attempt_id, "other")
    assert consumer.consume_once() == "busy"
    assert not consumer.queue.deleted and not consumer.builder.requests


def test_stale_attempt_is_acknowledged_without_build(setup):
    consumer, _operation, _ = setup
    consumer.queue.delivery = replace(
        consumer.queue.delivery, message={**consumer.queue.delivery.message, "attempt_id": str(uuid4())}
    )
    assert consumer.consume_once() == "duplicate"
    assert not consumer.builder.requests


def test_interrupted_observed_build_resumes_without_second_dispatch(setup):
    consumer, operation, database = setup
    lease = consumer.store.claim(operation.id, operation.attempt_id, "old")
    request = request_for(operation, consumer.store.workspace, consumer.settings)
    checkpoint = {
        "stage": "build_ready",
        "request": request,
        "request_digest": digest(request),
        "verified_run": consumer.builder.run,
        "build_result": consumer.objects.result(request),
    }
    consumer.store.begin_external(lease, {"kind": "github_build"})
    consumer.store.observe_external(lease, consumer.builder.run, checkpoint)
    with database() as connection:
        connection.execute(
            "UPDATE sky_state.operations SET lease_until=clock_timestamp()-interval '1 second' WHERE workspace=%s AND id=%s",
            (consumer.store.workspace, operation.id),
        )
    consumer.store.recover_expired()
    current = consumer.store.get(operation.id)
    consumer.queue.delivery = replace(
        consumer.queue.delivery, message={**consumer.queue.delivery.message, "attempt_id": current.attempt_id}
    )
    assert consumer.consume_once() == "build_ready"
    assert not consumer.builder.requests


def test_expired_lease_cannot_update_job_projection(setup):
    consumer, operation, database = setup
    lease = consumer.store.claim(operation.id, operation.attempt_id, "old")
    before = job(setup)
    with database() as connection:
        connection.execute(
            "UPDATE sky_state.operations SET lease_until=clock_timestamp()-interval '1 second' WHERE workspace=%s AND id=%s",
            (consumer.store.workspace, operation.id),
        )
    assert not consumer.store.build_progress(lease, {}, stage="building")
    assert job(setup) == before


def test_job_projection_failure_rolls_back_operation_and_event(setup):
    consumer, operation, _database = setup
    lease = consumer.store.claim(operation.id, operation.attempt_id, "worker")
    before = consumer.store.get(operation.id)
    with patch.object(consumer.store, "_event", side_effect=ValueError("abort")), pytest.raises(ValueError):
        consumer.store.build_progress(lease, {"stage": "building"}, stage="building")
    assert consumer.store.get(operation.id) == before
    assert job(setup)[1] == 1


def test_consumed_approval_binding_is_rechecked_before_remote_calls(setup):
    consumer, operation, database = setup
    with database() as connection:
        connection.execute(
            "UPDATE sky_state.deployment_approvals SET plan_digest=%s WHERE workspace=%s AND id=%s",
            ("f" * 64, consumer.store.workspace, operation.command["approval_id"]),
        )
    with pytest.raises(ValueError):
        consumer.consume_once()
    assert not consumer.builder.requests


def test_missing_workflow_observation_times_out_without_repeating_dispatch(setup):
    consumer, operation, _ = setup
    consumer.builder.run = None
    consumer.timeout_seconds = 0
    assert consumer.consume_once() == "needs_attention"
    assert consumer.store.get(operation.id).external_pending
    assert len(consumer.builder.requests) == 1
