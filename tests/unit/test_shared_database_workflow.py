"""Choice tampering and execution ownership never grant cloud mutation."""

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest

from application.shared_database_workflow import SharedDatabaseAdmission, SharedDatabaseWorker
from domain.access import LoginSource, Principal, Role
from domain.shared_database import PoolAllocationRequest, SharedDatabasePool
from engine.database_placement import select_shared_database
from ports.operations import ExecutionLease, Operation
from ports.state import StoredJob


def source():
    return {
        "id": "a" * 16,
        "application_id": "game",
        "organization_id": "team-a",
        "created_by": "creator",
        "target": "aws-ecs-express",
        "status": "planned",
        "source_digest": "a" * 64,
        "application_ir": {"source_revision": "b" * 64},
        "infrastructure_profile": {"database_engines": ["postgresql"], "evidence": ["package.json"]},
        "aws": {"expected_account": "111111111111", "region": "ap-northeast-2"},
    }


def actor(role=Role.DEPLOYER):
    return Principal("user", "team-a", role, LoginSource.EXTERNAL_IDP)


def pool():
    return SharedDatabasePool("team", "111111111111", "ap-northeast-2", "sky-team", "sky_pool_team", 30)


@pytest.fixture
def workflow():
    job = source()
    records = Mock(load_job=Mock(side_effect=lambda _: StoredJob(deepcopy(job), "unused", 1)))
    operations = Mock()
    selection = select_shared_database(job, pool(), config_digest="c" * 64)
    command = {
        "schema_version": 1,
        "job_id": job["id"],
        "job_revision": 1,
        "requested_by": {"user_id": "user", "organization_id": "team-a"},
        "selection": selection,
    }
    operation = Operation(
        str(uuid4()),
        "game",
        "db_shared_allocate",
        "queued",
        str(uuid4()),
        command,
        {},
        None,
        False,
        1,
        None,
        None,
    )
    operations.get.return_value = operation
    lease = ExecutionLease(
        operation.id, operation.attempt_id, "worker", 1, datetime.now(UTC) + timedelta(minutes=15), "team"
    )
    operations.claim.return_value = lease
    operations.begin_external.return_value = operations.observe_external.return_value = (
        operations.complete.return_value
    ) = True
    request = PoolAllocationRequest(pool(), "team-a", "game")
    allocator = Mock(
        allocate=Mock(
            return_value={
                "status": "ready",
                "allocation_id": request.id,
                "binding": asdict(request.binding()),
                "secret_ref": f"arn:aws:secretsmanager:ap-northeast-2:111111111111:secret:sky-pool/team/{request.id}-test01",
                "verified_scope": "postgresql_role_and_database_acl",
                "password": "must-never-be-persisted",
            }
        )
    )
    resolve = Mock(return_value=actor())
    worker = SharedDatabaseWorker(records, operations, pool(), "c" * 64, allocator, resolve, owner="worker")
    return worker, job, records, operations, allocator, resolve, operation


def test_admission_uses_owned_snapshot_and_only_stores_references(workflow):
    _, job, records, operations, allocator, _, _ = workflow
    service = SharedDatabaseAdmission(records, operations, pool(), "c" * 64)
    selection = service.choose(actor(), job["id"])
    assert selection["deployment_ready"] is False and selection["selection_basis"] == "explicit_user_choice"
    service.admit(actor(), job["id"], selection, "request-1")
    application, kind, key, command = operations.admit.call_args.args
    assert (application, kind, key) == ("game", "db_shared_allocate", "request-1")
    assert command["job_revision"] == 1 and "password" not in str(command)
    allocator.allocate.assert_not_called()
    selection["allocation"]["connection_limit"] = 50
    with pytest.raises(ValueError):
        service.admit(actor(), job["id"], selection, "request-2")
    assert operations.admit.call_count == 1


@pytest.mark.parametrize(
    "change", ["target", "running", "sqlite", "unknown", "existing", "migration", "source", "region"]
)
def test_unsuitable_or_existing_workload_is_not_silently_moved(change):
    job = source()
    if change == "target":
        job["target"] = "local-docker"
    elif change == "running":
        job["status"] = "running"
    elif change == "sqlite":
        job["infrastructure_profile"]["database_engines"] = ["sqlite"]
    elif change == "unknown":
        job["infrastructure_profile"]["database_engines"] = ["unknown"]
    elif change == "existing":
        job["result"] = {"database": "already-deployed"}
    elif change == "migration":
        job["sqlite_conversion"] = {"source": "data.db"}
    elif change == "source":
        job["application_ir"]["source_revision"] = None
    else:
        job["aws"]["region"] = "us-east-1"
    with pytest.raises(ValueError):
        select_shared_database(job, pool(), config_digest="c" * 64)


def test_worker_records_only_verified_allowlist_and_never_marks_app_deployed(workflow):
    worker, _, _, operations, allocator, _, operation = workflow
    assert worker.execute(operation.id, operation.attempt_id)["status"] == "succeeded"
    allocator.allocate.assert_called_once()
    assert "must-never-be-persisted" not in str(operations.method_calls)
    result = operations.complete.call_args.args[1]
    assert result["deployment_ready"] is False and result["deployment_status"] == "not_started"
    assert "http_verification" in result["remaining_gates"]
    assert operations.begin_external.call_count == operations.observe_external.call_count == 1


@pytest.mark.parametrize("change", ["revoked", "source", "config", "selection", "scope", "revision"])
def test_revalidation_failure_does_not_allocate_or_begin_external(workflow, change):
    worker, job, records, operations, allocator, resolve, operation = workflow
    if change == "revoked":
        resolve.return_value = actor(Role.VIEWER)
    elif change == "source":
        job["source_digest"] = "d" * 64
    elif change == "config":
        worker.config_digest = "d" * 64
    elif change == "selection":
        operation.command["selection"]["allocation"]["connection_limit"] = 50
    elif change == "scope":
        operations.get.return_value = replace(operation, application_id="other")
    else:
        records.load_job.side_effect = lambda _: StoredJob(job, "unused", 2)
    assert worker.execute(operation.id, operation.attempt_id)["status"] == "failed"
    allocator.allocate.assert_not_called()
    operations.begin_external.assert_not_called()
    assert operations.complete.call_args.kwargs["succeeded"] is False


def test_old_attempt_and_lost_claim_never_allocate(workflow):
    worker, _, _, operations, allocator, _, operation = workflow
    assert worker.execute(operation.id, str(uuid4()))["status"] == "stale_attempt"
    operations.claim.assert_not_called()
    operations.claim.return_value = None
    assert worker.execute(operation.id, operation.attempt_id)["status"] == "not_claimed"
    allocator.allocate.assert_not_called()


def test_uncertain_external_effect_is_preserved_and_not_completed(workflow):
    worker, _, _, operations, allocator, _, operation = workflow
    allocator.allocate.side_effect = RuntimeError("private credentials")
    assert worker.execute(operation.id, operation.attempt_id)["status"] == "needs_attention"
    operations.interrupt.assert_called_once()
    operations.observe_external.assert_not_called()
    operations.complete.assert_not_called()
    assert "private credentials" not in str(operations.method_calls)


def test_recorded_observation_resumes_without_new_cloud_request(workflow):
    worker, _, _, operations, allocator, _, operation = workflow
    worker.execute(operation.id, operation.attempt_id)
    receipt = operations.observe_external.call_args.args[1]
    intent = operations.begin_external.call_args.args[1]
    operations.get.return_value = replace(
        operation,
        external_receipt=receipt,
        external_intent=intent,
        checkpoint={"stage": "database_allocated"},
    )
    allocator.reset_mock()
    operations.begin_external.reset_mock()
    assert worker.execute(operation.id, operation.attempt_id)["status"] == "succeeded"
    allocator.allocate.assert_not_called()
    operations.begin_external.assert_not_called()


def test_ambiguous_observation_commit_is_never_retried_or_interrupted(workflow):
    worker, _, _, operations, _, _, operation = workflow
    operations.observe_external.side_effect = OSError("commit response lost")
    with pytest.raises(OSError):
        worker.execute(operation.id, operation.attempt_id)
    operations.observe_external.assert_called_once()
    operations.interrupt.assert_not_called()
    operations.complete.assert_not_called()
