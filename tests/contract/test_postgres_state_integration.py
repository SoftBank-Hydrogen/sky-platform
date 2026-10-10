"""Real PostgreSQL checks against a disposable local DB, never an AWS account."""

import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")

from adapters.state.postgres import PostgresDeploymentRecordStore


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
        assert connection.execute("SELECT version FROM sky_state.schema_versions").fetchall() == [(1,)]


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
        failing.save_job("job1", {"value": "uncommitted"})
    assert store.load_job("job1").record == {"value": "committed"}


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
