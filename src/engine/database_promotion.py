"""Plan an app database move without claiming that data or traffic has moved.

The Sky control-plane database is deliberately excluded. A plan is a review
artifact; provisioning, copying, cutover, and rollback require separate proofs.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_ROLES = {"shared_workload", "dedicated_workload", "sky_state"}
_TRIGGERS = {"scheduled_review", "capacity_review", "isolation_review"}
_GATES = (
    "source_inventory",
    "tenant_isolation",
    "source_snapshot",
    "target_provisioned",
    "schema_compatibility",
    "initial_copy_integrity",
    "continuous_sync_caught_up",
    "single_writer_routing",
    "cutover_probe",
    "rollback_window",
    "reverse_sync_or_write_freeze",
)


@dataclass(frozen=True)
class DatabaseBinding:
    organization_id: str
    application_id: str
    role: str
    account_id: str
    region: str
    instance_id: str
    database_name: str
    owner_ref: str

    def __post_init__(self) -> None:
        for name in (
            "organization_id",
            "application_id",
            "region",
            "instance_id",
            "database_name",
            "owner_ref",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise ValueError(f"Invalid database binding {name}")
        if (
            not isinstance(self.role, str)
            or self.role not in _ROLES
            or not isinstance(self.account_id, str)
            or not re.fullmatch(r"[0-9]{12}", self.account_id)
        ):
            raise ValueError("Invalid database binding role or account")


@dataclass(frozen=True)
class PromotionTrigger:
    kind: str
    policy_id: str
    evidence_ref: str
    observed_at: str

    def __post_init__(self) -> None:
        if self.kind not in _TRIGGERS or not all(
            isinstance(value, str) and _IDENTIFIER.fullmatch(value)
            for value in (self.policy_id, self.evidence_ref)
        ):
            raise ValueError("Promotion requires a policy and a recorded trigger")
        if not isinstance(self.observed_at, str):
            raise TypeError("Invalid promotion observation time")
        try:
            observed = datetime.fromisoformat(self.observed_at)
        except ValueError as exc:
            raise ValueError("Invalid promotion observation time") from exc
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise ValueError("Promotion observation time must include a timezone")


def plan_database_promotion(
    source: DatabaseBinding,
    target: DatabaseBinding,
    trigger: PromotionTrigger,
    *,
    source_revision: str,
    websocket_sessions: bool = False,
) -> dict:
    """Return a stable, blocked migration plan tied to one app and source revision."""
    if not isinstance(source, DatabaseBinding) or not isinstance(target, DatabaseBinding):
        raise TypeError("Database promotion requires typed source and target bindings")
    if not isinstance(trigger, PromotionTrigger):
        raise TypeError("Database promotion requires a recorded trigger")
    if source.role != "shared_workload" or target.role != "dedicated_workload":
        raise ValueError("Only workload shared-to-dedicated promotion can be planned")
    if (source.organization_id, source.application_id) != (target.organization_id, target.application_id):
        raise ValueError("Promotion cannot cross organization or application ownership")
    if (source.account_id, source.region) != (target.account_id, target.region):
        raise ValueError("Cross-account or cross-region migration is not implemented")
    if source.instance_id == target.instance_id:
        raise ValueError("Dedicated RDS must be a different instance")
    if not isinstance(source_revision, str) or not re.fullmatch(r"[a-f0-9]{64}", source_revision):
        raise ValueError("Promotion requires a source revision digest")
    if type(websocket_sessions) is not bool:
        raise TypeError("WebSocket session requirement must be explicit")

    identity = json.dumps(
        [
            source.organization_id,
            source.application_id,
            source.instance_id,
            source.database_name,
            target.instance_id,
            target.database_name,
            source_revision,
            trigger.policy_id,
            trigger.evidence_ref,
            trigger.observed_at,
        ],
        separators=(",", ":"),
    )
    gates = (*_GATES, "websocket_session_drain" if websocket_sessions else "")
    required_gates = [name for name in gates if name]
    return {
        "schema_version": 1,
        "plan_id": "DBP-" + hashlib.sha256(identity.encode()).hexdigest()[:16],
        "application_id": source.application_id,
        "organization_id": source.organization_id,
        "source_revision": source_revision,
        "source": asdict(source),
        "target": asdict(target),
        "trigger": asdict(trigger),
        "stages": [
            "provision",
            "snapshot",
            "copy",
            "catch_up",
            "verify",
            "cutover",
            "observe",
            "retire_source",
        ],
        "required_gates": [{"name": name, "status": "unverified"} for name in required_gates],
        "strategy_candidates": [
            {
                "name": "blue_green",
                "status": "blocked",
                "additional_gates": ["new_version_health", "atomic_write_cutover"],
                "reason": "Single-writer cutover and rollback are unverified",
            },
            {
                "name": "read_only_canary",
                "status": "blocked",
                "additional_gates": ["tenant_sticky_routing", "read_only_routing", "error_metrics"],
                "reason": "Read routing, sync, and metrics are unverified; writes cannot be split",
            },
        ],
        "execution_status": "unsupported_by_sky",
        "approval_required": True,
        "cost_estimate": None,
        "source_retirement_status": "blocked_until_rollback_window_closes",
        "ready_to_cutover": False,
    }
