"""Real logical DB isolation on a disposable, separate workload PostgreSQL."""

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql

from adapters.database.shared_pool import PostgresSharedPool
from domain.shared_database import PoolAllocationRequest, SharedDatabasePool
from engine.database_promotion import DatabaseBinding, PromotionTrigger, plan_database_promotion
from ports.shared_database import (
    AllocationCredentials,
    PoolAllocationConflict,
    PoolAllocationError,
    PoolCapacityExceeded,
)


@pytest.fixture
def database():
    dsn = os.environ.get("SKY_TEST_POOL_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Separate disposable workload PostgreSQL DSN required")
    params = psycopg.conninfo.conninfo_to_dict(dsn)
    if params.get("host") not in {"127.0.0.1", "localhost", "::1"} or not params.get("dbname", "").startswith(
        "sky_pool_"
    ):
        pytest.fail("Only a loopback workload pool control DB is allowed")
    settings = SharedDatabasePool(
        "contract", "111111111111", "ap-northeast-2", "disposable-pool", params["dbname"], 30
    )

    def admin(name):
        return psycopg.connect(
            **{**params, "dbname": name}, options="-c statement_timeout=30000 -c lock_timeout=10000"
        )

    def application(name, role, password):
        return psycopg.connect(**{**params, "dbname": name, "user": role, "password": password})

    adapter = PostgresSharedPool(settings, admin, application)
    adapter.initialize()
    yield adapter, admin, application
    with admin(settings.control_database) as connection:
        connection.autocommit = True
        allocations = connection.execute("SELECT request FROM sky_pool.allocations").fetchall()
        for (request,) in allocations:
            name, role = request["database_name"], request["login_role"]
            assert name.startswith("sky_app_") and role.startswith("sky_login_")
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )
        for (request,) in allocations:
            connection.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(request["login_role"]))
            )
        connection.execute("DELETE FROM sky_pool.allocations")


def credentials(request):
    reference = f"arn:aws:secretsmanager:{request.pool.region}:{request.pool.account_id}:secret:sky-pool/{request.pool.id}/{request.id}-test01"
    return AllocationCredentials(reference, "disposable-pool-password-for-test")


def test_two_apps_can_write_own_tables_but_cannot_connect_to_each_other(database):
    adapter, admin, application = database
    first = PoolAllocationRequest(adapter.pool, "team-a", "game")
    second = PoolAllocationRequest(adapter.pool, "team-b", "game")
    first_result = adapter.allocate(first, credentials(first))
    adapter.allocate(second, credentials(second))
    with application(first.database_name, first.login_role, credentials(first).password) as connection:
        connection.execute("CREATE TABLE scores(id integer PRIMARY KEY, score integer)")
        connection.execute("INSERT INTO scores VALUES (1, 42)")
    for name in (second.database_name, adapter.pool.control_database, "postgres"):
        with pytest.raises(psycopg.OperationalError):
            application(name, first.login_role, credentials(first).password)
    with application(first.database_name, first.login_role, credentials(first).password) as connection:
        assert connection.execute("SELECT score FROM scores WHERE id=1").fetchone() == (42,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("CREATE ROLE forbidden")
    with admin(adapter.pool.control_database) as connection:
        assert credentials(first).password not in str(
            connection.execute("SELECT request FROM sky_pool.allocations").fetchall()
        )
    assert first_result["verified_scope"] == "postgresql_role_and_database_acl"
    target = replace(
        DatabaseBinding(**first_result["binding"]), role="dedicated_workload", instance_id="dedicated-game"
    )
    plan = plan_database_promotion(
        DatabaseBinding(**first_result["binding"]),
        target,
        PromotionTrigger("scheduled_review", "policy-1", "observation-1", "2026-10-11T00:00:00+09:00"),
        source_revision="a" * 64,
    )
    assert plan["ready_to_cutover"] is False


def test_concurrent_retries_share_one_allocation_and_preserve_data(database):
    adapter, admin, application = database
    request = PoolAllocationRequest(adapter.pool, "team-a", "game")
    with ThreadPoolExecutor(max_workers=4) as workers:
        receipts = list(workers.map(lambda _: adapter.allocate(request, credentials(request)), range(6)))
    assert {receipt["allocation_id"] for receipt in receipts} == {request.id}
    with application(request.database_name, request.login_role, credentials(request).password) as connection:
        connection.execute("CREATE TABLE retained(id integer)")
        connection.execute("INSERT INTO retained VALUES (7)")
    PostgresSharedPool(adapter.pool, admin, application).allocate(request, credentials(request))
    with application(request.database_name, request.login_role, credentials(request).password) as connection:
        assert connection.execute("SELECT id FROM retained").fetchall() == [(7,)]
    with pytest.raises(PoolAllocationConflict):
        adapter.allocate(replace(request, connection_limit=10), credentials(request))


def test_budget_exhaustion_has_no_new_db_and_uncertain_effects_are_preserved(database):
    adapter, admin, _ = database
    first = PoolAllocationRequest(adapter.pool, "team-a", "first", 30)
    adapter.allocate(first, credentials(first))
    other = PoolAllocationRequest(adapter.pool, "team-a", "other")
    with pytest.raises(PoolCapacityExceeded):
        adapter.allocate(other, credentials(other))
    with admin(adapter.pool.control_database) as connection:
        assert not connection.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (other.database_name,)
        ).fetchone()
    with (
        patch.object(adapter, "_verify", side_effect=OSError("uncertain probe")),
        pytest.raises(PoolAllocationError),
    ):
        adapter.allocate(first, credentials(first))
    with pytest.raises(PoolAllocationError, match="reconciliation"):
        adapter.allocate(first, credentials(first))
    with admin(adapter.pool.control_database) as connection:
        assert connection.execute(
            "SELECT state FROM sky_pool.allocations WHERE id=%s", (first.id,)
        ).fetchone() == ("needs_attention",)
        assert connection.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (first.database_name,)
        ).fetchone()


def test_privilege_drift_and_state_db_reuse_are_blocked(database):
    adapter, admin, _ = database
    request = PoolAllocationRequest(adapter.pool, "team-a", "game")
    adapter.allocate(request, credentials(request))
    with admin(adapter.pool.control_database) as connection:
        connection.execute(sql.SQL("ALTER ROLE {} CREATEDB").format(sql.Identifier(request.login_role)))
    with pytest.raises(PoolAllocationConflict):
        adapter.allocate(request, credentials(request))
    with admin(adapter.pool.control_database) as connection:
        connection.execute("CREATE SCHEMA sky_state")
    try:
        with pytest.raises(PoolAllocationError, match="Sky state"):
            adapter.initialize()
    finally:
        with admin(adapter.pool.control_database) as connection:
            connection.execute("DROP SCHEMA sky_state")


def test_deleted_ready_database_is_not_recreated_as_empty_success(database):
    adapter, admin, _ = database
    request = PoolAllocationRequest(adapter.pool, "team-a", "game")
    adapter.allocate(request, credentials(request))
    with admin(adapter.pool.control_database) as connection:
        connection.autocommit = True
        connection.execute(
            sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(request.database_name))
        )
    with pytest.raises(PoolAllocationConflict, match="missing"):
        adapter.allocate(request, credentials(request))
    with admin(adapter.pool.control_database) as connection:
        assert not connection.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (request.database_name,)
        ).fetchone()


def test_named_cross_tenant_grant_blocks_further_allocations(database):
    adapter, admin, _ = database
    first = PoolAllocationRequest(adapter.pool, "team-a", "game")
    second = PoolAllocationRequest(adapter.pool, "team-b", "game")
    adapter.allocate(first, credentials(first))
    adapter.allocate(second, credentials(second))
    with admin(adapter.pool.control_database) as connection:
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(first.database_name), sql.Identifier(second.login_role)
            )
        )
    with pytest.raises(PoolAllocationError, match="another"):
        adapter.allocate(first, credentials(first))
