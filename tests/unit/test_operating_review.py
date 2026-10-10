"""Review is source/tenant/observation-bound and never grants execution rights."""

from copy import deepcopy
from dataclasses import asdict, replace

import pytest

from application.operating_review import (
    OperatingReview,
    ScheduledPromotionPolicy,
    deployment_observation_binding,
)
from domain.access import Role
from domain.database import DatabaseBinding
from ports.state import RecordConflict
from tests.unit.test_deployment_writes import Records, principal

NOW = "2026-10-11T00:00:00+00:00"
ID = "a" * 16


@pytest.fixture
def setup():
    source = DatabaseBinding("org1", "game", "shared_workload", "111111111111",
                             "ap-northeast-2", "pool", "game", "app-game")
    policy = ScheduledPromotionPolicy("after-30-days", "2026-10-10T00:00:00+00:00",
                                      replace(source, role="dedicated_workload", instance_id="dedicated"))
    records = Records()
    records.job.update(status="succeeded", deployment_state="active", source_digest="b" * 64,
                       result={"service": "owned-service", "image_digest": "sha256:" + "c" * 64},
                       plan={"source_digest": "d" * 64}, workload_database_binding=asdict(source))
    records.health = [{"healthy": True, "reason": "HTTP checked", "checked_at": NOW}]
    records.health[-1]["deployment_binding"] = deployment_observation_binding(records.job)
    records.health_revision = 1
    return records, policy, OperatingReview(records)


def test_due_review_is_persisted_without_deployment_mutation_and_is_idempotent(setup):
    records, policy, service = setup
    before = deepcopy(records.job)
    proposal = service.review(principal(), ID, policy, now=NOW)
    assert proposal["status"] == "pending_approval"
    assert proposal["plan"]["ready_to_cutover"] is False
    assert proposal["execution_status"] == "unsupported_by_sky"
    assert proposal["plan"]["shared_instance_policy"] == "retain"
    assert all(gate["status"] == "unverified" for gate in proposal["plan"]["required_gates"])
    assert {k: v for k, v in records.job.items() if k != "operating_proposal"} == before
    assert service.review(principal(), ID, policy, now=NOW) == proposal
    assert records.save_calls == 1


def test_approval_keeps_execution_blocked_and_duplicate_review_preserves_consent(setup):
    records, policy, service = setup
    proposal = service.review(principal(), ID, policy, now=NOW)
    approved = service.approve(principal(), ID, proposal["proposal_id"], policy, now=NOW)
    assert approved["approval"]["user_id"] == "user1"
    assert approved["execution_status"] == "unsupported_by_sky"
    assert service.review(principal(), ID, policy, now=NOW) == approved
    assert records.save_calls == 2


def test_before_schedule_no_proposal_is_written(setup):
    records, policy, service = setup
    policy = replace(policy, review_after="2026-10-12T00:00:00+00:00")
    assert service.review(principal(), ID, policy, now=NOW) is None
    assert records.save_calls == 0


@pytest.mark.parametrize("change", [
    {"healthy": False}, {"checked_at": "2026-10-10T23:54:59+00:00"},
    {"checked_at": "2026-10-11T00:00:01+00:00"}, {"checked_at": "invalid"},
    {"checked_at": "2026-10-11T00:00:00"}, {"reason": ""}])
def test_missing_stale_or_unhealthy_observations_block(setup, change):
    records, policy, service = setup
    records.health[-1].update(change)
    with pytest.raises(ValueError):
        service.review(principal(), ID, policy, now=NOW)
    assert records.save_calls == 0


@pytest.mark.parametrize("field,value", [
    ("status", "running"), ("deployment_state", "deleted"),
    ("release_rollback_state", "needs_attention"), ("source_digest", None),
    ("workload_database_binding", None), ("result", {})])
def test_incomplete_or_unsettled_bindings_fail_closed(setup, field, value):
    records, policy, service = setup
    records.job[field] = value
    with pytest.raises(ValueError):
        service.review(principal(), ID, policy, now=NOW)
    assert records.save_calls == 0


@pytest.mark.parametrize("field,value", [
    ("role", "sky_state"), ("application_id", "another"), ("organization_id", "another")])
def test_control_plane_and_foreign_workload_bindings_are_excluded(setup, field, value):
    records, policy, service = setup
    records.job["workload_database_binding"][field] = value
    with pytest.raises(ValueError):
        service.review(principal(), ID, policy, now=NOW)


def test_permissions_checked_for_review_and_approval(setup):
    records, policy, service = setup
    with pytest.raises(PermissionError):
        service.review(principal(role=Role.VIEWER), ID, policy, now=NOW)
    with pytest.raises(FileNotFoundError):
        service.review(principal(org="other"), ID, policy, now=NOW)
    assert records.save_calls == 0


@pytest.mark.parametrize("field,value", [
    ("result", {"service": "replacement"}), ("plan", {"source_digest": "e" * 64}),
    ("source_digest", "e" * 64)])
def test_changed_revision_or_runtime_invalidates_approval(setup, field, value):
    records, policy, service = setup
    proposal = service.review(principal(), ID, policy, now=NOW)
    records.job[field] = value
    with pytest.raises(ValueError):
        service.approve(principal(), ID, proposal["proposal_id"], policy, now=NOW)
    assert records.save_calls == 1


def test_new_observation_and_target_policy_invalidate_old_consent(setup):
    records, policy, service = setup
    proposal = service.review(principal(), ID, policy, now=NOW)
    records.health_revision += 1
    with pytest.raises(ValueError):
        service.approve(principal(), ID, proposal["proposal_id"], policy, now=NOW)
    fresh = service.review(principal(), ID, policy, now=NOW)
    policy = replace(policy, target=replace(policy.target, instance_id="another-dedicated"))
    with pytest.raises(ValueError):
        service.approve(principal(), ID, fresh["proposal_id"], policy, now=NOW)


def test_cas_conflict_does_not_commit_a_proposal(setup):
    records, policy, service = setup
    records.before_save = lambda store: setattr(store, "revision", store.revision + 1)
    with pytest.raises(RecordConflict):
        service.review(principal(), ID, policy, now=NOW)
    assert "operating_proposal" not in records.job


def test_uncertain_commit_is_not_retried_and_subsequent_review_recovers(setup):
    records, policy, service = setup
    records.uncertain = True
    with pytest.raises(OSError):
        service.review(principal(), ID, policy, now=NOW)
    records.uncertain = False
    assert service.review(principal(), ID, policy, now=NOW)["status"] == "pending_approval"
    assert records.save_calls == 1


def test_tampered_plan_is_not_approved(setup):
    records, policy, service = setup
    proposal = service.review(principal(), ID, policy, now=NOW)
    records.job["operating_proposal"]["plan"]["ready_to_cutover"] = True
    with pytest.raises(ValueError):
        service.approve(principal(), ID, proposal["proposal_id"], policy, now=NOW)


def test_websocket_policy_adds_session_drain_gate(setup):
    _, policy, service = setup
    proposal = service.review(principal(), ID, replace(policy, websocket_sessions=True), now=NOW)
    assert "websocket_session_drain" in {gate["name"] for gate in proposal["plan"]["required_gates"]}


def test_legacy_health_without_revision_binding_is_not_promotion_evidence(setup):
    records, policy, service = setup
    records.health[-1].pop("deployment_binding")
    with pytest.raises(ValueError, match="bound"):
        service.review(principal(), ID, policy, now=NOW)
    assert records.save_calls == 0


def test_next_review_keeps_previous_consent_in_history(setup):
    records, policy, service = setup
    proposal = service.review(principal(), ID, policy, now=NOW)
    approved = service.approve(principal(), ID, proposal["proposal_id"], policy, now=NOW)
    records.health_revision += 1
    next_proposal = service.review(principal(), ID, policy, now=NOW)
    assert next_proposal["status"] == "pending_approval"
    assert next_proposal["proposal_id"] != approved["proposal_id"]
    assert records.job["operating_proposal_history"] == [approved]
