"""`sky-service migrate` against an empty disposable local database, never an AWS account."""

import os
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")

from adapters.state.readiness import check_database_ready
from interfaces.b_runtime import main, run_migrations


@pytest.fixture
def empty_database():
    dsn = os.environ.get("SKY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("SKY_TEST_POSTGRES_DSN is required for disposable PostgreSQL tests")
    config = psycopg.conninfo.conninfo_to_dict(dsn)
    if config.get("host") not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("PostgreSQL integration tests accept loopback endpoints only")
    # A fresh database proves cold migration even after other suites initialized sky_test.
    name = "sky_migrate_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        try:
            admin.execute(f'CREATE DATABASE "{name}"')
        except psycopg.errors.InsufficientPrivilege:
            pytest.skip("Disposable PostgreSQL user cannot create databases")
    target = psycopg.conninfo.make_conninfo(dsn, dbname=name)
    yield lambda: psycopg.connect(target, options="-c statement_timeout=30000 -c lock_timeout=10000")
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def migrate(connect):
    with (
        patch("adapters.state.postgres.PostgresStateSettings.from_environment", return_value=Mock()),
        patch("adapters.state.postgres.RotatingDatabaseConnection", return_value=connect),
    ):
        main(["migrate"])


def test_migrate_applies_once_then_reports_up_to_date(empty_database, capsys):
    with pytest.raises(OSError):
        check_database_ready(empty_database, operations=True)
    migrate(empty_database)
    assert capsys.readouterr().out.splitlines() == [
        "metadata schema: applied 1,2 (now 1,2)",
        "operation schema: applied 1,2 (now 1,2)",
    ]
    check_database_ready(empty_database, operations=True)
    migrate(empty_database)
    assert capsys.readouterr().out.splitlines() == [
        "metadata schema: up to date (now 1,2)",
        "operation schema: up to date (now 1,2)",
    ]
    check_database_ready(empty_database, operations=True)


def test_concurrent_migrations_serialize_on_advisory_lock(empty_database):
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: run_migrations(empty_database, "team"), range(4)))
    check_database_ready(empty_database, operations=True)
    with empty_database() as connection:
        for table in ("schema_versions", "operation_schema_versions"):
            rows = connection.execute(f"SELECT version FROM sky_state.{table} ORDER BY version").fetchall()
            assert rows == [(1,), (2,)]


def test_unsupported_schema_fails_without_diagnostics_or_changes(empty_database, capsys):
    migrate(empty_database)
    with empty_database() as connection:
        connection.execute("INSERT INTO sky_state.operation_schema_versions (version) VALUES (3)")
    capsys.readouterr()
    with pytest.raises(SystemExit) as error:
        migrate(empty_database)
    assert error.value.code == 1
    output = capsys.readouterr()
    assert (
        output.err == "B state migration failed; check database access, schema version and configuration.\n"
    )
    assert "sky_state" not in output.out + output.err
    with empty_database() as connection:
        rows = connection.execute(
            "SELECT version FROM sky_state.operation_schema_versions ORDER BY version"
        ).fetchall()
    assert rows == [(1,), (2,), (3,)]
