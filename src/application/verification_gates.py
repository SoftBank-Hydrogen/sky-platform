"""Carry unresolved static checks to the gate that can actually observe them."""

from __future__ import annotations

import copy


_TARGET_RULES = {"PROTOCOL-WS-01"}


def static_consistency_gate(compilation: dict, decision: dict, checks: list[dict]) -> dict:
    """Approve execution only with an explicit destination for known obligations."""
    if (
        compilation.get("architecture_decision_id") != decision.get("decision_id")
        or compilation.get("decision_revision") != decision.get("decision_revision")
        or compilation.get("source_revision") != decision.get("source_revision")
    ):
        raise ValueError("Static gate compilation and architecture decision disagree")
    if (
        not isinstance(checks, list)
        or not checks
        or any(
            not isinstance(item, dict)
            or item.get("status") not in {"pass", "unknown"}
            or not isinstance(item.get("id"), str)
            for item in checks
        )
    ):
        raise ValueError("Static gate requires valid consistency checks")
    deployment_ir = compilation.get("deployment_ir")
    if (isinstance(deployment_ir, dict)
            and "session_affinity_behavior" in (deployment_ir.get("unknowns") or ())
            and not any(item.get("id") == "CV-08" and item.get("status") == "unknown" for item in checks)):
        raise ValueError("Static gate requires CV-08 WebSocket state uncertainty")
    pending = decision.get("pending_verification_rule_ids")
    if not isinstance(pending, (list, tuple)) or any(rule not in _TARGET_RULES for rule in pending):
        raise ValueError("Static gate has an unresolved rule without a verification route")
    obligations = [
        {"check_id": rule, "due_gate": "target_verification", "status": "pending", "verification_refs": []}
        for rule in sorted(set(pending))
    ]
    if any(item["id"] == "CV-06" and item["status"] == "unknown" for item in checks):
        obligations.append({"check_id": "CV-06", "due_gate": "target_verification",
                            "status": "pending", "verification_refs": []})
    if any(item["id"] == "CV-08" and item["status"] == "unknown" for item in checks):
        # A single WebSocket handshake cannot prove session continuity after
        # task replacement or scale-out. Keep this visible until such a probe exists.
        obligations.append({"check_id": "CV-08", "due_gate": "target_verification",
                            "status": "pending", "verification_refs": []})
    return {
        "schema_version": 1,
        "compilation_id": compilation["compilation_id"],
        "decision_revision": compilation["decision_revision"],
        "decision": "approved",
        "scope": "pre_execution_only",
        "consistency_checks": copy.deepcopy(checks),
        "unknown_checks": sorted(item["id"] for item in checks if item["status"] == "unknown"),
        "required_obligations": obligations,
    }


def target_verification_obligations(
    gate: dict, websocket: dict | None, *, deployment_http_verified: bool = False
) -> list[dict]:
    """Project later observations without rewriting the original static decision."""
    if not isinstance(gate, dict):
        return []
    obligations = gate.get("required_obligations")
    if not isinstance(obligations, list):
        return []
    result = copy.deepcopy(obligations)
    for item in result:
        if item.get("check_id") == "CV-06":
            if deployment_http_verified:
                item["status"] = "verified"
                item["verification_refs"] = ["deployment_http"]
            continue
        if item.get("check_id") != "PROTOCOL-WS-01" or not isinstance(websocket, dict):
            continue
        if websocket.get("protocol") != "sky.probe.v1" or not isinstance(websocket.get("checked_at"), str):
            continue
        if websocket.get("status") == "passed":
            item["status"] = "verified"
            item["verification_refs"] = ["websocket_verification"]
        elif websocket.get("status") == "failed":
            item["status"] = "failed"
            item["verification_refs"] = ["websocket_verification"]
    return result
