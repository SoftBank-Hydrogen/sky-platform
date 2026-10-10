"""Persist an owned promotion proposal; approval never authorizes a cloud mutation.

This is an internal boundary for a trusted scheduler/operator. It reads recorded
health, not user-supplied metrics. Health and job writes are not one transaction;
an execution admission layer must revalidate both before acquiring a worker lease.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from application.deployment_writes import DeploymentMetadataWriter
from domain.database import DatabaseBinding
from engine.database_promotion import PromotionTrigger, plan_database_promotion


def _time(value):
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError()
        return stamp
    except (ValueError, TypeError):
        raise ValueError("A timezone-aware operating review time is required") from None


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def deployment_observation_binding(job):
    """Bind a probe to its snapshot, including prepared plan and runtime identity."""
    return {"job_id": job.get("id"), "application_id": job.get("application_id"),
            "source_revision": job.get("source_digest"), "runtime_digest": _digest(job.get("result")),
            "deployment_plan_digest": _digest(job.get("plan"))}


@dataclass(frozen=True)
class ScheduledPromotionPolicy:
    policy_id: str
    review_after: str
    target: DatabaseBinding
    max_health_age_seconds: int = 300
    websocket_sessions: bool = False

    def __post_init__(self):
        _time(self.review_after)
        # Reuse the promotion trigger's identifier validation.
        PromotionTrigger("scheduled_review", self.policy_id, "policy-validation", self.review_after)
        if not isinstance(self.target, DatabaseBinding) or self.target.role != "dedicated_workload":
            raise ValueError("Policy requires an explicit dedicated workload target")
        if type(self.max_health_age_seconds) is not int or not 1 <= self.max_health_age_seconds <= 3600:
            raise ValueError("Invalid health freshness bound")
        if type(self.websocket_sessions) is not bool:
            raise TypeError("WebSocket requirement must be explicit")


def _binding(job):
    if (job.get("status") != "succeeded" or job.get("deployment_state") != "active"
            or not isinstance(job.get("result"), dict) or not job["result"]
            or job.get("release_rollback_state") in {"running", "needs_attention"}):
        raise ValueError("Operating review requires an active, settled deployment")
    raw = job.get("workload_database_binding")
    if not isinstance(raw, dict):
        raise ValueError("A recorded workload database binding is required")  # noqa: TRY004 -- missing evidence
    source = DatabaseBinding(**raw)
    if (source.role != "shared_workload" or source.organization_id != job.get("organization_id")
            or source.application_id != job.get("application_id")):
        raise ValueError("Workload binding differs from deployment ownership")
    return source


class OperatingReview:
    def __init__(self, records):
        self.writer = DeploymentMetadataWriter(records)

    def _proposal(self, principal, job_id, policy, now):
        if not isinstance(policy, ScheduledPromotionPolicy):
            raise TypeError("An explicit operating policy is required")
        current_time = _time(now)
        snapshot = self.writer.snapshot(principal, job_id)
        job = snapshot.record
        source = _binding(job)
        if current_time < _time(policy.review_after):
            return snapshot, None
        health = self.writer.health_snapshot(principal, job_id)
        if health is None or not health.record:
            raise ValueError("A recorded health observation is required")
        observation = health.record[-1]
        if observation.get("deployment_binding") != deployment_observation_binding(job):
            raise ValueError("Health observation is not bound to the current deployment revision")
        checked = _time(observation.get("checked_at"))
        if (observation.get("healthy") is not True or not observation.get("reason")
                or checked > current_time
                or current_time - checked > timedelta(seconds=policy.max_health_age_seconds)):
            raise ValueError("A fresh healthy observation is required")
        evidence = {"job_id": job_id, "health_revision": health.revision,
                    "observation_digest": _digest(observation), "checked_at": observation["checked_at"]}
        trigger = PromotionTrigger("scheduled_review", policy.policy_id,
                                   "OBS-" + _digest(evidence)[:24], observation["checked_at"])
        plan = plan_database_promotion(source, policy.target, trigger,
                                       source_revision=job.get("source_digest"),
                                       websocket_sessions=policy.websocket_sessions)
        binding = {"job_id": job_id, "application_id": job["application_id"],
                   "organization_id": job["organization_id"], "source_revision": job["source_digest"],
                   "runtime_digest": _digest(job["result"]), "policy_digest": _digest(asdict(policy)),
                   "deployment_plan_digest": _digest(job.get("plan")),
                   "source": asdict(source), "health": evidence}
        document = {"schema_version": 1, "kind": "shared_to_dedicated_rds",
                    "binding": binding, "plan": plan}
        return snapshot, {**document, "proposal_id": "OPR-" + _digest(document)[:24],
                          "status": "pending_approval", "execution_status": "unsupported_by_sky"}

    def review(self, principal, job_id, policy, *, now):
        """Scheduled time is a review trigger, never a provisioning trigger."""
        snapshot, proposal = self._proposal(principal, job_id, policy, now)
        if proposal is None:
            return None
        previous = snapshot.record.get("operating_proposal")
        if isinstance(previous, dict) and previous.get("proposal_id") == proposal["proposal_id"]:
            # Do not turn an approved review back into an unapproved review.
            stable = {key: value for key, value in previous.items() if key not in {"status", "approval"}}
            expected = {key: value for key, value in proposal.items() if key != "status"}
            if stable != expected or previous.get("status") not in {"pending_approval", "approved"}:
                raise ValueError("Persisted operating proposal is inconsistent")
            return deepcopy(previous)
        changes = {"operating_proposal": proposal}
        if previous is not None:
            history = snapshot.record.get("operating_proposal_history", [])
            if (not isinstance(previous, dict) or not isinstance(history, list)
                    or any(not isinstance(item, dict) for item in history)):
                raise ValueError("Invalid persisted operating review history")
            changes["operating_proposal_history"] = (history + [deepcopy(previous)])[-20:]
        self.writer.patch(principal, job_id, snapshot.revision, changes)
        return deepcopy(proposal)

    def approve(self, principal, job_id, proposal_id, policy, *, now):
        """Record consent for this exact review, with no queue message or AWS call."""
        snapshot, candidate = self._proposal(principal, job_id, policy, now)
        previous = snapshot.record.get("operating_proposal")
        if (candidate is None or not isinstance(previous, dict)
                or candidate["proposal_id"] != proposal_id or previous != candidate):
            raise ValueError("Proposal changed, expired, or was already approved; review again")
        approved = {**candidate, "status": "approved", "approval": {
            "user_id": principal.user_id, "organization_id": principal.organization_id,
            "approved_at": now, "proposal_id": proposal_id}}
        self.writer.patch(principal, job_id, snapshot.revision, {"operating_proposal": approved})
        return deepcopy(approved)
