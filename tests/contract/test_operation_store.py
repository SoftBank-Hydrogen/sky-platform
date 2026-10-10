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
            SET available_at=clock_timestamp(),publisher_until=clock_timestamp()-interval '1 second' WHERE workspace=%s AND id=%s""",
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

    store.outbox_retry_base = 0
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

    publisher = OutboxPublisher(store, Queue(), "publisher")
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
            "UPDATE sky_state.outbox_events SET available_at=clock_timestamp(),publisher_until=clock_timestamp()-interval '1 second' WHERE workspace=%s",
            (store.workspace,),
        )
    assert publisher.dispatch_once().confirmed == 1
    assert len(accepted) == 2
    assert accepted[0].attempt_id == accepted[1].attempt_id


def blocked_operation(store):
    operation = admit(store)
    lease = store.claim(operation.id, operation.attempt_id, "worker")
    store.begin_external(lease, {"request_key": "stable-token", "resource": "stack1"})
    store.interrupt(lease, {"phase": "requested"})
    return store.get(operation.id), lease


def resolve(store, snapshot, *, outcome="succeeded", **changes):
    arguments = {
        "operation_id": snapshot.id,
        "attempt_id": snapshot.attempt_id,
        "expected_version": snapshot.row_version,
        "expected_intent": snapshot.external_intent,
        "receipt": {"resource": "stack1", "state": "verified_final"},
        "checkpoint": {"phase": "external_verified"},
        "resolver": "trusted-reconciler",
        "outcome": outcome,
        "result": None if outcome == "resume" else {"confirmed": True},
    }
    arguments.update(changes)
    return store.resolve_attention(**arguments)


@pytest.mark.parametrize("outcome", ["succeeded", "failed"])
def test_verified_resolution_finishes_original_operation_and_releases_scope(store, database, outcome):
    blocked, old = blocked_operation(store)
    assert resolve(store, blocked, outcome=outcome)
    finished = store.get(blocked.id)
    assert finished.status == outcome and not finished.external_pending
    assert finished.result == {"confirmed": True}
    assert finished.checkpoint == {"phase": "external_verified"}
    assert finished.external_receipt["resolution"] == {
        "resolver": "trusted-reconciler",
        "outcome": outcome,
        "attempt_id": blocked.attempt_id,
        "row_version": blocked.row_version,
    }
    assert count(store, database, "mutation_scopes") == 0
    assert count(store, database, "outbox_events") == 1
    assert not store.complete(old, {})
    assert not resolve(store, blocked, outcome=outcome)
    assert admit(store, key="next").status == "queued"


def test_verified_resume_retains_reservation_and_creates_one_new_attempt(store, database):
    blocked, old = blocked_operation(store)
    assert resolve(store, blocked, outcome="resume")
    resumed = store.get(blocked.id)
    assert resumed.status == "queued" and not resumed.external_pending
    assert resumed.attempt_id != blocked.attempt_id
    assert resumed.checkpoint == {"phase": "external_verified"}
    assert resumed.external_receipt["observation"]["resource"] == "stack1"
    assert resumed.result is None
    assert count(store, database, "mutation_scopes") == 1
    assert count(store, database, "outbox_events") == 2
    assert not resolve(store, blocked, outcome="resume")
    assert store.claim(blocked.id, blocked.attempt_id, "old-message") is None
    assert not store.heartbeat(old)
    with pytest.raises(ApplicationBusy):
        admit(store, key="next")
    new = store.claim(resumed.id, resumed.attempt_id, "new-worker")
    assert store.complete(new, {"done": True})
    assert admit(store, key="next").status == "queued"


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_version": 1},
        {"attempt_id": str(uuid4())},
        {"expected_intent": {"request_key": "different-token"}},
    ],
)
def test_wrong_snapshot_cannot_resolve_uncertainty(store, database, changes):
    blocked, _ = blocked_operation(store)
    before = [
        count(store, database, table) for table in ["operation_events", "outbox_events", "mutation_scopes"]
    ]
    assert not resolve(store, blocked, **changes)
    assert store.get(blocked.id) == blocked
    assert before == [
        count(store, database, table) for table in ["operation_events", "outbox_events", "mutation_scopes"]
    ]


def test_competing_resolvers_commit_only_one_resolution(store, database):
    from threading import Barrier

    blocked, _ = blocked_operation(store)
    barrier = Barrier(8)

    def finish(index):
        other = PostgresOperationStore(database, workspace=store.workspace)
        barrier.wait(timeout=10)
        return resolve(other, blocked, outcome="resume" if index % 2 else "succeeded")

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(finish, range(8))) == 1
    current = store.get(blocked.id)
    assert current.status in {"queued", "succeeded"}
    assert count(store, database, "outbox_events") == (2 if current.status == "queued" else 1)
    assert count(store, database, "mutation_scopes") == (1 if current.status == "queued" else 0)


@pytest.mark.parametrize("outcome", ["succeeded", "resume"])
def test_resolution_transaction_failure_preserves_blocked_state(store, database, outcome):
    blocked, _ = blocked_operation(store)
    original = store._event

    def fail_after_effect(connection, identity, stage):
        original(connection, identity, stage)
        if stage in {"succeeded", "requeued"}:
            raise psycopg.OperationalError("lost after final mutation")

    with patch.object(store, "_event", side_effect=fail_after_effect), pytest.raises(OSError):
        resolve(store, blocked, outcome=outcome)
    assert store.get(blocked.id) == blocked
    assert count(store, database, "mutation_scopes") == 1
    assert count(store, database, "outbox_events") == 1
    assert count(store, database, "operation_events") == 4
    assert resolve(store, blocked, outcome=outcome)


@pytest.mark.parametrize(
    "changes",
    [
        {"receipt": {}},
        {"checkpoint": {}},
        {"expected_intent": {}},
        {"outcome": "retry_unknown"},
        {"outcome": []},
        {"expected_version": True},
        {"expected_version": 0},
        {"resolver": "bad/name"},
        {"result": None},
        {"receipt": {"bad": float("nan")}},
        {"outcome": "resume", "result": {}},
    ],
)
def test_invalid_resolution_preserves_uncertainty(store, changes):
    blocked, _ = blocked_operation(store)
    with pytest.raises(ValueError):
        resolve(store, blocked, **changes)
    assert store.get(blocked.id) == blocked


def test_reconciliation_is_workspace_scoped(store, database):
    blocked, _ = blocked_operation(store)
    other = PostgresOperationStore(database, workspace=uuid4().hex)
    with pytest.raises(FileNotFoundError):
        resolve(other, blocked)
    assert store.get(blocked.id) == blocked


def test_running_operation_cannot_be_resolved_outside_its_worker_lease(store):
    operation = admit(store)
    lease = store.claim(operation.id, operation.attempt_id, "worker")
    store.begin_external(lease, {"request_key": "token"})
    running = store.get(operation.id)
    assert not resolve(store, running)
    assert store.get(operation.id) == running


def test_resolution_requires_original_reservation(store, database):
    blocked, _ = blocked_operation(store)
    with database() as connection:
        connection.execute("DELETE FROM sky_state.mutation_scopes WHERE workspace=%s", (store.workspace,))
    with pytest.raises(ValueError, match="reservation"):
        resolve(store, blocked)
    assert store.get(blocked.id) == blocked


def test_old_resolution_cannot_change_a_later_uncertain_attempt(store):
    blocked, _ = blocked_operation(store)
    assert resolve(store, blocked, outcome="resume")
    resumed = store.get(blocked.id)
    lease = store.claim(resumed.id, resumed.attempt_id, "new-worker")
    store.begin_external(lease, {"request_key": "next-step"})
    store.interrupt(lease, {"phase": "next-requested"})
    newer = store.get(blocked.id)
    assert newer.status == "needs_attention"
    assert not resolve(store, blocked)
    assert store.get(blocked.id) == newer


def make_outbox_available(store, database, *, expire_owner=False):
    with database() as connection:
        connection.execute(
            """UPDATE sky_state.outbox_events SET available_at=clock_timestamp(),
            publisher_until=CASE WHEN %s THEN clock_timestamp()-interval '1 second' ELSE publisher_until END
            WHERE workspace=%s""",
            (expire_owner, store.workspace),
        )


def test_persisted_exponential_backoff_and_cap_survive_new_publisher(store, database):
    operation = admit(store)
    for expected_delay in [5, 10, 20, 40, 80, 160, 300]:
        # A differently configured replica must still use the event's original policy.
        replica = PostgresOperationStore(database, workspace=store.workspace, outbox_retry_base=99)
        delivery = replica.claim_outbox("publisher")[0]
        assert replica.release_outbox(delivery)
        with database() as connection:
            delay = connection.execute(
                """SELECT extract(epoch FROM available_at-clock_timestamp())
                FROM sky_state.outbox_events WHERE workspace=%s AND id=%s""",
                (store.workspace, delivery.id),
            ).fetchone()[0]
        assert expected_delay - 2 < delay <= expected_delay
        assert store.claim_outbox("too-early") == ()
        assert delivery.attempt_id == operation.attempt_id
        make_outbox_available(store, database)
    final = store.claim_outbox("last")[0]
    assert store.release_outbox(final)
    failed = store.list_failed_outbox()[0]
    assert failed.publish_attempts == failed.max_attempts == 8
    assert failed.failure_code == "retry_exhausted"
    assert failed.operation_id == operation.id and failed.attempt_id == operation.attempt_id
    assert store.claim_outbox("never-again") == ()
    assert store.get(operation.id).status == "queued"
    assert count(store, database, "mutation_scopes") == 1


def test_lost_send_responses_stop_at_budget_without_marking_running_job_failed(store, database):
    from application.outbox import OutboxPublisher

    store.outbox_max_attempts, store.outbox_retry_base = 3, 0
    operation = admit(store)
    accepted = []

    class Queue:
        def publish(self, delivery):
            accepted.append(delivery.message())
            store.claim(delivery.operation_id, delivery.attempt_id, "consumer")
            raise OSError("accepted but response lost")

    publisher = OutboxPublisher(store, Queue(), "publisher")
    for _ in range(3):
        assert publisher.dispatch_once().deferred == 1
    assert publisher.dispatch_once().deferred == 0
    assert len(accepted) == 3 and all(item == accepted[0] for item in accepted)
    assert store.get(operation.id).status == "running"
    assert store.list_failed_outbox()[0].publish_attempts == 3
    assert count(store, database, "mutation_scopes") == 1


def test_last_allowed_send_can_be_confirmed_successfully(store):
    store.outbox_max_attempts = 1
    admit(store)
    delivery = store.claim_outbox("publisher")[0]
    assert store.confirm_outbox(delivery)
    assert store.list_failed_outbox() == ()
    assert store.claim_outbox("again") == ()


def test_publisher_crashes_consume_budget_and_observe_delay(store, database):
    store.outbox_max_attempts = 2
    admit(store)
    old = store.claim_outbox("old")[0]
    with database() as connection:
        connection.execute(
            """UPDATE sky_state.outbox_events SET publisher_until=clock_timestamp()-interval '1 second'
            WHERE workspace=%s""",
            (store.workspace,),
        )
    assert store.claim_outbox("early") == ()
    make_outbox_available(store, database)
    final = store.claim_outbox("final")[0]
    assert not store.confirm_outbox(old)
    assert store.claim_outbox("while-final-active") == ()
    assert store.list_failed_outbox() == ()
    make_outbox_available(store, database, expire_owner=True)
    assert store.claim_outbox("after-crash") == ()
    assert not store.confirm_outbox(final)
    assert not store.release_outbox(final, delay=0)
    assert store.list_failed_outbox()[0].publish_attempts == 2


def test_failed_outbox_is_workspace_scoped_and_immutable_to_stale_tokens(store, database):
    store.outbox_max_attempts = 1
    admit(store)
    delivery = store.claim_outbox("publisher")[0]
    assert store.release_outbox(delivery)
    before = store.list_failed_outbox()
    assert before[0].failed_at is not None
    other = PostgresOperationStore(database, workspace=uuid4().hex)
    assert other.list_failed_outbox() == ()
    assert not other.confirm_outbox(delivery)
    assert not store.confirm_outbox(delivery)
    assert not store.release_outbox(delivery, delay=0)
    assert store.list_failed_outbox() == before


def test_failed_release_transaction_does_not_lose_last_attempt(store, database):
    store.outbox_max_attempts = 1
    admit(store)
    delivery = store.claim_outbox("publisher")[0]

    class RollbackConnection:
        def __enter__(self):
            self.connection = database()
            return self.connection

        def __exit__(self, *args):
            try:
                self.connection.execute("SELECT 1/0")
            finally:
                self.connection.rollback()
                self.connection.close()

    broken = PostgresOperationStore(RollbackConnection, workspace=store.workspace)
    with pytest.raises(OSError):
        broken.release_outbox(delivery)
    assert store.list_failed_outbox() == ()
    assert store.confirm_outbox(delivery)


@pytest.mark.parametrize(
    "policy",
    [
        {"outbox_max_attempts": True},
        {"outbox_max_attempts": 0},
        {"outbox_max_attempts": 101},
        {"outbox_retry_base": -1},
        {"outbox_retry_cap": 3601},
        {"outbox_retry_base": 10, "outbox_retry_cap": 5},
    ],
)
def test_invalid_outbox_policy_is_rejected(database, policy):
    with pytest.raises(ValueError):
        PostgresOperationStore(database, **policy)


def test_version_one_outbox_migration_preserves_existing_delivery(store, database):
    operation = admit(store)
    delivery = store.claim_outbox("publisher")[0]
    with database() as connection:
        connection.execute("BEGIN")
        connection.execute("ALTER TABLE sky_state.outbox_events DROP CONSTRAINT outbox_failure_consistent")
        connection.execute("""ALTER TABLE sky_state.outbox_events
            DROP COLUMN max_attempts, DROP COLUMN retry_base_seconds, DROP COLUMN retry_cap_seconds,
            DROP COLUMN failed_at, DROP COLUMN failure_code""")
        connection.execute("DELETE FROM sky_state.operation_schema_versions WHERE version=2")

        class BorrowedConnection:
            def __enter__(self):
                return connection

            def __exit__(self, *args):
                pass

        migrated = PostgresOperationStore(BorrowedConnection, workspace=store.workspace)
        migrated.initialize()
        migrated.initialize()
        row = connection.execute(
            """SELECT operation_id,attempt_id,publish_attempts,publisher_epoch,max_attempts,
            retry_base_seconds,retry_cap_seconds,failed_at FROM sky_state.outbox_events
            WHERE workspace=%s AND id=%s""",
            (store.workspace, delivery.id),
        ).fetchone()
        assert tuple(map(str, row[:2])) == (operation.id, operation.attempt_id)
        assert row[2:] == (1, delivery.epoch, 8, 5, 300, None)
        connection.rollback()


def test_competing_publishers_cannot_exceed_last_attempt_budget(store, database):
    store.outbox_max_attempts, store.outbox_retry_base = 1, 0
    admit(store)
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda index: store.claim_outbox(f"publisher{index}"), range(8)))
    deliveries = [item for group in claims for item in group]
    assert len(deliveries) == 1
    make_outbox_available(store, database, expire_owner=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(
            group == () for group in pool.map(lambda index: store.claim_outbox(f"retry{index}"), range(8))
        )
    failures = store.list_failed_outbox()
    assert len(failures) == 1 and failures[0].publish_attempts == 1


def test_failed_operation_migration_rolls_back_policy_columns(store, database):
    with database() as connection:
        connection.execute("BEGIN")
        connection.execute("ALTER TABLE sky_state.outbox_events DROP CONSTRAINT outbox_failure_consistent")
        connection.execute("""ALTER TABLE sky_state.outbox_events
            DROP COLUMN max_attempts, DROP COLUMN retry_base_seconds, DROP COLUMN retry_cap_seconds,
            DROP COLUMN failed_at, DROP COLUMN failure_code""")
        connection.execute("DELETE FROM sky_state.operation_schema_versions WHERE version=2")
        # An unexpected pre-existing column makes migration fail atomically.
        connection.execute("ALTER TABLE sky_state.outbox_events ADD COLUMN failed_at text")
        connection.execute("SAVEPOINT before_migration")

        class BorrowedConnection:
            def __enter__(self):
                return connection

            def __exit__(self, *args):
                pass

        with pytest.raises(OSError):
            PostgresOperationStore(BorrowedConnection).initialize()
        connection.execute("ROLLBACK TO SAVEPOINT before_migration")
        assert connection.execute("SELECT version FROM sky_state.operation_schema_versions").fetchall() == [
            (1,)
        ]
        assert (
            connection.execute("""SELECT count(*) FROM information_schema.columns
            WHERE table_schema='sky_state' AND table_name='outbox_events' AND column_name='max_attempts'""").fetchone()
            == (0,)
        )
        connection.rollback()


def test_outbox_runtime_publishes_real_db_claim_without_running_deployment(store, database, monkeypatch):
    import json
    from unittest.mock import Mock

    from assets import ASSET_ROOT
    from interfaces.b_runtime import main

    operation = admit(store)
    env = {
        "SKY_DATABASE_HOST": "db.example",
        "SKY_DATABASE_NAME": "sky",
        "SKY_DATABASE_SECRET_ARN": "arn:aws:secretsmanager:ap-northeast-2:977889523182:secret:test",
        "SKY_AWS_REGION": "ap-northeast-2",
        "SKY_AWS_ACCOUNT_ID": "977889523182",
        "SKY_DATABASE_SSLROOTCERT": str(ASSET_ROOT / "infra/rds-global-bundle.pem"),
        "SKY_STATE_WORKSPACE": store.workspace,
        "SKY_JOB_QUEUE_URL": "https://sqs.ap-northeast-2.amazonaws.com/977889523182/test.fifo",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    client = Mock()
    with (
        patch("adapters.state.postgres.RotatingDatabaseConnection", return_value=database),
        patch("boto3.client", return_value=client),
        patch(
            "adapters.state.operations.PostgresOperationStore.initialize",
            side_effect=AssertionError("Runtime DDL"),
        ),
    ):
        main(["worker", "--mode", "outbox", "--once"])
    sent = client.send_message.call_args.kwargs
    assert json.loads(sent["MessageBody"])["operation_id"] == operation.id
    assert sent["MessageDeduplicationId"] == operation.attempt_id
    assert store.get(operation.id).status == "queued"
    with database() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM sky_state.outbox_events "
                "WHERE workspace=%s AND published_at IS NOT NULL",
                (store.workspace,),
            ).fetchone()[0]
            == 1
        )
