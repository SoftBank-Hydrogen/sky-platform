"""Real PostgreSQL tests; run only against an explicit disposable loopback DB."""

import os
from dataclasses import asdict
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from adapters.database.promotion_copy import PostgresPromotionCopy
from domain.database import DatabaseBinding


@pytest.fixture
def databases():
    dsn = os.environ.get("SKY_TEST_PROMOTION_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Disposable PostgreSQL required")
    params = conninfo_to_dict(dsn)
    assert params.get("host") == "127.0.0.1" and params.get("dbname") == "postgres"
    names = ["copy_src_" + uuid4().hex[:12], "copy_dst_" + uuid4().hex[:12]]
    with psycopg.connect(**params, autocommit=True) as admin:
        for name in names:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    factories = [lambda name=name: psycopg.connect(**{**params, "dbname": name}) for name in names]
    bindings = [
        asdict(DatabaseBinding("org", "app", role, "111111111111", "ap-northeast-2", instance, name, "owner"))
        for role, instance, name in zip(
            ["shared_workload", "dedicated_workload"], ["pool", "dedicated"], names
        )
    ]
    yield factories, {"plan_id": "DBP-test", "source": bindings[0], "target": bindings[1]}
    with psycopg.connect(**params, autocommit=True) as admin:
        for name in names:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def seed(connect):
    with connect() as db:
        db.execute(
            "CREATE TABLE scores(id integer PRIMARY KEY, score bigint NOT NULL, label text, payload jsonb)"
        )
        db.execute("INSERT INTO scores VALUES (1,42,'한글', '{\"a\":1}'),(2,0,NULL,NULL)")


def test_copy_preserves_schema_null_unicode_and_rows(databases):
    (source, target), plan = databases
    seed(source)
    receipt = PostgresPromotionCopy(source, target).copy(plan, guard=lambda: None)
    assert receipt["tables"][0]["rows"] == 2
    assert receipt["source_retained"] and not receipt["ready_to_cutover"] and not receipt["routing_changed"]
    with source() as src, target() as dst:
        assert (
            src.execute("SELECT * FROM scores ORDER BY id").fetchall()
            == dst.execute("SELECT * FROM scores ORDER BY id").fetchall()
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            dst.execute("INSERT INTO scores VALUES (1,1,NULL,NULL)")


@pytest.mark.parametrize(
    "extra",
    [
        "CREATE TABLE extra(id serial)",
        "CREATE VIEW extra AS SELECT * FROM scores",
        "CREATE INDEX extra ON scores(label)",
        "ALTER TABLE scores ADD CHECK(score>=0)",
    ],
)
def test_unsupported_schema_fails_without_partial_target(databases, extra):
    (source, target), plan = databases
    seed(source)
    with source() as db:
        db.execute(extra)
    with pytest.raises(ValueError, match="Unsupported"):
        PostgresPromotionCopy(source, target).copy(plan, guard=lambda: None)
    with target() as db:
        assert db.execute("SELECT to_regclass('scores')").fetchone() == (None,)


def test_existing_target_never_overwritten(databases):
    (source, target), plan = databases
    seed(source)
    seed(target)
    with pytest.raises(ValueError, match="empty"):
        PostgresPromotionCopy(source, target).copy(plan, guard=lambda: None)


def test_lease_loss_rolls_back_target(databases):
    (source, target), plan = databases
    seed(source)
    count = 0

    def guard():
        nonlocal count
        count += 1
        if count == 3:
            raise RuntimeError("lease lost")

    with pytest.raises(RuntimeError):
        PostgresPromotionCopy(source, target).copy(plan, guard=guard)
    with target() as db:
        assert db.execute("SELECT to_regclass('scores')").fetchone() == (None,)


def test_source_writes_blocked_during_copy_then_resume(databases):
    (source, target), plan = databases
    seed(source)
    count = 0

    def guard():
        nonlocal count
        count += 1
        if count == 2:
            with source() as db:
                db.execute("SET LOCAL lock_timeout='100ms'")
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    db.execute("UPDATE scores SET score=99 WHERE id=1")

    PostgresPromotionCopy(source, target).copy(plan, guard=guard)
    with source() as db:
        db.execute("UPDATE scores SET score=99 WHERE id=1")
    with target() as db:
        assert db.execute("SELECT score FROM scores WHERE id=1").fetchone() == (42,)


def test_bounded_copy_rolls_back(databases):
    (source, target), plan = databases
    seed(source)
    with pytest.raises(ValueError, match="limit"):
        PostgresPromotionCopy(source, target, max_rows=1).copy(plan, guard=lambda: None)
    with target() as db:
        assert db.execute("SELECT to_regclass('scores')").fetchone() == (None,)


def test_wrong_target_binding_cannot_modify_database(databases):
    (source, target), plan = databases
    seed(source)
    plan["target"]["database_name"] = "foreign"
    with pytest.raises(ValueError, match="binding"):
        PostgresPromotionCopy(source, target).copy(plan, guard=lambda: None)
    with target() as db:
        assert db.execute("SELECT to_regclass('scores')").fetchone() == (None,)


def test_copy_budget_applies_across_tables(databases):
    (source, target), plan = databases
    seed(source)
    with source() as db:
        db.execute("CREATE TABLE extra(id integer PRIMARY KEY)")
        db.execute("INSERT INTO extra VALUES(1)")
    with pytest.raises(ValueError, match="limit"):
        PostgresPromotionCopy(source, target, max_rows=2).copy(plan, guard=lambda: None)
    with target() as db:
        assert db.execute("SELECT to_regclass('extra'),to_regclass('scores')").fetchone() == (None, None)
