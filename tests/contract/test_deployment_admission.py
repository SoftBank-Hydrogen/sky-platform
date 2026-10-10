"""Atomic admission against disposable PostgreSQL, no AWS or operational DB."""

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")

from adapters.state.deployment_admission import PostgresDeploymentAdmission
from adapters.state.deployment_reads import PostgresDeploymentReads
from adapters.state.operations import PostgresOperationStore
from application.deployment_reads import DeploymentReadService
from domain.access import LoginSource, Principal, Role
from ports.artifacts import SourceArtifact
from ports.operations import ApplicationBusy, IdempotencyConflict
from ports.state import RecordConflict


@pytest.fixture(scope="module")
def database():
    dsn = os.environ.get("SKY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Disposable PostgreSQL DSN required")
    if psycopg.conninfo.conninfo_to_dict(dsn).get("host") not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("Only loopback databases are accepted")
    connect = lambda: psycopg.connect(dsn, options="-c statement_timeout=30000 -c lock_timeout=10000")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda _: PostgresDeploymentAdmission(
                    PostgresOperationStore(connect), account_id="123456789012", region="ap-northeast-2"
                ).initialize(),
                range(4),
            )
        )
    return connect


@pytest.fixture
def store(database):
    return PostgresDeploymentAdmission(
        PostgresOperationStore(database, workspace=uuid4().hex),
        account_id="123456789012",
        region="ap-northeast-2",
    )


def principal(org="team", user="alice", role=Role.DEPLOYER):
    return Principal(user, org, role, LoginSource.CORPORATE_SSO)


def artifact(org="team", app="game"):
    return SourceArtifact(org, app, "a" * 32, "prepared", "b" * 64, 10, "c" * 64)


def admit(store, *, key="request1", who=None, source=None, plan=None, digest=None):
    plan = {"runtime": "node"} if plan is None else plan
    digest = (
        digest
        or hashlib.sha256(
            json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    source = source or artifact()
    return store.admit(
        who or principal(),
        source,
        key,
        plan,
        expected_plan_digest=digest,
        expected_source_digest=source.source_digest,
    )


def counts(store, database):
    with database() as connection:
        return [
            connection.execute(
                f"SELECT count(*) FROM sky_state.{table} WHERE workspace=%s", (store.operations.workspace,)
            ).fetchone()[0]
            for table in (
                "application_owners",
                "operations",
                "mutation_scopes",
                "operation_events",
                "outbox_events",
                "metadata_records",
            )
        ]


def test_admission_links_readable_owned_job_operation_and_outbox(store, database):
    result = admit(store)
    job = store.operations.records.load_job(result.job_id)
    operation = store.operations.get(result.operation_id)
    assert job.revision == 1
    assert job.record["operation_id"] == result.operation_id
    assert job.record["created_at"]
    assert job.record["source_ref"] == operation.command["source_ref"] == artifact().record()
    assert operation.command["job_id"] == result.job_id
    assert operation.command["account_id"] == "123456789012"
    reads = DeploymentReadService(PostgresDeploymentReads(database, workspace=store.operations.workspace))
    assert reads.detail(principal(), result.job_id)["status"] == "queued"
    with pytest.raises(FileNotFoundError):
        reads.detail(principal(org="foreign", role=Role.ADMIN), result.job_id)
    assert counts(store, database) == [1, 1, 1, 1, 1, 1]


def test_concurrent_identical_requests_are_one_atomic_admission(store, database):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: admit(store), range(16)))
    assert len(set(results)) == 1
    assert counts(store, database) == [1, 1, 1, 1, 1, 1]


def test_competing_requests_have_no_partial_records(store, database):
    def request(index):
        try:
            return admit(store, key=f"request{index}")
        except ApplicationBusy:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(request, range(8)))
    assert sum(result is not None for result in results) == 1
    assert counts(store, database) == [1, 1, 1, 1, 1, 1]


@pytest.mark.parametrize("change", ["plan", "source", "application"])
def test_same_scoped_key_cannot_change_command(store, change):
    admit(store)
    kwargs = (
        {"plan": {"runtime": "python"}}
        if change == "plan"
        else {
            "source": replace(
                artifact(), **({"sha256": "d" * 64} if change == "source" else {"application_id": "other"})
            )
        }
    )
    with pytest.raises(IdempotencyConflict):
        admit(store, **kwargs)


def test_request_keys_are_user_and_org_scoped(store):
    first = admit(store)
    second = admit(store, who=principal(org="other", user="bob"), source=artifact("other", "another"))
    assert first != second
    with pytest.raises(ApplicationBusy):
        admit(store, who=principal(user="bob"))


def test_replay_preserves_progress_and_does_not_requeue_completed_operation(store, database):
    result = admit(store)
    lease = store.operations.claim(
        result.operation_id, store.operations.get(result.operation_id).attempt_id, "worker"
    )
    assert store.operations.complete(lease, {"url": "https://example.com"})
    snapshot = store.operations.records.load_job(result.job_id)
    changed = {**snapshot.record, "status": "succeeded", "events": [{"stage": "done"}]}
    store.operations.records.save_job(result.job_id, changed, expected_revision=snapshot.revision)
    assert admit(store) == result
    assert store.operations.records.load_job(result.job_id).record == changed
    assert counts(store, database) == [1, 1, 0, 3, 1, 1]


@pytest.mark.parametrize("stage", ["_outbox", "_event"])
def test_mid_transaction_failure_rolls_back_all_records(store, database, stage):
    with (
        patch.object(store.operations, stage, side_effect=RuntimeError("injected")),
        pytest.raises(RuntimeError),
    ):
        admit(store)
    assert counts(store, database) == [0] * 6
    admit(store)
    assert counts(store, database) == [1] * 6


def test_job_collision_rolls_back_operation_scope_owner_and_outbox(store, database):
    scope = [store.operations.workspace, "team", "alice", "request1"]
    job_id = store._digest(["job", *scope])[:16]
    store.operations.records.save_job(job_id, {"id": job_id, "application_id": "unrelated"})
    with pytest.raises(RecordConflict):
        admit(store)
    assert counts(store, database) == [0, 0, 0, 0, 0, 1]


@pytest.mark.parametrize("legacy", ["metadata", "operation"])
def test_existing_app_is_not_automatically_claimed(store, database, legacy):
    if legacy == "metadata":
        store.operations.records.save_job("old", {"id": "old", "application_id": "game"})
    else:
        store.operations.admit("game", "deploy", "old", {})
    before = counts(store, database)
    with pytest.raises(FileNotFoundError, match="explicit migration"):
        admit(store)
    assert counts(store, database) == before


def test_foreign_admin_cannot_take_existing_application(store, database):
    admit(store)
    before = counts(store, database)
    with pytest.raises(FileNotFoundError):
        admit(store, who=principal(org="foreign", role=Role.ADMIN), source=artifact("foreign"))
    assert counts(store, database) == before


def test_concurrent_foreign_orgs_cannot_both_claim_app(store, database):
    def request(org):
        try:
            return admit(store, who=principal(org=org), source=artifact(org))
        except FileNotFoundError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, ["team", "foreign"]))
    assert sum(result is not None for result in results) == 1
    assert counts(store, database) == [1] * 6


@pytest.mark.parametrize("case", ["viewer", "foreign_source", "original", "plan_digest", "nan", "oversized"])
def test_invalid_admission_does_not_write(store, database, case):
    kwargs = {
        "viewer": {"who": principal(role=Role.VIEWER)},
        "foreign_source": {"source": artifact("foreign")},
        "original": {"source": replace(artifact(), kind="original")},
        "plan_digest": {"digest": "d" * 64},
        "nan": {"plan": {"value": float("nan")}},
        "oversized": {"plan": {"value": "x" * 65536}},
    }[case]
    with pytest.raises((PermissionError, FileNotFoundError, ValueError)):
        admit(store, **kwargs)
    assert counts(store, database) == [0] * 6


def test_source_approval_digest_must_match(store, database):
    plan = {"runtime": "node"}
    with pytest.raises(ValueError, match="Source no longer"):
        store.admit(
            principal(),
            artifact(),
            "request",
            plan,
            expected_plan_digest=store._digest(plan),
            expected_source_digest="d" * 64,
        )
    assert counts(store, database) == [0] * 6


def test_workspace_isolation(database):
    stores = [
        PostgresDeploymentAdmission(
            PostgresOperationStore(database, workspace=uuid4().hex),
            account_id="123456789012",
            region="ap-northeast-2",
        )
        for _ in range(2)
    ]
    assert admit(stores[0]) != admit(stores[1])


def test_same_org_can_redeploy_after_completion_without_changing_app_owner(store, database):
    first = admit(store)
    operation = store.operations.get(first.operation_id)
    lease = store.operations.claim(operation.id, operation.attempt_id, "worker")
    assert store.operations.complete(lease, {})
    second = admit(store, key="request2", who=principal(user="bob"))
    assert second != first
    with database() as connection:
        owner = connection.execute(
            "SELECT organization_id,created_by FROM sky_state.application_owners WHERE workspace=%s",
            (store.operations.workspace,),
        ).fetchone()
    assert owner == ("team", "alice")
    assert store.operations.records.load_job(second.job_id).record["created_by"] == "bob"


def test_non_object_job_collision_is_rejected_without_partial_admission(store, database):
    job_id = store._digest(["job", store.operations.workspace, "team", "alice", "request1"])[:16]
    store.operations.records.save_job(job_id, ["legacy"])
    with pytest.raises(RecordConflict):
        admit(store)
    assert counts(store, database) == [0, 0, 0, 0, 0, 1]


def test_uncertain_commit_can_be_retried_without_duplicate_records(store, database):
    original = store.operations.records.connection_factory

    @contextmanager
    def lost_commit_reply():
        with original() as connection:
            yield connection
        # The server committed, but the caller did not receive its acknowledgement.
        raise psycopg.OperationalError("injected connection loss after commit")

    with (
        patch.object(store.operations.records, "connection_factory", lost_commit_reply),
        pytest.raises(OSError, match="outcome may be uncertain"),
    ):
        admit(store)
    assert counts(store, database) == [1] * 6
    result = admit(store)
    assert store.operations.records.load_job(result.job_id).record["operation_id"] == result.operation_id
    assert counts(store, database) == [1] * 6
