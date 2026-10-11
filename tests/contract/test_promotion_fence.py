from dataclasses import replace
from uuid import uuid4

import pytest
from psycopg import sql

from adapters.database.promotion_fence import PostgresApplicationFence
from domain.database import DatabaseBinding
from tests.contract.test_promotion_copy import databases as databases  # noqa: PLC0414


def test_owned_login_fenced_and_restored(databases):
    (src, _dst), plan = databases
    role = "sky_login_" + uuid4().hex[:24]
    marker = "sky-pool-allocation-v1:" + role.removeprefix("sky_login_")
    binding = DatabaseBinding(**plan["source"])
    with src() as db:
        db.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            ).format(sql.Identifier(role))
        )
        db.execute(sql.SQL("COMMENT ON ROLE {} IS {}").format(sql.Identifier(role), sql.Literal(marker)))
        db.execute(
            sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                sql.Identifier(binding.database_name), sql.Literal(marker)
            )
        )
    try:
        fence = PostgresApplicationFence(src, binding, role, marker)
        fence.set_login(False, guard=lambda: None)
        with src() as db:
            assert db.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname=%s", (role,)).fetchone() == (
                False,
            )
        fence.set_login(True, guard=lambda: None)
        with src() as db:
            assert db.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname=%s", (role,)).fetchone() == (
                True,
            )
        with pytest.raises(ValueError, match="unowned"):
            PostgresApplicationFence(src, binding, role, marker + "wrong").set_login(
                False, guard=lambda: None
            )
        with pytest.raises(ValueError, match="binding"):
            PostgresApplicationFence(src, replace(binding, database_name="wrong"), role, marker).set_login(
                False, guard=lambda: None
            )
    finally:
        with src() as db:
            db.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_pool_admin_database_can_fence_when_app_database_connection_limit_is_full(databases):
    import os

    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    (_src, _dst), plan = databases
    binding = DatabaseBinding(**plan["source"])
    suffix = uuid4().hex[:12]
    admin_role, app_role, control = "fence_admin_" + suffix, "sky_login_" + suffix, "sky_pool_" + suffix
    marker = "sky-pool-allocation-v1:" + suffix
    params = conninfo_to_dict(os.environ["SKY_TEST_PROMOTION_POSTGRES_DSN"])
    root = psycopg.connect(**params, autocommit=True)
    held = None
    try:
        root.execute(
            sql.SQL("CREATE ROLE {} LOGIN CREATEROLE PASSWORD 'validation-only'").format(
                sql.Identifier(admin_role)
            )
        )
        root.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'validation-only'").format(sql.Identifier(app_role))
        )
        root.execute(
            sql.SQL("GRANT {} TO {} WITH ADMIN OPTION").format(
                sql.Identifier(app_role), sql.Identifier(admin_role)
            )
        )
        root.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(control), sql.Identifier(admin_role))
        )
        root.execute(
            sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                sql.Identifier(binding.database_name), sql.Identifier(admin_role)
            )
        )
        root.execute(
            sql.SQL("ALTER DATABASE {} CONNECTION LIMIT 1").format(sql.Identifier(binding.database_name))
        )
        root.execute(
            sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                sql.Identifier(binding.database_name), sql.Literal(marker)
            )
        )
        root.execute(
            sql.SQL("COMMENT ON ROLE {} IS {}").format(sql.Identifier(app_role), sql.Literal(marker))
        )
        held = psycopg.connect(**{**params, "dbname": binding.database_name, "user": app_role})
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(**{**params, "dbname": binding.database_name, "user": admin_role})
        admin_connect = lambda: psycopg.connect(**{**params, "dbname": control, "user": admin_role})
        fence = PostgresApplicationFence(
            admin_connect, binding, app_role, marker, administrative_database=control
        )
        fence.set_login(False, guard=lambda: None)
        with pytest.raises(psycopg.OperationalError):
            held.execute("SELECT 1")
        fence.set_login(True, guard=lambda: None)
    finally:
        if held:
            held.close()
        root.execute(
            sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                sql.Identifier(binding.database_name), sql.Identifier(params["user"])
            )
        )
        root.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(control)))
        root.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(admin_role)))
        root.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(app_role)))
        root.close()
