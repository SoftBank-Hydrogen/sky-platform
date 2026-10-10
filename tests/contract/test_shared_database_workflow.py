"""Real metadata leases + separate real workload DBs; no live AWS mutation."""

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql

from adapters.database.shared_pool import PostgresSharedPool
from adapters.state.operations import PostgresOperationStore
from adapters.state.postgres import PostgresDeploymentRecordStore
from application.shared_database_workflow import SharedDatabaseAdmission, SharedDatabaseWorker
from domain.access import LoginSource, Principal, Role
from domain.shared_database import SharedDatabasePool
from ports.operations import ApplicationBusy
from ports.shared_database import AllocationCredentials


@pytest.fixture
def workflow():
    state_dsn, pool_dsn = (
        os.environ.get(name) for name in ("SKY_TEST_POSTGRES_DSN", "SKY_TEST_POOL_POSTGRES_DSN")
    )
    if not state_dsn or not pool_dsn:
        pytest.skip("Separate disposable metadata and workload databases required")
    state, pool_params = (psycopg.conninfo.conninfo_to_dict(dsn) for dsn in (state_dsn, pool_dsn))
    if any(
        params.get("host") not in {"127.0.0.1", "localhost", "::1"} for params in (state, pool_params)
    ) or state.get("port") == pool_params.get("port"):
        pytest.fail("Separate loopback PostgreSQL servers required")
    connect_state = lambda: psycopg.connect(**state)
    workspace = uuid4().hex
    records = PostgresDeploymentRecordStore(connect_state, workspace=workspace)
    records.initialize()
    operations = PostgresOperationStore(connect_state, workspace=workspace)
    operations.initialize()
    pool = SharedDatabasePool(
        "contract", "111111111111", "ap-northeast-2", "disposable-pool", pool_params["dbname"], 30
    )
    admin = lambda db: psycopg.connect(**{**pool_params, "dbname": db})
    app = lambda db, user, password: psycopg.connect(
        **{**pool_params, "dbname": db, "user": user, "password": password}
    )
    allocator = PostgresSharedPool(pool, admin, app)
    allocator.initialize()
    actor = Principal("user", "team-a", Role.DEPLOYER, LoginSource.EXTERNAL_IDP)
    job = {
        "id": "a" * 16,
        "application_id": "game",
        "organization_id": "team-a",
        "created_by": "creator",
        "target": "aws-ecs-express",
        "status": "planned",
        "source_digest": "a" * 64,
        "application_ir": {"source_revision": "b" * 64},
        "infrastructure_profile": {"database_engines": ["postgresql"], "evidence": ["package.json"]},
        "aws": {"expected_account": pool.account_id, "region": pool.region},
    }
    records.save_job(job["id"], job)
    admission = SharedDatabaseAdmission(records, operations, pool, "c" * 64)
    operation = admission.admit(actor, job["id"], admission.choose(actor, job["id"]), "choice-1")
    calls = []

    class Managed:
        def allocate(self, request):
            calls.append(request.id)
            arn = f"arn:aws:secretsmanager:{pool.region}:{pool.account_id}:secret:sky-pool/{pool.id}/{request.id}-test01"
            return allocator.allocate(request, AllocationCredentials(arn, "disposable-test-password-for-app"))

    worker = SharedDatabaseWorker(
        records, operations, pool, "c" * 64, Managed(), lambda *_: actor, owner="contract-worker"
    )
    yield worker, operation, records, operations, calls, connect_state
    with admin(pool.control_database) as connection:
        connection.autocommit = True
        allocations = connection.execute("SELECT request FROM sky_pool.allocations").fetchall()
        for (request,) in allocations:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(request["database_name"])
                )
            )
        for (request,) in allocations:
            connection.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(request["login_role"]))
            )
        connection.execute("DELETE FROM sky_pool.allocations")


def expire(operations, operation, connect):
    with connect() as connection:
        connection.execute(
            "UPDATE sky_state.operations SET lease_until=clock_timestamp()-interval '1 second' WHERE workspace=%s AND id=%s",
            (operations.workspace, operation.id),
        )
    assert operations.recover_expired() == (operation.id,)
    return operations.get(operation.id)


def test_outbox_identity_is_executable_once_without_claiming_application_success(workflow):
    worker, operation, records, operations, calls, _ = workflow
    deliveries = operations.claim_outbox("publisher", limit=1)
    message = deliveries[0].message()
    assert set(message) == {"version", "workspace", "operation_id", "attempt_id", "application_id"}
    operations.confirm_outbox(deliveries[0])
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: worker.execute(message["operation_id"], message["attempt_id"]), range(8)))
    final = operations.get(operation.id)
    assert final.status == "succeeded" and len(calls) == 1
    assert final.result["deployment_ready"] is False and final.result["deployment_status"] == "not_started"
    assert "disposable-test-password-for-app" not in str(asdict(final))
    assert records.load_job("a" * 16).record["status"] == "planned"
    assert worker.execute(operation.id, operation.attempt_id)["status"] == "succeeded"
    assert len(calls) == 1


def test_allocation_failure_after_creation_keeps_attention_and_app_reservation(workflow):
    worker, operation, _, operations, calls, _ = workflow
    allocate = worker.service.allocator.allocate

    def uncertain(request):
        allocate(request)
        raise OSError("response lost after creation")

    worker.service.allocator.allocate = uncertain
    assert worker.execute(operation.id, operation.attempt_id)["status"] == "needs_attention"
    final = operations.get(operation.id)
    assert final.status == "needs_attention" and final.external_pending
    with pytest.raises(ApplicationBusy):
        operations.admit("game", "deploy", "new", {})
    assert worker.execute(operation.id, operation.attempt_id)["status"] == "needs_attention"
    assert len(calls) == 1


def test_recovery_after_verified_receipt_never_allocates_again(workflow):
    worker, operation, _, operations, calls, connect = workflow
    complete = operations.complete
    operations.complete = lambda *_args, **_kwargs: False
    assert worker.execute(operation.id, operation.attempt_id)["status"] == "lease_lost"
    recovered = expire(operations, operation, connect)
    assert recovered.status == "queued" and recovered.external_receipt is not None
    operations.complete = complete
    assert worker.execute(recovered.id, recovered.attempt_id)["status"] == "succeeded"
    assert len(calls) == 1


def test_source_change_after_admission_is_failed_without_workload_mutation(workflow):
    worker, operation, records, operations, calls, _ = workflow
    snapshot = records.load_job("a" * 16)
    changed = {**snapshot.record, "source_digest": "d" * 64}
    records.save_job("a" * 16, changed, expected_revision=snapshot.revision)
    assert worker.execute(operation.id, operation.attempt_id)["status"] == "failed"
    assert calls == [] and operations.get(operation.id).external_intent is None
