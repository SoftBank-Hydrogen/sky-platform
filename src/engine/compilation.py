"""Bind source-change intent and target configuration to one architecture decision."""

from __future__ import annotations

import hashlib
import json

from engine.architecture_decision import verify_architecture_decision
from engine.deployment_policy import DeploymentPolicy
from engine.target_lowering import lower_target_configuration


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _compile_decision(
    decision: dict, ir: dict, policy: DeploymentPolicy, infrastructure_plan: dict, *, legacy: bool
) -> dict:
    """Compile the known requirements; source edits remain pending until applied."""
    verify_architecture_decision(decision, ir, policy, infrastructure_plan)
    compilation_id = "comp-" + _digest(decision)[:16]
    revision = decision["decision_revision"]
    source_revision = decision["source_revision"]
    common = {
        "compilation_id": compilation_id,
        "decision_revision": revision,
        "source_revision": source_revision,
    }
    patch_plan = {
        **common,
        "id": "patch-" + compilation_id[5:],
        "status": "pending_source_transform",
        "changes": [],
        "checks": ["changed_file_allowlist", "no_secrets_written", "executable_build"],
    }
    deployment_ir = {
        **common,
        "id": "depir-" + compilation_id[5:],
        "services": [{"id": "source-bundle", "kind": "container_service", "replicas": 1}],
        "requirements": ir.get("requirements", []),
        "topology_status": ir.get("topology_status", "unresolved"),
        "unknowns": ir.get("unknowns", []),
    }
    target_plan = {
        **common,
        "id": "target-" + compilation_id[5:],
        "target": decision["selected_candidate"],
        "resources": infrastructure_plan.get("resources", []),
        "access_mode": infrastructure_plan["compatibility"]["access_mode"],
        "infrastructure_plan_digest": decision["target_plan_digest"],
    }
    if not legacy:
        target_plan["execution_configuration"] = lower_target_configuration(
            infrastructure_plan, deployment_ir
        )
    return {
        **common,
        **({} if legacy else {"schema_version": 2}),
        "architecture_decision_id": decision["decision_id"],
        "source_patch_plan": patch_plan,
        "deployment_ir": deployment_ir,
        "target_plan": target_plan,
    }


def compile_decision(decision: dict, ir: dict, policy: DeploymentPolicy, infrastructure_plan: dict) -> dict:
    """Compile a versioned plan with explicit, capability-checked target settings."""
    return _compile_decision(decision, ir, policy, infrastructure_plan, legacy=False)


def verify_compilation(
    record: dict, decision: dict, ir: dict, policy: DeploymentPolicy, infrastructure_plan: dict
) -> None:
    """Reject missing, mixed, or edited compilation outputs before adapter use."""
    legacy = isinstance(record, dict) and "schema_version" not in record
    expected = _compile_decision(decision, ir, policy, infrastructure_plan, legacy=legacy)
    if not isinstance(record, dict) or _digest(record) != _digest(expected):
        raise ValueError("Stored compilation does not match its architecture decision")
