"""Compilation outputs must stay tied to the same source and decision."""

import copy

import pytest

from engine.application_ir import application_ir
from engine.architecture_decision import architecture_decision
from engine.compatibility import InfrastructureProfile, infrastructure_compatibility
from engine.compilation import compile_decision, verify_compilation
from engine.deployment_policy import deployment_policy


def test_compilation_binds_both_outputs_to_one_decision():
    profile = InfrastructureProfile("unconfirmed", (), 0)
    ir = application_ir(profile, "a" * 64).as_dict()
    policy = deployment_policy("local-docker", False)
    plan = {
        "target": "local-docker",
        "planner": "user",
        "resources": ["Docker image", "local container"],
        "compatibility": infrastructure_compatibility(profile, "local-docker", public_access=False),
    }
    decision = architecture_decision(ir, policy, plan).as_dict()
    record = compile_decision(decision, ir, policy, plan)
    assert record == compile_decision(decision, ir, policy, plan)
    for output in (record["source_patch_plan"], record["deployment_ir"], record["target_plan"]):
        assert output["compilation_id"] == record["compilation_id"]
        assert output["decision_revision"] == decision["decision_revision"]
        assert output["source_revision"] == ir["source_revision"]
    assert record["source_patch_plan"]["status"] == "pending_source_transform"
    assert record["source_patch_plan"]["changes"] == []
    verify_compilation(record, decision, ir, policy, plan)

    mixed = copy.deepcopy(record)
    mixed["target_plan"]["compilation_id"] = "comp-other"
    with pytest.raises(ValueError, match="Stored compilation"):
        verify_compilation(mixed, decision, ir, policy, plan)
    changed = copy.deepcopy(record)
    changed["source_patch_plan"]["changes"].append({"path": "server.js"})
    with pytest.raises(ValueError, match="Stored compilation"):
        verify_compilation(changed, decision, ir, policy, plan)
    plan["resources"].append("unplanned resource")
    with pytest.raises(ValueError, match="source and plan"):
        verify_compilation(record, decision, ir, policy, plan)
