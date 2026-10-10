"""`sky-service migrate` against an empty disposable local database, never an AWS account."""

import os
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")

from adapters.state.readiness import check_database_ready
from interfaces.b_runtime import main, migration_steps, run_migrations

ACCOUNT = "977889523182"
REGION = "ap-northeast-2"
LEDGERS = (
    ("metadata", "schema_versions", "1,2"),
    ("operation", "operation_schema_versions", "1,2"),
    ("admission", "admission_schema_versions", "1"),
    ("approval", "approval_schema_versions", "1"),
    ("preview", "preview_schema_versions", "1"),
)


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
        patch.dict(os.environ, {"SKY_AWS_ACCOUNT_ID": ACCOUNT}),
        patch(
            "adapters.state.postgres.PostgresStateSettings.from_environment",
            return_value=Mock(region=REGION),
        ),
        patch("adapters.state.postgres.RotatingDatabaseConnection", return_value=connect),
    ):
        main(["migrate"])


def preparation_ready(connect):
    """The readiness the opt-in preparation API runs at startup (read-only, never migrates)."""
    previews = migration_steps(connect, "team", account_id=ACCOUNT, region=REGION)[-1]
    previews.check_ready()


def ledger_rows(connect):
    with connect() as connection:
        return {
            table: [
                row[0]
                for row in connection.execute(f"SELECT version FROM sky_state.{table} ORDER BY version")
            ]
            for _, table, _ in LEDGERS
        }


def test_migrate_applies_once_then_reports_up_to_date(empty_database, capsys):
    with pytest.raises(OSError):
        check_database_ready(empty_database, operations=True)
    with pytest.raises(OSError):
        preparation_ready(empty_database)
    migrate(empty_database)
    assert capsys.readouterr().out.splitlines() == [
        f"{name} schema: applied {now} (now {now})" for name, _, now in LEDGERS
    ]
    check_database_ready(empty_database, operations=True)
    preparation_ready(empty_database)
    migrate(empty_database)
    assert capsys.readouterr().out.splitlines() == [
        f"{name} schema: up to date (now {now})" for name, _, now in LEDGERS
    ]
    check_database_ready(empty_database, operations=True)
    preparation_ready(empty_database)


def test_migrate_continues_from_existing_foundation(empty_database, capsys):
    """A database migrated by the earlier metadata/operation-only command gains the other three."""
    steps = migration_steps(empty_database, "team", account_id=ACCOUNT, region=REGION)
    steps[1].initialize()
    migrate(empty_database)
    assert capsys.readouterr().out.splitlines() == [
        "metadata schema: up to date (now 1,2)",
        "operation schema: up to date (now 1,2)",
        "admission schema: applied 1 (now 1)",
        "approval schema: applied 1 (now 1)",
        "preview schema: applied 1 (now 1)",
    ]
    preparation_ready(empty_database)


def test_concurrent_migrations_serialize_on_advisory_lock(empty_database):
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda _: run_migrations(empty_database, "team", account_id=ACCOUNT, region=REGION), range(4)
            )
        )
    check_database_ready(empty_database, operations=True)
    preparation_ready(empty_database)
    assert ledger_rows(empty_database) == {
        table: [int(version) for version in now.split(",")] for _, table, now in LEDGERS
    }


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


def test_unsupported_preview_schema_fails_without_diagnostics_or_changes(empty_database, capsys):
    migrate(empty_database)
    with empty_database() as connection:
        connection.execute("INSERT INTO sky_state.preview_schema_versions (version) VALUES (2)")
    before = ledger_rows(empty_database)
    capsys.readouterr()
    with pytest.raises(SystemExit) as error:
        migrate(empty_database)
    assert error.value.code == 1
    output = capsys.readouterr()
    assert (
        output.err == "B state migration failed; check database access, schema version and configuration.\n"
    )
    assert "sky_state" not in output.out + output.err
    assert ledger_rows(empty_database) == before
