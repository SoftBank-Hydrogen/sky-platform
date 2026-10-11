"""A source-bound record of the chosen architecture, not a deployment approval."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass

from engine.backend_identity import backend_identity
from engine.deployment_policy import DeploymentPolicy

_ELIGIBILITY = {
    "eligible": "valid",
    "rejected": "rejected",
    "needs_review": "needs_review",
    "requires_setup": "needs_review",
    "requires_database_binding": "needs_review",
    "unsupported_by_sky": "unsupported_by_sky",
}
_CONSTRAINT = {"satisfied": "pass", "violated": "fail", "unknown": "unknown"}


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class CandidateAssessment:
    target: str
    eligibility: str
    selection: str
    reason_codes: tuple[str, ...]
    prior_status: str


@dataclass(frozen=True)
class ConstraintAssessment:
    rule_id: str
    status: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class ArchitectureDecision:
    schema_version: int
    decision_revision: int
    decision_id: str
    source_revision: str
    application_ir_digest: str
    policy_digest: str
    target_plan_digest: str
    selection_mode: str
    selected_candidate: str
    selection_basis: str
    candidates: tuple[CandidateAssessment, ...]
    constraints: tuple[ConstraintAssessment, ...]
    unresolved_evidence_ids: tuple[str, ...]
    pending_verification_rule_ids: tuple[str, ...]

    def as_dict(self) -> dict:
        return asdict(self)


def architecture_decision(ir: dict, policy: DeploymentPolicy, plan: dict) -> ArchitectureDecision:
    """Normalize the existing evaluated plan without inventing costs or capability proof."""
    revision = ir.get("source_revision") if isinstance(ir, dict) else None
    if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{64}", revision):
        raise ValueError("Architecture decision requires a source-bound IR")
    target = plan.get("target") if isinstance(plan, dict) else None
    compatibility = plan.get("compatibility") if isinstance(plan, dict) else None
    if not isinstance(compatibility, dict) or compatibility.get("target") != target:
        raise ValueError("Architecture decision target and compatibility disagree")
    policy.require(target, compatibility.get("access_mode"))
    if compatibility.get("compatible") is not True:
        raise ValueError("Architecture decision cannot select an incompatible target")

    source_ids = {
        item["id"]
        for item in ir.get("evidence", ())
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and isinstance(item.get("source"), dict)
        and item["source"].get("revision") == revision
    }
    constraints = []
    for item in compatibility.get("constraint_results", ()):
        if (
            not isinstance(item, dict)
            or item.get("status") not in _CONSTRAINT
            or not isinstance(item.get("rule_id"), str)
        ):
            raise ValueError("Invalid architecture constraint result")
        status = _CONSTRAINT[item["status"]]
        if status == "fail":
            raise ValueError("Architecture decision cannot select a failed hard constraint")
        identifiers = item.get("evidence_ids", ())
        if not isinstance(identifiers, (list, tuple)) or any(not isinstance(ref, str) for ref in identifiers):
            raise ValueError("Invalid architecture constraint evidence")
        constraints.append(ConstraintAssessment(item["rule_id"], status, tuple(sorted(set(identifiers)))))

    raw_candidates = plan.get("candidates")
    evaluated_candidates = raw_candidates is not None
    if raw_candidates is None:
        status = "needs_review" if any(item.status == "unknown" for item in constraints) else "eligible"
        raw_candidates = [
            {
                "id": target,
                "status": status,
                "selected": True,
                "reason_codes": [item.rule_id for item in constraints if item.status == "unknown"],
            }
        ]
    if not isinstance(raw_candidates, (list, tuple)) or not raw_candidates:
        raise ValueError("Architecture decision requires evaluated candidates")
    options = plan.get("architecture_options", ())
    if not isinstance(options, (list, tuple)) or any(not isinstance(item, dict) for item in options):
        raise ValueError("Invalid architecture options")
    for item in options:
        if (
            not isinstance(item, dict)
            or item.get("status") != "unsupported_by_sky"
            or item.get("selected") is not False
            or backend_identity(item.get("id")).sky_adapter_support != "unimplemented"
        ):
            raise ValueError("Architecture options cannot be selected for execution")
    raw_candidates = [*raw_candidates, *options]
    candidates = []
    for item in raw_candidates:
        if (
            not isinstance(item, dict)
            or item.get("status") not in _ELIGIBILITY
            or not isinstance(item.get("id"), str)
        ):
            raise ValueError("Invalid architecture candidate")
        codes = item.get("reason_codes", item.get("violated_rule_ids", ()))
        if not isinstance(codes, (list, tuple)) or any(not isinstance(code, str) for code in codes):
            raise ValueError("Invalid architecture rejection reasons")
        reason_codes = set(codes)
        if item["status"] == "requires_setup":
            reason_codes.add("SETUP_REQUIRED")
        if item["status"] == "requires_database_binding":
            reason_codes.add("DATABASE_BINDING_REQUIRED")
        candidates.append(
            CandidateAssessment(
                target=item["id"],
                eligibility=_ELIGIBILITY[item["status"]],
                selection="selected" if item.get("selected") is True else "not_selected",
                reason_codes=tuple(sorted(reason_codes)),
                prior_status=item["status"],
            )
        )
    selected = [item for item in candidates if item.selection == "selected"]
    if (
        len(selected) != 1
        or selected[0].target != target
        or selected[0].eligibility in {"rejected", "unsupported_by_sky"}
        or len({item.target for item in candidates}) != len(candidates)
    ):
        raise ValueError("Architecture decision selection disagrees with the chosen target")
    if policy.selection_mode == "auto_target" and evaluated_candidates and selected[0].eligibility != "valid":
        raise ValueError("Automatic selection requires an eligible candidate")
    if selected[0].eligibility == "valid" and any(item.status == "unknown" for item in constraints):
        raise ValueError("Unknown selected constraints require review")

    unresolved = tuple(
        sorted({ref for item in constraints for ref in item.evidence_ids if ref not in source_ids})
    )
    pending = tuple(sorted({item.rule_id for item in constraints if item.status == "unknown"}))
    basis = plan.get("planner")
    if not isinstance(basis, str) or not basis:
        raise ValueError("Architecture decision requires a selection basis")
    record = {
        "schema_version": 1,
        "decision_revision": 1,
        "source_revision": revision,
        "application_ir_digest": _digest(ir),
        "policy_digest": _digest(policy.as_dict()),
        "target_plan_digest": _digest(plan),
        "selection_mode": policy.selection_mode,
        "selected_candidate": target,
        "selection_basis": basis,
        "candidates": tuple(candidates),
        "constraints": tuple(constraints),
        "unresolved_evidence_ids": unresolved,
        "pending_verification_rule_ids": pending,
    }
    payload = {
        **record,
        "candidates": [asdict(item) for item in candidates],
        "constraints": [asdict(item) for item in constraints],
    }
    identifier = _digest(payload)[:16]
    return ArchitectureDecision(decision_id="D-" + identifier, **record)


def verify_architecture_decision(record: dict, ir: dict, policy: DeploymentPolicy, plan: dict) -> None:
    """Reject a changed decision, policy, or plan before versioned job execution."""
    expected = architecture_decision(ir, policy, plan).as_dict()
    if (
        not isinstance(record, dict)
        or set(record) != set(expected)
        or json.dumps(record, sort_keys=True) != json.dumps(expected, sort_keys=True)
    ):
        raise ValueError("Stored architecture decision does not match its source and plan")
