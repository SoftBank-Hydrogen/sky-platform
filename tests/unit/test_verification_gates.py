"""Static uncertainty must retain its destination and later evidence state."""

import copy

import pytest

from application.verification_gates import static_consistency_gate, target_verification_obligations


def test_websocket_unknown_is_carried_to_target_verification():
    decision = {
        "decision_id": "D-example",
        "decision_revision": 1,
        "source_revision": "a" * 64,
        "pending_verification_rule_ids": ["PROTOCOL-WS-01"],
    }
    compilation = {
        "architecture_decision_id": "D-example",
        "decision_revision": 1,
        "source_revision": "a" * 64,
        "compilation_id": "comp-example",
    }
    checks = [{"id": "CV-03", "status": "unknown", "source": "final_working_copy"}]
    gate = static_consistency_gate(compilation, decision, checks)
    assert gate["scope"] == "pre_execution_only"
    assert gate["unknown_checks"] == ["CV-03"]
    assert gate["required_obligations"] == [
        {
            "check_id": "PROTOCOL-WS-01",
            "due_gate": "target_verification",
            "status": "pending",
            "verification_refs": [],
        }
    ]
    assert target_verification_obligations(gate, None)[0]["status"] == "pending"
    assert target_verification_obligations(gate, {"status": "passed"})[0]["status"] == "pending"
    observed = {"status": "passed", "protocol": "sky.probe.v1", "checked_at": "2026-10-09T00:00:00Z"}
    projected = target_verification_obligations(gate, observed)
    assert projected[0]["status"] == "verified"
    assert projected[0]["verification_refs"] == ["websocket_verification"]
    assert gate["required_obligations"][0]["status"] == "pending"
    observed["status"] = "failed"
    assert target_verification_obligations(gate, observed)[0]["status"] == "failed"

    invalid = copy.deepcopy(compilation)
    invalid["decision_revision"] = 2
    with pytest.raises(ValueError, match="disagree"):
        static_consistency_gate(invalid, decision, checks)
    decision["pending_verification_rule_ids"] = ["UNROUTED-01"]
    with pytest.raises(ValueError, match="without a verification route"):
        static_consistency_gate(compilation, decision, checks)


def test_failed_or_empty_static_checks_cannot_approve_execution():
    decision = {
        "decision_id": "D-example",
        "decision_revision": 1,
        "source_revision": "a" * 64,
        "pending_verification_rule_ids": [],
    }
    compilation = {
        "architecture_decision_id": "D-example",
        "decision_revision": 1,
        "source_revision": "a" * 64,
        "compilation_id": "comp-example",
    }
    for checks in ([], [{"id": "CV-03", "status": "fail"}]):
        with pytest.raises(ValueError, match="valid consistency checks"):
            static_consistency_gate(compilation, decision, checks)


def test_unknown_port_check_requires_target_http_evidence():
    decision = {
        "decision_id": "D-example",
        "decision_revision": 1,
        "source_revision": "a" * 64,
        "pending_verification_rule_ids": [],
    }
    compilation = {
        "architecture_decision_id": "D-example",
        "decision_revision": 1,
        "source_revision": "a" * 64,
        "compilation_id": "comp-example",
    }
    checks = [{"id": "CV-06", "status": "unknown", "source": "executable_dockerfile"}]
    gate = static_consistency_gate(compilation, decision, checks)
    assert gate["unknown_checks"] == ["CV-06"]
    assert gate["required_obligations"] == [
        {
            "check_id": "CV-06",
            "due_gate": "target_verification",
            "status": "pending",
            "verification_refs": [],
        }
    ]
    assert target_verification_obligations(gate, None)[0]["status"] == "pending"
    projected = target_verification_obligations(gate, None, deployment_http_verified=True)
    assert projected[0]["status"] == "verified"
    assert projected[0]["verification_refs"] == ["deployment_http"]
    assert gate["required_obligations"][0]["status"] == "pending"


def test_websocket_state_uncertainty_survives_a_successful_handshake():
    decision = {
        "decision_id": "D-example",
        "decision_revision": 1,
        "source_revision": "a" * 64,
        "pending_verification_rule_ids": ["PROTOCOL-WS-01"],
    }
    compilation = {
        "architecture_decision_id": "D-example",
        "decision_revision": 1,
        "source_revision": "a" * 64,
        "compilation_id": "comp-example",
    }
    checks = [{"id": "CV-08", "status": "unknown", "source": "websocket_state_and_replica_plan"}]
    compilation["deployment_ir"] = {"unknowns": ["session_affinity_behavior"]}
    with pytest.raises(ValueError, match="requires CV-08"):
        static_consistency_gate(compilation, decision, [{"id": "CV-06", "status": "unknown"}])
    gate = static_consistency_gate(compilation, decision, checks)
    assert [item["check_id"] for item in gate["required_obligations"]] == ["PROTOCOL-WS-01", "CV-08"]
    observed = {"status": "passed", "protocol": "sky.probe.v1", "checked_at": "2026-10-09T00:00:00Z"}
    obligations = target_verification_obligations(gate, observed, deployment_http_verified=True)
    assert [item["status"] for item in obligations] == ["verified", "pending"]
