from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest

from application.database_cutover import DatabaseCutover
from application.database_cutover_workflow import KIND, PromotionCutoverAdmission, PromotionCutoverWorker
from application.shared_database_consumer import AllocationQueueRouter, SharedDatabaseConsumer
from domain.database import DatabaseBinding
from ports.operations import ExecutionLease, Operation, OutboxDelivery
from ports.queue import ReceivedOperation
from tests.unit.test_database_cutover import Fence, Traffic
from tests.unit.test_deployment_writes import principal
from tests.unit.test_operating_review import ID, NOW

pytest_plugins = ["tests.unit.test_operating_review"]


@pytest.fixture
def flow(setup):
    records, policy, reviews = setup
    proposal = reviews.review(principal(), ID, policy, now=NOW)
    approved = reviews.approve(principal(), ID, proposal["proposal_id"], policy, now=NOW)
    plan = approved["plan"]
    baseline = {key: plan[key] for key in ("plan_id", "source", "target")}
    baseline.update(
        schema_sha256="a" * 64,
        tables=[{"table": "scores", "rows": 3, "sha256": "b" * 64}],
        verified_scope="locked_table_snapshot_copy",
        source_retained=True,
        routing_changed=False,
        ready_to_cutover=False,
        requires_final_write_freeze=True,
    )
    registration = {key: plan[key] for key in ("plan_id", "source", "target")}
    registration.update(
        baseline=baseline,
        controller_id="game-control",
        controller_revision="c" * 64,
        verified_until="2026-10-11T01:00:00+00:00",
        evidence={
            name: "evidence:validation/" + name
            for name in ("target_ownership_tls", "schema_copy", "all_writers_controlled", "control_auth_tls")
        },
    )
    registrations = {ID: registration}
    operations = Mock(workspace="team")
    admission = PromotionCutoverAdmission(
        records, operations, {ID: policy}, registrations, clock=lambda: NOW, enabled=True
    )
    admission.admit(principal(), ID, approved["proposal_id"], "cutover-one")
    command = operations.admit.call_args.args[3]
    operation = Operation(
        str(uuid4()), "game", KIND, "queued", str(uuid4()), command, {}, None, False, 1, None, None
    )
    operations.get.return_value = operation
    operations.claim.return_value = ExecutionLease(
        operation.id, operation.attempt_id, "worker", 1, datetime.now(UTC) + timedelta(minutes=15), "team"
    )
    for name in (
        "heartbeat",
        "begin_external",
        "checkpoint",
        "observe_external",
        "complete",
        "require_attention",
    ):
        getattr(operations, name).return_value = True
    traffic = Traffic(plan["source"])
    sync = Mock()
    sync.refresh.return_value = {**baseline, "verified_scope": "final_locked_refresh"}

    def factory(plan, registration, *, guard, journal):
        return DatabaseCutover(
            sync,
            Fence(DatabaseBinding(**plan["source"])),
            Fence(DatabaseBinding(**plan["target"])),
            traffic,
            guard=guard,
            journal=journal,
        )

    factory = Mock(side_effect=factory)
    resolver = Mock(return_value=principal())
    worker = PromotionCutoverWorker(
        records, operations, {ID: policy}, registrations, factory, resolver, owner="worker", clock=lambda: NOW
    )
    return worker, admission, records, operations, registrations, factory, traffic, sync, operation, resolver


def run(flow):
    worker, _, _, _, _, _, _, _, operation, _ = flow
    return worker.execute(operation.id, operation.attempt_id)


def test_approved_cutover_projects_binding_only_after_verified_app_probe(flow):
    _, _, records, operations, _, factory, traffic, _, operation, _ = flow
    assert run(flow) == {"status": "succeeded"}
    assert (
        records.job["workload_database_binding"]
        == operation.command["approval"]["proposal"]["plan"]["target"]
    )
    assert not traffic.closed
    assert records.job["database_promotion"]["operation_id"] == operation.id
    assert records.job["result"]["service"] == "owned-service"
    assert (
        operations.begin_external.call_count
        == operations.observe_external.call_count
        == operations.complete.call_count
        == 1
    )
    stages = operations.observe_external.call_args.args[2]["cutover_steps"]
    assert stages[-1]["stage"] == "resume_target" and stages[-1]["status"] == "observed"
    factory.assert_called_once()


@pytest.mark.parametrize(
    "change", ["revision", "approval", "membership", "baseline", "controller", "expired", "evidence"]
)
def test_revalidation_never_calls_controller_or_database(flow, change):
    _, _, records, operations, registrations, factory, _, sync, _, resolver = flow
    if change == "revision":
        records.revision += 1
    elif change == "approval":
        records.job["operating_proposal"]["approval"]["proposal_id"] = "different"
    elif change == "membership":
        resolver.return_value = None
    elif change == "baseline":
        registrations[ID]["baseline"]["tables"][0]["sha256"] = "d" * 64
    elif change == "controller":
        registrations[ID]["controller_revision"] = "e" * 64
    elif change == "expired":
        registrations[ID]["verified_until"] = NOW
    else:
        registrations[ID]["evidence"].pop("all_writers_controlled")
    assert run(flow)["status"] == "failed"
    operations.begin_external.assert_not_called()
    factory.assert_not_called()
    sync.refresh.assert_not_called()


def test_probe_failure_preserves_source_projection_and_durable_recovery_scope(flow):
    _, _, records, operations, _, _, traffic, _, operation, _ = flow
    original = deepcopy(records.job)
    traffic.fail = True
    assert run(flow)["status"] == "needs_attention"
    assert traffic.closed and records.job == original
    operations.require_attention.assert_called_once()
    operations.complete.assert_not_called()
    stages = operations.require_attention.call_args.args[1]["cutover_steps"]
    assert stages[-1] == {"stage": "probe_target", "status": "intent"}
    operations.get.return_value = replace(operation, status="needs_attention", external_pending=True)
    assert run(flow)["status"] == "needs_attention"


@pytest.mark.parametrize(
    "failure", ["cas", "metadata_commit_unknown", "observation_commit", "completion_commit"]
)
def test_post_cutover_projection_or_operation_failure_never_replays_app_mutation(flow, failure):
    _, _, records, operations, _, factory, traffic, _, _, _ = flow
    if failure == "cas":
        records.before_save = lambda records: setattr(records, "revision", records.revision + 1)
    elif failure == "metadata_commit_unknown":
        records.uncertain = True
    elif failure == "observation_commit":
        operations.observe_external.side_effect = OSError("commit response lost")
    else:
        operations.complete.side_effect = OSError("commit response lost")
    assert run(flow)["status"] == "needs_attention"
    assert not traffic.closed  # app was verified; do not blindly reverse later writes
    assert factory.call_count == 1
    operations.require_attention.assert_called_once()


def test_existing_intent_is_held_without_replaying_any_controller_call(flow):
    _, _, _, operations, _, factory, _, _, operation, _ = flow
    operations.get.return_value = replace(
        operation, external_pending=True, external_intent={"action": "database_cutover"}
    )
    assert run(flow)["status"] == "needs_attention"
    factory.assert_not_called()


def test_cutover_admission_default_disabled_and_no_secret_in_queue_command(flow):
    _, admission, _, operations, _, _, _, _, operation, _ = flow
    admission.enabled = False
    operations.admit.reset_mock()
    with pytest.raises(ValueError, match="registered"):
        admission.admit(principal(), ID, operation.command["approval"]["proposal"]["proposal_id"], "next")
    operations.admit.assert_not_called()
    assert set(operation.command) == {"schema_version", "approval", "registration_digest"}
    assert "baseline" not in str(operation.command)


def test_new_kind_uses_explicit_queue_never_builder_and_consumer_executes_worker(flow):
    worker, _, _, operations, _, _, _, _, operation, _ = flow
    default, allocation, preparation, queue = Mock(), Mock(), Mock(), Mock()
    delivery = OutboxDelivery("event", "owner", 1, operation.id, operation.attempt_id, "game", "team")
    with pytest.raises(OSError):
        AllocationQueueRouter(operations, default, allocation, promotion_queue=preparation).publish(delivery)
    default.publish.assert_not_called()
    preparation.publish.assert_not_called()
    AllocationQueueRouter(operations, default, allocation, cutover_queue=queue).publish(delivery)
    queue.publish.assert_called_once_with(delivery)
    queue.receive.return_value = ReceivedOperation("receipt", delivery.message())

    def complete(*args, **kwargs):
        operations.get.return_value = replace(operation, status="succeeded")
        return True

    operations.complete.side_effect = complete
    consumer = SharedDatabaseConsumer(operations, queue, worker, kind=KIND)
    assert consumer.consume_once() == "succeeded"
    queue.delete.assert_called_once()


def test_revision_change_during_cutover_stops_before_next_external_step(flow):
    _, _, records, operations, _, _, traffic, sync, _, _ = flow

    def refresh(*args, **kwargs):
        records.revision += 1
        return sync.refresh.return_value

    sync.refresh.side_effect = refresh
    assert run(flow)["status"] == "needs_attention"
    assert traffic.closed
    operations.observe_external.assert_not_called()
