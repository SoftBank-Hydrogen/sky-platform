import pytest

from adapters.database.dedicated_access import PostgresDedicatedAccess, access_identity
from domain.database import DatabaseBinding
from tests.contract.test_promotion_copy import databases as databases  # noqa: PLC0414
from tests.contract.test_promotion_copy import psycopg, seed


@pytest.fixture
def access(databases):
    (_source, target), plan = databases
    from psycopg import sql

    # Local fixture emulates two instances in one cluster: the source DB must
    # not expose default PUBLIC CONNECT to the new dedicated login.
    with _source() as db:
        db.execute(
            sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(plan["source"]["database_name"])
            )
        )
    seed(target)
    binding = DatabaseBinding(**plan["target"])
    role, _ = access_identity(binding)

    def login(user, password):
        with target() as db:
            params = db.info.get_parameters()
        return psycopg.connect(**{**params, "user": user, "password": password})

    adapter = PostgresDedicatedAccess(target, login)
    yield adapter, binding, target, login
    from psycopg import sql

    with target() as db:
        if db.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
            db.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            db.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def prepare(adapter, binding, guard=lambda: None):
    return adapter.provision(
        binding, password="disposable-app-password", secret_ref="test-secret-reference", guard=guard
    )


def test_app_has_crud_without_schema_or_database_admin_rights(access):
    adapter, binding, _admin, login = access
    receipt = prepare(adapter, binding)
    with login(receipt["login_role"], "disposable-app-password") as db:
        assert db.execute("SELECT count(*) FROM scores").fetchone() == (2,)
        db.execute("INSERT INTO scores VALUES(3,10,'app',NULL)")
        db.execute("UPDATE scores SET score=11 WHERE id=3")
        db.execute("DELETE FROM scores WHERE id=3")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("CREATE TABLE forbidden(id integer)")
    assert receipt["routing_changed"] is False
    assert "disposable-app-password" not in str(receipt)


def test_owned_retry_uses_same_role_and_secret(access):
    adapter, binding, _, _ = access
    assert prepare(adapter, binding) == prepare(adapter, binding)


def test_foreign_role_not_adopted(access):
    adapter, binding, admin, _ = access
    from psycopg import sql

    role, _ = access_identity(binding)
    with admin() as db:
        db.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
    with pytest.raises(ValueError, match="unowned"):
        prepare(adapter, binding)


def test_lost_guard_rolls_back_new_role(access):
    adapter, binding, admin, _ = access
    calls = 0

    def guard():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("lease lost")

    with pytest.raises(RuntimeError):
        prepare(adapter, binding, guard)
    role, _ = access_identity(binding)
    with admin() as db:
        assert db.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone() is None


def test_existing_role_privilege_escalation_is_rejected(access):
    adapter, binding, admin, _ = access
    receipt = prepare(adapter, binding)
    from psycopg import sql

    with admin() as db:
        db.execute(sql.SQL("ALTER ROLE {} CREATEDB").format(sql.Identifier(receipt["login_role"])))
    with pytest.raises(ValueError, match="privileges"):
        prepare(adapter, binding)
