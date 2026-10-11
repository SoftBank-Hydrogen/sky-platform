"""Read-only runtime proposals grounded in recorded deployment observations.

This layer never changes a deployment or upgrades a failed probe into a verified
root cause. A controller may offer the proposal for human review later.
"""

from __future__ import annotations

import hashlib


def runtime_proposal(job: dict, health_history: list[dict]) -> dict | None:
    """Suggest investigation after three consecutive failed health observations.

    Three observations avoid treating a single transient probe as a deployment
    failure. The observation timestamps are evidence references, not a diagnosis.
    """
    if (
        job.get("status") != "succeeded"
        or job.get("deployment_state", "active") != "active"
        or job.get("release_rollback_state") in {"running", "needs_attention"}
        or not isinstance(job.get("result"), dict)
        or not isinstance(health_history, list)
    ):
        return None
    recent = health_history[-3:]
    if len(recent) != 3 or any(
        not isinstance(item, dict)
        or item.get("healthy") is not False
        or not isinstance(item.get("checked_at"), str)
        or not item["checked_at"]
        for item in recent
    ):
        return None
    job_id = job.get("id")
    if not isinstance(job_id, str) or not job_id:
        return None
    observed_at = [item["checked_at"] for item in recent]
    fingerprint = hashlib.sha256((job_id + "\0" + "\0".join(observed_at)).encode()).hexdigest()[:16]
    return {
        "id": f"RP-{fingerprint}",
        "job_id": job_id,
        "kind": "investigate_runtime_health",
        "state": "proposed",
        "desired_state": "healthy",
        "observed_state": "three_consecutive_failed_probes",
        "evidence": [
            {"source": "health_history", "checked_at": timestamp, "healthy": False}
            for timestamp in observed_at
        ],
        "root_cause_verified": False,
        "recommended_action": "대상의 실제 상태와 최근 변경을 확인한 뒤 재배포·재컴파일 필요 여부를 검토하세요.",
        "execution": "none",
        "approval_required": True,
    }
