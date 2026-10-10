"""Real PostgreSQL checks against a disposable local DB, never an AWS account."""

import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")

from adapters.state.postgres import PostgresDeploymentRecordStore
from ports.state import RecordConflict


@pytest.fixture(scope="module")
def database():
    dsn = os.environ.get("SKY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("SKY_TEST_POSTGRES_DSN is required for disposable PostgreSQL tests")
    config = psycopg.conninfo.conninfo_to_dict(dsn)
    if config.get("host") not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("PostgreSQL integration tests accept loopback endpoints only")
    connect = lambda: psycopg.connect(dsn, options="-c statement_timeout=30000 -c lock_timeout=10000")
    # Exercise cold initialization too: the dedicated database starts empty.
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: PostgresDeploymentRecordStore(connect).initialize(), range(4)))
    return connect


@pytest.fixture
def store(database):
    return PostgresDeploymentRecordStore(database, workspace=uuid4().hex)


def test_replicas_initialize_concurrently_without_duplicate_migrations(database):
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: PostgresDeploymentRecordStore(database).initialize(), range(4)))
    with database() as connection:
        assert connection.execute(
            "SELECT version FROM sky_state.schema_versions ORDER BY version"
        ).fetchall() == [(1,), (2,)]


def test_job_health_and_github_survive_new_adapter_and_are_detached(store, database):
    original = {"id": "job1", "job_record_version": 1, "result": {"url": "https://game.example"}}
    store.save_job("job1", original)
    original["result"]["url"] = "changed"
    store.save_health("job1", [{"healthy": False, "reason": "offline"}])
    store.save_github_sources([{"branch": "main", "enabled": True}])
    restarted = PostgresDeploymentRecordStore(database, workspace=store.workspace)
    assert restarted.list_job_ids() == ("job1",)
    assert restarted.load_job("job1").record["result"]["url"] == "https://game.example"
    assert restarted.load_job("job1").modified_at.endswith("+00:00")
    assert restarted.load_health("job1") == [{"healthy": False, "reason": "offline"}]
    assert restarted.load_github_sources() == [{"branch": "main", "enabled": True}]
    snapshot = restarted.load_job("job1").record
    snapshot["result"]["url"] = "mutated"
    assert store.load_job("job1").record["result"]["url"] == "https://game.example"


def test_workspaces_do_not_share_job_or_configuration(store, database):
    other = PostgresDeploymentRecordStore(database, workspace=uuid4().hex)
    store.save_job("shared", {"value": "one"})
    other.save_job("shared", {"value": "two"})
    store.save_github_sources([{"value": "one"}])
    assert other.load_github_sources() is None
    assert store.load_job("shared").record == {"value": "one"}
    assert other.load_job("shared").record == {"value": "two"}


def test_missing_record_semantics(store):
    with pytest.raises(FileNotFoundError):
        store.load_job("missing")
    assert store.load_health("missing") is None
    assert store.load_github_sources() is None


def test_sql_like_document_is_data_and_does_not_execute(store):
    payload = "'); DROP SCHEMA sky_state CASCADE; --"
    store.save_job("quoted", {"name": payload, "unicode": "한글"})
    assert store.load_job("quoted").record == {"name": payload, "unicode": "한글"}
    assert store.list_job_ids() == ("quoted",)


def test_failed_upsert_rolls_back_and_keeps_committed_record(store, database):
    store.save_job("job1", {"value": "committed"})

    # Real server-side transaction failure after the UPDATE has executed.
    class RollbackConnection:
        def __enter__(self):
            self.connection = database()
            return self.connection

        def __exit__(self, kind, value, traceback):
            try:
                self.connection.execute("SELECT 1 / 0")
            finally:
                self.connection.rollback()
                self.connection.close()

    failing = PostgresDeploymentRecordStore(RollbackConnection, workspace=store.workspace)
    with pytest.raises(OSError):
        failing.save_job("job1", {"value": "uncommitted"}, expected_revision=1)
    assert store.load_job("job1").record == {"value": "committed"}
    assert store.load_job("job1").revision == 1


def test_newer_schema_refuses_startup_and_preserves_records(store, database):
    store.save_job("job1", {"value": "retained"})
    with database() as connection:
        connection.execute("INSERT INTO sky_state.schema_versions (version) VALUES (999)")
    try:
        with pytest.raises(ValueError, match="schema version"):
            store.initialize()
        assert store.load_job("job1").record == {"value": "retained"}
    finally:
        with database() as connection:
            connection.execute("DELETE FROM sky_state.schema_versions WHERE version = 999")


def test_stale_snapshot_cannot_overwrite_another_replica(store, database):
    store.save_job("job1", {"events": []})
    other = PostgresDeploymentRecordStore(database, workspace=store.workspace)
    first, stale = store.load_job("job1"), other.load_job("job1")
    assert store.save_job("job1", {"events": ["A"]}, expected_revision=first.revision) == 2
    committed = store.load_job("job1")
    with pytest.raises(RecordConflict):
        other.save_job("job1", {"events": ["B"]}, expected_revision=stale.revision)
    assert store.load_job("job1") == committed


def test_simultaneous_updates_have_exactly_one_winner(store, database):
    from threading import Barrier

    store.save_job("job1", {"winner": None})
    barrier = Barrier(8)

    def update(index):
        replica = PostgresDeploymentRecordStore(database, workspace=store.workspace)
        revision = replica.load_job("job1").revision
        barrier.wait(timeout=10)
        try:
            replica.save_job("job1", {"winner": index}, expected_revision=revision)
            return index
        except RecordConflict:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        winners = [index for index in pool.map(update, range(8)) if index is not None]
    assert len(winners) == 1
    assert store.load_job("job1").record == {"winner": winners[0]}
    assert store.load_job("job1").revision == 2


def test_duplicate_create_and_missing_update_are_conflicts(store):
    store.save_job("job1", {"value": "original"})
    before = store.load_job("job1")
    with pytest.raises(RecordConflict):
        store.save_job("job1", {"value": "overwrite"})
    with pytest.raises(RecordConflict):
        store.save_job("missing", {}, expected_revision=1)
    assert store.load_job("job1") == before
    assert store.list_job_ids() == ("job1",)


@pytest.mark.parametrize("kind", ["health", "github_sources"])
def test_optional_records_also_reject_stale_writes(store, kind):
    if kind == "health":
        load = lambda: store.load_health_record("job1")
        save = lambda value, **kwargs: store.save_health("job1", value, **kwargs)
    else:
        load = store.load_github_sources_record
        save = store.save_github_sources
    assert load() is None
    assert save([{"value": "original"}]) == 1
    stale = load()
    assert save([{"value": "current"}], expected_revision=stale.revision) == 2
    committed = load()
    with pytest.raises(RecordConflict):
        save([{"value": "stale"}], expected_revision=stale.revision)
    with pytest.raises(RecordConflict):
        save([])
    assert load() == committed


def test_version_one_migration_preserves_documents_and_timestamps(database):
    # Isolated schema in a rolled-back transaction: never alter the shared test ledger.
    with database() as connection:
        connection.execute("BEGIN")
        connection.execute("DROP SCHEMA sky_state CASCADE")
        connection.execute("CREATE SCHEMA sky_state")
        connection.execute("CREATE TABLE sky_state.schema_versions (version integer PRIMARY KEY)")
        connection.execute("INSERT INTO sky_state.schema_versions VALUES (1)")
        connection.execute("""CREATE TABLE sky_state.metadata_records (
            workspace text, kind text, record_id text, document jsonb NOT NULL,
            modified_at timestamptz NOT NULL, PRIMARY KEY(workspace,kind,record_id))""")
        connection.execute("""INSERT INTO sky_state.metadata_records VALUES
            ('legacy','job','job1','{"value":"retained"}', '2026-01-01T00:00:00Z')""")

        class BorrowedConnection:
            def __enter__(self):
                return connection

            def __exit__(self, *args):
                pass

        migrated = PostgresDeploymentRecordStore(BorrowedConnection, workspace="legacy")
        migrated.initialize()
        migrated.initialize()
        snapshot = migrated.load_job("job1")
        assert snapshot.record == {"value": "retained"}
        assert snapshot.revision == 1
        assert snapshot.modified_at == "2026-01-01T00:00:00+00:00"
        assert migrated.save_job("job1", {}, expected_revision=1) == 2
        connection.rollback()


def test_concurrent_creates_have_exactly_one_winner(store, database):
    from threading import Barrier

    barrier = Barrier(8)

    def create(index):
        replica = PostgresDeploymentRecordStore(database, workspace=store.workspace)
        barrier.wait(timeout=10)
        try:
            replica.save_job("job1", {"winner": index})
            return index
        except RecordConflict:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        winners = [index for index in pool.map(create, range(8)) if index is not None]
    assert len(winners) == 1
    snapshot = store.load_job("job1")
    assert snapshot.record == {"winner": winners[0]}
    assert snapshot.revision == 1


def test_failed_migration_rolls_back_schema_and_ledger(database):
    with database() as connection:
        connection.execute("BEGIN")
        connection.execute("DROP SCHEMA sky_state CASCADE")
        connection.execute("CREATE SCHEMA sky_state")
        connection.execute("CREATE TABLE sky_state.schema_versions (version integer PRIMARY KEY)")
        connection.execute("INSERT INTO sky_state.schema_versions VALUES (1)")
        connection.execute("CREATE TABLE sky_state.metadata_records (revision integer)")
        connection.execute("SAVEPOINT before_migration")

        class BorrowedConnection:
            def __enter__(self):
                return connection

            def __exit__(self, *args):
                pass

        with pytest.raises(OSError):
            PostgresDeploymentRecordStore(BorrowedConnection).initialize()
        connection.execute("ROLLBACK TO SAVEPOINT before_migration")
        assert connection.execute("SELECT version FROM sky_state.schema_versions").fetchall() == [(1,)]
        assert connection.execute("SELECT count(*) FROM sky_state.metadata_records").fetchone() == (0,)
        connection.rollback()
