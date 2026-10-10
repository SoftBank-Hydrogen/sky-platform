"""Execution fencing and ambiguous preparation are tested without creating RDS."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest

from application.database_promotion_workflow import (
    KIND,
    PromotionPreparationAdmission,
    PromotionPreparationWorker,
)
from domain.access import Role
from ports.operations import ExecutionLease, Operation
from tests.unit.test_deployment_writes import principal
from tests.unit.test_operating_review import ID, NOW

pytest_plugins = ["tests.unit.test_operating_review"]


@pytest.fixture
def flow(setup):
    records, policy, reviews = setup
    proposal = reviews.review(principal(), ID, policy, now=NOW)
    approved = reviews.approve(principal(), ID, proposal["proposal_id"], policy, now=NOW)
    operations = Mock()
    admission = PromotionPreparationAdmission(records, operations, {ID: policy}, enabled=True, clock=lambda: NOW)
    admission.admit(principal(), ID, approved["proposal_id"], "prepare-one")
    command = operations.admit.call_args.args[3]
    operation = Operation(str(uuid4()), "game", KIND, "queued", str(uuid4()), command, {}, None,
                          False, 1, None, None)
    operations.get.return_value = operation
    operations.claim.return_value = ExecutionLease(operation.id, operation.attempt_id, "preparer", 1,
        datetime.now(UTC) + timedelta(minutes=15), "team")
    operations.begin_external.return_value = operations.observe_external.return_value = True
    operations.complete.return_value = True
    preparer = Mock()
    receipt = {"operation_id": operation.id, "plan_id": approved["plan"]["plan_id"],
               "binding": approved["plan"]["target"], "status": "available",
               "verified_scope": "rds_ownership_network_tls", "encrypted": True,
               "publicly_accessible": False, "resource_id": "db-" + "A" * 26,
               "password": "must-not-persist"}
    preparer.prepare.return_value = receipt
    resolver = Mock(return_value=principal())
    worker = PromotionPreparationWorker(records, operations, {ID: policy}, preparer, resolver,
                                       owner="preparer", clock=lambda: NOW)
    return worker, records, policy, operations, preparer, operation, receipt, resolver


def run(flow):
    worker, _, _, _, _, operation, _, _ = flow
    return worker.execute(operation.id, operation.attempt_id)


def test_preparation_has_durable_intent_and_never_claims_data_or_traffic_moved(flow):
    _, records, _, operations, preparer, _, _, _ = flow
    assert run(flow)["status"] == "succeeded"
    operations.begin_external.assert_called_once()
    preparer.prepare.assert_called_once()
    observed = operations.observe_external.call_args.args[1]
    result = operations.complete.call_args.args[1]
    assert "password" not in str(observed) and "must-not-persist" not in str(result)
    assert result["ready_to_cutover"] is False and result["promotion_status"] == "not_started"
    assert "copy" in result["remaining_stages"] and records.job["workload_database_binding"]["role"] == "shared_workload"


def test_admission_is_disabled_until_executor_and_transport_are_registered(flow):
    _, records, policy, operations, _, operation, _, _ = flow
    operations.admit.reset_mock()
    service = PromotionPreparationAdmission(records, operations, {ID: policy}, clock=lambda: NOW)
    with pytest.raises(ValueError, match="registered"):
        service.admit(principal(), ID, operation.command["proposal"]["proposal_id"], "two")
    operations.admit.assert_not_called()


@pytest.mark.parametrize("change", ["revision", "runtime", "approval", "member", "viewer", "app"])
def test_revalidation_blocks_external_effects(flow, change):
    _, records, _, operations, preparer, operation, _, resolver = flow
    if change == "revision":
        records.revision += 1
    elif change == "runtime":
        records.job["result"] = {"service": "new-service"}
    elif change == "approval":
        records.job["operating_proposal"]["approval"]["proposal_id"] = "changed"
    elif change == "member":
        resolver.return_value = None
    elif change == "viewer":
        resolver.return_value = principal(role=Role.VIEWER)
    else:
        operations.get.return_value = replace(operation, application_id="other")
    assert run(flow)["status"] == "failed"
    operations.begin_external.assert_not_called()
    preparer.prepare.assert_not_called()


def test_revoked_approver_is_checked_separately(flow):
    _, _, _, _, preparer, _, _, resolver = flow
    resolver.side_effect = [principal(), principal(role=Role.VIEWER)]
    assert run(flow)["status"] == "failed"
    preparer.prepare.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("status", "creating"), ("resource_id", "fake"), ("encrypted", False),
    ("publicly_accessible", True), ("operation_id", "other"), ("binding", {})])
def test_incomplete_or_foreign_target_receipts_require_attention(flow, field, value):
    _, _, _, operations, _, _, receipt, _ = flow
    receipt[field] = value
    assert run(flow)["status"] == "needs_attention"
    operations.observe_external.assert_not_called()
    operations.complete.assert_not_called()


def test_external_failure_never_retries_provisioning(flow):
    _, _, _, operations, preparer, _, _, _ = flow
    preparer.prepare.side_effect = OSError("creation result lost")
    assert run(flow)["status"] == "needs_attention"
    assert preparer.prepare.call_count == 1
    operations.interrupt.assert_called_once()


def test_observation_commit_failure_is_not_retried_or_falsely_completed(flow):
    _, _, _, operations, preparer, _, _, _ = flow
    operations.observe_external.side_effect = OSError("commit response lost")
    with pytest.raises(OSError):
        run(flow)
    assert preparer.prepare.call_count == 1
    operations.complete.assert_not_called()
    operations.interrupt.assert_not_called()


def test_recorded_receipt_resumes_without_recreating_target(flow):
    _, _, _, operations, preparer, operation, receipt, _ = flow
    intent = {"kind": KIND, "operation_id": operation.id,
              "plan_id": receipt["plan_id"], "target": receipt["binding"]}
    operations.get.return_value = replace(operation, external_intent=intent, external_receipt=receipt,
                                          checkpoint={"stage": "target_prepared"})
    assert run(flow)["status"] == "succeeded"
    preparer.prepare.assert_not_called()
    operations.begin_external.assert_not_called()


def test_uncertain_existing_intent_is_not_provisioned_again(flow):
    _, _, _, operations, preparer, operation, _, _ = flow
    operations.get.return_value = replace(operation, external_pending=True, external_intent={"kind": KIND})
    assert run(flow)["status"] == "needs_attention"
    preparer.prepare.assert_not_called()
    operations.complete.assert_not_called()


def test_changed_source_after_external_effect_keeps_reservation_for_reconciliation(flow):
    _, records, _, operations, preparer, operation, _, _ = flow
    records.revision += 1
    operations.get.return_value = replace(operation, external_pending=True, external_intent={"kind": KIND})
    assert run(flow)["status"] == "needs_attention"
    operations.complete.assert_not_called()
    preparer.prepare.assert_not_called()


def test_terminal_redelivery_never_claims_or_calls_adapter(flow):
    _, _, _, operations, preparer, operation, _, _ = flow
    operations.get.return_value = replace(operation, status="succeeded")
    assert run(flow)["status"] == "succeeded"
    operations.claim.assert_not_called()
    preparer.prepare.assert_not_called()


def test_lost_lease_prevents_external_effect(flow):
    _, _, _, operations, preparer, _, _, _ = flow
    operations.begin_external.return_value = False
    assert run(flow)["status"] == "lease_lost"
    preparer.prepare.assert_not_called()
