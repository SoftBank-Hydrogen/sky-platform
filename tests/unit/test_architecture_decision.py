"""Architecture decisions preserve suitability, selection, and unresolved proof separately."""

import json

import pytest

from engine.application_ir import application_ir
from engine.architecture_decision import architecture_decision, verify_architecture_decision
from engine.compatibility import InfrastructureProfile, infrastructure_compatibility
from engine.deployment_policy import deployment_policy


def test_explicit_decision_is_source_bound_and_deterministic():
    profile = InfrastructureProfile("unconfirmed", (), 0)
    ir = application_ir(profile, "a" * 64).as_dict()
    policy = deployment_policy("local-docker", False)
    plan = {
        "target": "local-docker",
        "planner": "user",
        "compatibility": infrastructure_compatibility(profile, "local-docker", public_access=False),
    }
    decision = architecture_decision(ir, policy, plan)
    assert decision.source_revision == ir["source_revision"]
    assert decision.selected_candidate == "local-docker"
    assert decision.selection_mode == "fixed_target"
    assert decision.candidates[0].eligibility == "valid"
    assert decision.candidates[0].selection == "selected"
    assert decision.unresolved_evidence_ids == ()
    assert decision.decision_id == architecture_decision(ir, policy, plan).decision_id
    verify_architecture_decision(json.loads(json.dumps(decision.as_dict())), ir, policy, plan)
    with pytest.raises(ValueError, match="source and plan"):
        verify_architecture_decision(decision.as_dict(), ir, deployment_policy("local-docker", True), plan)
    with pytest.raises(ValueError, match="source and plan"):
        verify_architecture_decision(
            {**decision.as_dict(), "selected_candidate": "cloud-run"}, ir, policy, plan
        )
    plan["resources"] = ["unexpected-resource"]
    with pytest.raises(ValueError, match="source and plan"):
        verify_architecture_decision(decision.as_dict(), ir, policy, plan)
    plan.pop("resources")
    ir["unknowns"] = ["tampered"]
    with pytest.raises(ValueError, match="source and plan"):
        verify_architecture_decision(decision.as_dict(), ir, policy, plan)


def test_unknown_websocket_constraint_remains_pending_after_explicit_selection():
    profile = InfrastructureProfile(
        "unconfirmed", ("server.js",), 1, source_signals=(("websocket", ("server.js",)),)
    )
    ir = application_ir(profile, "b" * 64).as_dict()
    plan = {
        "target": "local-docker",
        "planner": "user",
        "compatibility": infrastructure_compatibility(profile, "local-docker", public_access=False),
    }
    decision = architecture_decision(ir, deployment_policy("local-docker", False), plan)
    assert decision.candidates[0].eligibility == "needs_review"
    assert decision.candidates[0].selection == "selected"
    assert decision.pending_verification_rule_ids == ("PROTOCOL-WS-01",)
    assert next(item for item in decision.constraints if item.rule_id == "PROTOCOL-WS-01").status == "unknown"


def test_auto_decision_cannot_select_rejected_or_unconfigured_candidate():
    profile = InfrastructureProfile("unconfirmed", (), 0)
    ir = application_ir(profile, "c" * 64).as_dict()
    policy = deployment_policy("auto", True)
    plan = {
        "target": "local-docker",
        "planner": "openai",
        "compatibility": infrastructure_compatibility(profile, "local-docker", public_access=True),
        "candidates": [
            {"id": "local-docker", "status": "eligible", "selected": True, "violated_rule_ids": []},
            {"id": "aws-ecs-express", "status": "requires_setup", "selected": False, "violated_rule_ids": []},
        ],
    }
    decision = architecture_decision(ir, policy, plan)
    assert decision.candidates[1].eligibility == "needs_review"
    assert decision.candidates[1].selection == "not_selected"
    assert decision.candidates[1].reason_codes == ("SETUP_REQUIRED",)
    plan["candidates"][0]["selected"] = False
    plan["candidates"][1]["selected"] = True
    with pytest.raises(ValueError, match="selection disagrees"):
        architecture_decision(ir, policy, plan)


def test_unmatched_constraint_evidence_is_recorded_as_unresolved():
    profile = InfrastructureProfile("unconfirmed", (), 0)
    ir = application_ir(profile, "d" * 64).as_dict()
    compatibility = infrastructure_compatibility(profile, "local-docker", public_access=False)
    compatibility["constraint_results"][0]["evidence_ids"] = ["E-" + "f" * 12]
    plan = {"target": "local-docker", "planner": "user", "compatibility": compatibility}
    decision = architecture_decision(ir, deployment_policy("local-docker", False), plan)
    assert decision.unresolved_evidence_ids == ("E-" + "f" * 12,)
