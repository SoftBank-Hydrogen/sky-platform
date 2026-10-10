"""Concurrency and recovery contracts against a disposable PostgreSQL server."""

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")

from adapters.state.operations import PostgresOperationStore
from adapters.state.postgres import PostgresDeploymentRecordStore
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
        list(pool.map(lambda _: PostgresOperationStore(connect).initialize(), range(4)))
    return connect


@pytest.fixture
def store(database):
    return PostgresOperationStore(database, workspace=uuid4().hex)


def admit(store, key="request1", app="game", command=None):
    return store.admit(app, "deploy", key, command if command is not None else {"source_digest": "a" * 64})


def expire(store, lease, database):
    with database() as connection:
        connection.execute(
            """UPDATE sky_state.operations SET lease_until=clock_timestamp()-interval '1 second'
            WHERE workspace=%s AND id=%s""",
            (store.workspace, lease.operation_id),
        )


def count(store, database, table):
    assert table in {"operations", "mutation_scopes", "operation_events", "outbox_events"}
    with database() as connection:
        return connection.execute(
            f"SELECT count(*) FROM sky_state.{table} WHERE workspace=%s", (store.workspace,)
        ).fetchone()[0]


def test_concurrent_identical_requests_create_one_operation_and_outbox(store, database):
    with ThreadPoolExecutor(max_workers=8) as pool:
        operations = list(pool.map(lambda _: admit(store), range(16)))
    assert len({operation.id for operation in operations}) == 1
    assert len({operation.attempt_id for operation in operations}) == 1
    assert [
        count(store, database, table)
        for table in ["operations", "mutation_scopes", "operation_events", "outbox_events"]
    ] == [1, 1, 1, 1]
    command = operations[0].command
    command["source_digest"] = "mutated"
    assert store.get(operations[0].id).command["source_digest"] == "a" * 64


def test_same_key_different_command_or_app_is_conflict(store):
    admit(store)
    with pytest.raises(IdempotencyConflict):
        admit(store, command={"source_digest": "b" * 64})
    with pytest.raises(IdempotencyConflict):
        admit(store, app="other")
    # Key order is not a semantic change.
    operation = admit(store, "ordered", "other", {"a": 1, "b": 2})
    assert admit(store, "ordered", "other", {"b": 2, "a": 1}).id == operation.id


def test_competing_requests_reserve_application_once_without_orphans(store, database):
    def request(index):
        try:
            return admit(store, key=f"request{index}").id
        except ApplicationBusy:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        result = list(pool.map(request, range(8)))
    assert sum(value is not None for value in result) == 1
    assert count(store, database, "operations") == count(store, database, "outbox_events") == 1


def test_outbox_insert_failure_rolls_back_operation_scope_and_event(store, database):
    with patch.object(store, "_outbox", side_effect=psycopg.OperationalError("lost")), pytest.raises(OSError):
        admit(store)
    assert [
        count(store, database, table)
        for table in ["operations", "mutation_scopes", "operation_events", "outbox_events"]
    ] == [0, 0, 0, 0]
    assert admit(store).status == "queued"


def test_duplicate_claim_only_grants_one_worker(store):
    operation = admit(store)
    with ThreadPoolExecutor(max_workers=8) as pool:
        leases = list(
            pool.map(lambda i: store.claim(operation.id, operation.attempt_id, f"worker{i}"), range(8))
        )
    lease = next(lease for lease in leases if lease is not None)
    assert sum(lease is not None for lease in leases) == 1
    assert store.get(operation.id).status == "running"
    assert store.heartbeat(lease)


@pytest.mark.parametrize("succeeded", [True, False])
def test_terminal_result_and_scope_release_are_atomic(store, succeeded):
    operation = admit(store)
    lease = store.claim(operation.id, operation.attempt_id, "worker")
    assert store.complete(lease, {"url": "https://game.example"}, succeeded=succeeded)
    final = store.get(operation.id)
    assert final.status == ("succeeded" if succeeded else "failed")
    assert final.result == {"url": "https://game.example"}
    assert not store.complete(lease, {"url": "overwritten"})
    assert admit(store, key="next").id != operation.id
    assert admit(store).id == operation.id


def test_completion_event_failure_restores_running_job_and_reservation(store):
    operation = admit(store)
    lease = store.claim(operation.id, operation.attempt_id, "worker")
    with (
        patch.object(store, "_event", side_effect=psycopg.OperationalError("commit failure")),
        pytest.raises(OSError),
    ):
        store.complete(lease, {"done": True})
    assert store.get(operation.id).status == "running"
    assert store.get(operation.id).result is None
    with pytest.raises(ApplicationBusy):
        admit(store, key="next")


def test_expired_worker_cannot_renew_or_mutate_new_attempt(store, database):
    operation = admit(store)
    old = store.claim(operation.id, operation.attempt_id, "same-worker")
    assert store.checkpoint(old, {"phase": "source_verified"})
    expire(store, old, database)
    assert not store.heartbeat(old)
    assert store.recover_expired() == (operation.id,)
    recovered = store.get(operation.id)
    assert recovered.status == "queued"
    assert recovered.attempt_id != operation.attempt_id
    assert recovered.checkpoint == {"phase": "source_verified"}
    assert store.claim(operation.id, operation.attempt_id, "stale-message") is None
    new = store.claim(operation.id, recovered.attempt_id, "same-worker")
    assert new.epoch > old.epoch
    assert not store.heartbeat(old)
    assert not store.checkpoint(old, {"phase": "invalid"})
    assert not store.begin_external(old, {"request_key": "invalid"})
    assert not store.observe_external(old, {"seen": True}, {"phase": "invalid"})
    assert not store.complete(old, {"done": True})
    assert not store.interrupt(old, {})
    assert store.complete(new, {"done": True})


def test_uncertain_external_request_blocks_reexecution_and_keeps_scope(store, database):
    operation = admit(store)
    lease = store.claim(operation.id, operation.attempt_id, "worker")
    assert store.begin_external(lease, {"request_key": "stable-aws-token", "stack": "sky-game"})
    with pytest.raises(ValueError, match="Observe"):
        store.complete(lease, {"done": True})
    with pytest.raises(ValueError, match="already"):
        store.begin_external(lease, {"request_key": "other-token"})
    expire(store, lease, database)
    assert store.recover_expired() == (operation.id,)
    blocked = store.get(operation.id)
    assert blocked.status == "needs_attention"
    assert blocked.external_intent["request_key"] == "stable-aws-token"
    assert blocked.external_pending
    assert count(store, database, "outbox_events") == 1
    assert store.claim(operation.id, operation.attempt_id, "new-worker") is None
    with pytest.raises(ApplicationBusy):
        admit(store, key="next")
    assert admit(store, key="other-app", app="other").status == "queued"


def test_verified_external_receipt_and_resume_checkpoint_commit_together(store, database):
    operation = admit(store)
    lease = store.claim(operation.id, operation.attempt_id, "worker")
    assert store.begin_external(lease, {"request_key": "stable-token"})
    assert store.observe_external(lease, {"resource_id": "stack1"}, {"phase": "stack_observed"})
    expire(store, lease, database)
    store.recover_expired()
    resumed = store.get(operation.id)
    assert resumed.status == "queued"
    assert resumed.checkpoint == {"phase": "stack_observed"}
    assert resumed.external_receipt == {"resource_id": "stack1"}
    assert not resumed.external_pending


@pytest.mark.parametrize("external", [True, False])
def test_sigterm_checkpoint_requeues_or_blocks_safely(store, database, external):
    operation = admit(store)
    lease = store.claim(operation.id, operation.attempt_id, "worker")
    if external:
        store.begin_external(lease, {"request_key": "stable-token"})
    assert store.interrupt(lease, {"phase": "interrupted"})
    resumed = store.get(operation.id)
    assert resumed.checkpoint == {"phase": "interrupted"}
    assert resumed.status == ("needs_attention" if external else "queued")
    assert count(store, database, "outbox_events") == (1 if external else 2)
    assert not store.heartbeat(lease)


def test_independent_recovery_scanners_do_not_create_duplicate_attempts(store, database):
    operation = admit(store)
    lease = store.claim(operation.id, operation.attempt_id, "worker")
    expire(store, lease, database)
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(lambda _: store.recover_expired(), range(4)))
    assert sum(len(ids) for ids in result) == 1
    assert count(store, database, "outbox_events") == 2


def test_parallel_outbox_publishers_do_not_share_leases(store):
    for i in range(12):
        admit(store, key=f"request{i}", app=f"game{i}")
    with ThreadPoolExecutor(max_workers=4) as pool:
        groups = list(pool.map(lambda i: store.claim_outbox(f"publisher{i}", limit=3), range(4)))
    deliveries = [delivery for group in groups for delivery in group]
    assert len(deliveries) == len({delivery.id for delivery in deliveries}) == 12
    assert store.claim_outbox("another") == ()
    for delivery in deliveries:
        assert store.confirm_outbox(delivery)
    assert store.claim_outbox("again") == ()


def test_publish_failure_preserves_attempt_id_and_rejects_old_publisher(store):
    operation = admit(store)
    old = store.claim_outbox("old")[0]
    assert old.message() == {
        "version": 1,
        "workspace": store.workspace,
        "operation_id": operation.id,
        "attempt_id": operation.attempt_id,
        "application_id": "game",
    }
    assert store.release_outbox(old, delay=0)
    new = store.claim_outbox("new")[0]
    assert new.id == old.id and new.attempt_id == old.attempt_id and new.epoch > old.epoch
    assert not store.confirm_outbox(old)
    assert not store.release_outbox(old, delay=0)
    assert store.confirm_outbox(new)
    assert not store.confirm_outbox(new)


def test_publisher_crash_can_be_recovered_without_changing_dedup_identity(store, database):
    admit(store)
    old = store.claim_outbox("old")[0]
    with database() as connection:
        connection.execute(
            """UPDATE sky_state.outbox_events
            SET publisher_until=clock_timestamp()-interval '1 second' WHERE workspace=%s AND id=%s""",
            (store.workspace, old.id),
        )
    assert not store.confirm_outbox(old)
    new = store.claim_outbox("new")[0]
    assert new.attempt_id == old.attempt_id
    assert store.confirm_outbox(new)


def test_workspaces_do_not_share_requests_jobs_or_execution_tokens(store, database):
    other = PostgresOperationStore(database, workspace=uuid4().hex)
    operation = admit(store)
    foreign = admit(other)
    assert operation.id != foreign.id
    with pytest.raises(FileNotFoundError):
        other.get(operation.id)
    lease = store.claim(operation.id, operation.attempt_id, "worker")
    assert not other.heartbeat(lease)
    # Even a structurally matching token from another workspace is rejected.
    forged = replace(lease, operation_id=foreign.id, attempt_id=foreign.attempt_id)
    assert not other.complete(forged, {"done": True})
    delivery = store.claim_outbox("publisher")[0]
    assert not other.confirm_outbox(delivery)


def test_events_are_ordered_and_old_metadata_initializer_stays_compatible(store, database):
    operation = admit(store)
    lease = store.claim(operation.id, operation.attempt_id, "worker")
    store.checkpoint(lease, {"phase": "prepared"})
    store.complete(lease, {"done": True})
    with database() as connection:
        assert connection.execute(
            """SELECT sequence,stage FROM sky_state.operation_events
            WHERE workspace=%s AND operation_id=%s ORDER BY sequence""",
            (store.workspace, operation.id),
        ).fetchall() == [(1, "queued"), (2, "claimed"), (3, "checkpoint"), (4, "succeeded")]
    PostgresDeploymentRecordStore(database).initialize()


def test_future_operation_schema_is_rejected_without_losing_state(store, database):
    operation = admit(store)
    with database() as connection:
        connection.execute("INSERT INTO sky_state.operation_schema_versions (version) VALUES (999)")
    try:
        with pytest.raises(ValueError, match="schema version"):
            store.initialize()
        assert store.get(operation.id).command == operation.command
    finally:
        with database() as connection:
            connection.execute("DELETE FROM sky_state.operation_schema_versions WHERE version=999")


@pytest.mark.parametrize(
    "arguments",
    [
        ("bad/app", "deploy", "request", {}),
        ("game", "unknown", "request", {}),
        ("game", [], "request", {}),
        ("game", "deploy", "\n", {}),
        ("game", "deploy", "request", []),
        ("game", "deploy", "request", {"bad": float("nan")}),
        ("game", "deploy", "request", {"large": "x" * 65537}),
    ],
)
def test_invalid_admission_does_not_create_database_state(store, database, arguments):
    with pytest.raises(ValueError):
        store.admit(*arguments)
    assert count(store, database, "operations") == 0


def test_observation_failure_does_not_clear_uncertainty_or_advance_checkpoint(store):
    operation = admit(store)
    lease = store.claim(operation.id, operation.attempt_id, "worker")
    store.begin_external(lease, {"request_key": "stable-token"})
    with patch.object(store, "_event", side_effect=psycopg.OperationalError("lost")), pytest.raises(OSError):
        store.observe_external(lease, {"resource": "stack1"}, {"phase": "stack_observed"})
    state = store.get(operation.id)
    assert state.external_pending
    assert state.external_receipt is None and state.checkpoint == {}


def test_worker_waiting_for_row_lock_cannot_finish_after_lease_expiry(store, database):
    operation = admit(store)
    lease = store.claim(operation.id, operation.attempt_id, "worker", seconds=1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with database() as connection:
            connection.execute(
                "SELECT id FROM sky_state.operations WHERE workspace=%s AND id=%s FOR UPDATE",
                (store.workspace, operation.id),
            )
            pending = pool.submit(store.complete, lease, {"done": True})
            connection.execute("SELECT pg_sleep(1.2)")
        assert pending.result(timeout=5) is False
    assert store.get(operation.id).status == "running"
    assert store.get(operation.id).result is None


def test_scope_and_outbox_reject_cross_application_links(store, database):
    operation = admit(store)
    with pytest.raises(psycopg.errors.ForeignKeyViolation), database() as connection:
        connection.execute(
            """INSERT INTO sky_state.mutation_scopes (workspace,application_id,operation_id)
            VALUES (%s,'other-app',%s)""",
            (store.workspace, operation.id),
        )
    with pytest.raises(psycopg.errors.ForeignKeyViolation), database() as connection:
        connection.execute(
            """INSERT INTO sky_state.outbox_events
            (workspace,id,operation_id,attempt_id,application_id,generation)
            VALUES (%s,%s,%s,%s,'other-app',99)""",
            (store.workspace, str(uuid4()), operation.id, operation.attempt_id),
        )


def test_cold_operation_migration_failure_rolls_back_all_new_tables(database):
    from adapters.state import operations as module

    # A newly created local test database isolates a failed cold migration.
    name = "sky_ops_test_" + uuid4().hex
    with database() as connection:
        connection.autocommit = True
        connection.execute(psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(name)))
    original = psycopg.conninfo.conninfo_to_dict(os.environ["SKY_TEST_POSTGRES_DSN"])
    connect = lambda: psycopg.connect(**{**original, "dbname": name})
    try:
        broken = PostgresOperationStore(connect)
        with patch.object(module, "DDL", (*module.DDL, "SELECT 1/0")), pytest.raises(OSError):
            broken.initialize()
        with connect() as connection:
            assert connection.execute("SELECT to_regclass('sky_state.operations')").fetchone() == (None,)
            assert connection.execute(
                "SELECT to_regclass('sky_state.operation_schema_versions')"
            ).fetchone() == (None,)
        broken.initialize()
        assert broken.admit("game", "deploy", "request", {}).status == "queued"
    finally:
        assert name.startswith("sky_ops_test_") and len(name) == 45
        with database() as connection:
            connection.autocommit = True
            connection.execute(psycopg.sql.SQL("DROP DATABASE {}").format(psycopg.sql.Identifier(name)))


def test_uncertain_fifo_send_republishes_same_attempt_and_cannot_claim_twice(store, database):
    from application.outbox import OutboxPublisher

    admit(store)
    accepted = []
    leases = []

    class Queue:
        def publish(self, delivery):
            # Separate DB reads see a committed publisher claim during remote I/O.
            with database() as connection:
                assert (
                    connection.execute(
                        "SELECT publisher_owner FROM sky_state.outbox_events WHERE workspace=%s AND id=%s",
                        (store.workspace, delivery.id),
                    ).fetchone()[0]
                    == "publisher"
                )
            accepted.append(delivery.message())
            leases.append(store.claim(delivery.operation_id, delivery.attempt_id, "consumer"))
            if len(accepted) == 1:
                raise OSError("Queue accepted it but response was lost")

    publisher = OutboxPublisher(store, Queue(), "publisher", retry_delay=0)
    assert publisher.dispatch_once().deferred == 1
    assert publisher.dispatch_once().confirmed == 1
    assert accepted[0] == accepted[1]
    assert leases[0] is not None and leases[1] is None
    assert store.complete(leases[0], {"done": True})
    assert publisher.dispatch_once().confirmed == 0


def test_database_confirmation_failure_keeps_outbox_recoverable_after_send(store, database):
    from application.outbox import OutboxPublisher

    admit(store)
    accepted = []

    class Queue:
        def publish(self, delivery):
            accepted.append(delivery)

    publisher = OutboxPublisher(store, Queue(), "publisher")
    with (
        patch.object(store, "confirm_outbox", side_effect=OSError("uncertain commit")),
        pytest.raises(OSError),
    ):
        publisher.dispatch_once()
    assert len(accepted) == 1
    with database() as connection:
        connection.execute(
            "UPDATE sky_state.outbox_events SET publisher_until=clock_timestamp()-interval '1 second' WHERE workspace=%s",
            (store.workspace,),
        )
    assert publisher.dispatch_once().confirmed == 1
    assert len(accepted) == 2
    assert accepted[0].attempt_id == accepted[1].attempt_id
