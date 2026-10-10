"""Real PostgreSQL + HTTP review consent, atomicity and owner isolation."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")

from adapters.state.operations import PostgresOperationStore
from adapters.state.shared_database_reviews import PostgresSharedDatabaseReviews
from domain.access import LoginSource, Principal, Role
from domain.shared_database import SharedDatabasePool
from ports.operations import ApplicationBusy, IdempotencyConflict
from ports.shared_database_reviews import DatabaseReviewUnavailable

ACTOR = Principal("user", "team-a", Role.DEPLOYER, LoginSource.EXTERNAL_IDP)
JOB = "a" * 16


@pytest.fixture(scope="module")
def database():
    dsn = os.environ.get("SKY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Disposable PostgreSQL DSN required")
    if psycopg.conninfo.conninfo_to_dict(dsn).get("host") not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("Disposable loopback PostgreSQL required")
    connect = lambda: psycopg.connect(dsn, options="-c statement_timeout=30000 -c lock_timeout=10000")
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(
            workers.map(
                lambda _: PostgresSharedDatabaseReviews.initialize_schema(PostgresOperationStore(connect)),
                range(4),
            )
        )
    return connect


@pytest.fixture
def store(database):
    operations = PostgresOperationStore(database, workspace=uuid4().hex)
    pool = SharedDatabasePool("team", "111111111111", "ap-northeast-2", "sky-team", "sky_pool_team", 30)
    value = PostgresSharedDatabaseReviews(operations, pool, "c" * 64)
    operations.records.save_job(
        JOB,
        {
            "id": JOB,
            "application_id": "game",
            "organization_id": "team-a",
            "created_by": "creator",
            "target": "aws-ecs-express",
            "status": "planned",
            "source_digest": "a" * 64,
            "application_ir": {"source_revision": "b" * 64},
            "infrastructure_profile": {"database_engines": ["postgresql"], "evidence": ["package.json"]},
            "aws": {"expected_account": "111111111111", "region": "ap-northeast-2"},
        },
    )
    return value


def counts(store, database):
    with database() as connection:
        return [
            connection.execute(
                f"SELECT count(*) FROM sky_state.{table} WHERE workspace=%s", (store.operations.workspace,)
            ).fetchone()[0]
            for table in ("operations", "outbox_events", "mutation_scopes")
        ]


def test_review_and_atomic_consume_keep_app_unstarted(store, database):
    review = store.review(ACTOR, JOB, connection_limit=7)
    assert review.selection["deployment_ready"] is False and review.job_revision == 1
    assert counts(store, database) == [0, 0, 0]
    accepted = store.submit(ACTOR, review.id, "consent")
    assert store.submit(ACTOR, review.id, "consent") == accepted
    operation = store.operations.get(accepted.operation_id)
    assert operation.kind == "db_shared_allocate" and operation.status == "queued"
    assert operation.command["selection"] == review.selection
    assert operation.command["job_revision"] == review.job_revision
    assert counts(store, database) == [1, 1, 1]
    assert store.operations.records.load_job(JOB).record["status"] == "planned"
    detail = store.detail(ACTOR, review.id)
    assert detail["status"] == "queued" and detail["deployment_ready"] is False
    store.check_ready()


def test_concurrent_same_consent_creates_one_operation_and_outbox(store, database):
    review = store.review(ACTOR, JOB)
    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(lambda _: store.submit(ACTOR, review.id, "same"), range(16)))
    assert len(set(results)) == 1 and counts(store, database) == [1, 1, 1]


def test_late_consumption_failure_rolls_back_operation_outbox_and_reservation(store, database):
    review = store.review(ACTOR, JOB)
    admit = store.operations._admit_in_transaction

    def expire_mid_transaction(connection, *args):
        operation = admit(connection, *args)
        connection.execute(
            """UPDATE sky_state.shared_database_reviews
            SET created_at=clock_timestamp()-interval '2 minutes',expires_at=clock_timestamp()-interval '1 minute'
            WHERE workspace=%s AND id=%s""",
            (store.operations.workspace, review.id),
        )
        return operation

    with (
        patch.object(store.operations, "_admit_in_transaction", side_effect=expire_mid_transaction),
        pytest.raises(DatabaseReviewUnavailable),
    ):
        store.submit(ACTOR, review.id, "consent")
    assert counts(store, database) == [0, 0, 0]
    assert store.detail(ACTOR, review.id)["status"] == "reviewed"
    assert store.submit(ACTOR, review.id, "consent")


@pytest.mark.parametrize("change", ["source", "revision", "owner", "pool_config", "target", "binding"])
def test_changed_review_cannot_admit_work(store, database, change):
    review = store.review(ACTOR, JOB)
    snapshot = store.operations.records.load_job(JOB)
    job = snapshot.record
    if change == "source":
        job["source_digest"] = "d" * 64
    elif change == "revision":
        job["display_name"] = "edited"
    elif change == "owner":
        job["organization_id"] = "other-team"
    elif change == "target":
        job["target"] = "local-docker"
    elif change == "binding":
        job["postgres"] = {"existing": True}
    else:
        store.config_digest = "d" * 64
    if change != "pool_config":
        store.operations.records.save_job(JOB, job, expected_revision=snapshot.revision)
    with pytest.raises((ValueError, FileNotFoundError)):
        store.submit(ACTOR, review.id, "consent")
    assert counts(store, database) == [0, 0, 0]


@pytest.mark.parametrize("stage", ["review", "submit", "detail", "revoke"])
@pytest.mark.parametrize("change", ["viewer", "foreign", "different_user"])
def test_role_org_and_reviewer_checks(store, database, stage, change):
    review = store.review(ACTOR, JOB)
    actor = replace(
        ACTOR,
        **(
            {"role": Role.VIEWER}
            if change == "viewer"
            else {"organization_id": "foreign"}
            if change == "foreign"
            else {"user_id": "other"}
        ),
    )
    if stage == "review" and change == "different_user":
        assert store.review(actor, JOB)
        return
    with pytest.raises((PermissionError, FileNotFoundError)):
        if stage == "review":
            store.review(actor, JOB)
        elif stage == "submit":
            store.submit(actor, review.id, "consent")
        else:
            getattr(store, stage)(actor, review.id)
    assert counts(store, database) == [0, 0, 0]


def test_revoke_and_expiry_stop_admission_but_receipt_replay_survives_expiry(store, database):
    review = store.review(ACTOR, JOB)
    assert store.revoke(ACTOR, review.id) is True and store.revoke(ACTOR, review.id) is False
    with pytest.raises(DatabaseReviewUnavailable):
        store.submit(ACTOR, review.id, "consent")
    review = store.review(ACTOR, JOB)
    accepted = store.submit(ACTOR, review.id, "consent")
    with database() as connection:
        connection.execute(
            """UPDATE sky_state.shared_database_reviews
            SET created_at=clock_timestamp()-interval '2 minutes',expires_at=clock_timestamp()-interval '1 minute'
            WHERE workspace=%s""",
            (store.operations.workspace,),
        )
    assert store.submit(ACTOR, review.id, "consent") == accepted
    fresh = store.review(ACTOR, JOB)
    with database() as connection:
        connection.execute(
            """UPDATE sky_state.shared_database_reviews
            SET created_at=clock_timestamp()-interval '2 minutes',expires_at=clock_timestamp()-interval '1 minute'
            WHERE workspace=%s AND id=%s""",
            (store.operations.workspace, fresh.id),
        )
    with pytest.raises(DatabaseReviewUnavailable):
        store.submit(ACTOR, fresh.id, "consent")
    assert counts(store, database) == [1, 1, 1]


def test_conflicting_consent_or_active_app_does_not_consume_another_review(store, database):
    first, second = store.review(ACTOR, JOB), store.review(ACTOR, JOB)
    store.submit(ACTOR, first.id, "one")
    with pytest.raises(IdempotencyConflict):
        store.submit(ACTOR, first.id, "other")
    with pytest.raises(ApplicationBusy):
        store.submit(ACTOR, second.id, "second")
    with pytest.raises(DatabaseReviewUnavailable):
        store.revoke(ACTOR, first.id)
    assert store.detail(ACTOR, second.id)["status"] == "reviewed"
    assert counts(store, database) == [1, 1, 1]


def test_admin_can_revoke_but_not_submit_another_persons_review(store):
    review = store.review(ACTOR, JOB)
    admin = replace(ACTOR, user_id="admin", role=Role.ADMIN)
    with pytest.raises(FileNotFoundError):
        store.submit(admin, review.id, "admin")
    assert store.revoke(admin, review.id) is True


def test_status_projection_never_returns_arbitrary_result_values(store):
    review = store.review(ACTOR, JOB)
    receipt = store.submit(ACTOR, review.id, "one")
    lease = store.operations.claim(receipt.operation_id, receipt.attempt_id, "worker")
    assert store.operations.complete(
        lease, {"password": "hidden-credential-sentinel", "remaining_gates": ["http_verification"]}
    )
    detail = store.detail(ACTOR, review.id)
    assert detail["status"] == "succeeded" and detail["deployment_ready"] is False
    assert detail["allocation_status"] == "unverified" and detail["remaining_gates"] == [
        "schema_migration",
        "app_secret_access",
        "application_runtime",
        "http_verification",
    ]
    assert "hidden-credential-sentinel" not in json.dumps(detail)
