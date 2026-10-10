"""Runtime feedback must remain a reviewable observation, never an action."""

import pytest

from application.deployment_reads import DeploymentReadService
from domain.access import LoginSource, Principal, Role
from engine.runtime_proposals import runtime_proposal
from ports.deployment_reads import DeploymentSnapshot, SnapshotPage


def _job(**changes):
    return {
        "id": "job-1",
        "status": "succeeded",
        "deployment_state": "active",
        "result": {"url": "https://example.test"},
        **changes,
    }


def _history(*health):
    return [
        {"checked_at": f"2026-10-10T00:00:0{i}Z", "healthy": value, "reason": "secret=do-not-echo"}
        for i, value in enumerate(health)
    ]


def test_repeated_failures_propose_review_without_claiming_cause_or_exposing_probe_output():
    proposal = runtime_proposal(_job(), _history(False, False, False))

    assert proposal["kind"] == "investigate_runtime_health"
    assert proposal["observed_state"] == "three_consecutive_failed_probes"
    assert proposal["root_cause_verified"] is False
    assert proposal["execution"] == "none"
    assert proposal["approval_required"] is True
    assert "do-not-echo" not in str(proposal)
    assert proposal == runtime_proposal(_job(), _history(False, False, False))


def test_recovery_and_non_active_deployments_have_no_proposal():
    assert runtime_proposal(_job(), _history(False, False, True)) is None
    assert runtime_proposal(_job(), _history(False, False)) is None
    assert runtime_proposal(_job(deployment_state="superseded"), _history(False, False, False)) is None
    assert (
        runtime_proposal(_job(release_rollback_state="needs_attention"), _history(False, False, False))
        is None
    )


def test_hosted_read_exposes_proposal_only_to_owning_organization():
    job = {**_job(), "organization_id": "team-a", "created_by": "alice"}
    snapshot = DeploymentSnapshot(job, _history(False, False, False), 1)

    class Reads:
        def detail(self, organization_id, job_id):
            return snapshot if job_id == "job-1" else None

        def page(self, organization_id, **kwargs):
            return SnapshotPage((snapshot,), None)

    service = DeploymentReadService(Reads())
    owner = Principal("alice", "team-a", Role.VIEWER, LoginSource.LOCAL)
    outsider = Principal("bob", "team-b", Role.VIEWER, LoginSource.LOCAL)

    assert service.detail(owner, "job-1")["runtime_proposal"]["execution"] == "none"
    assert service.summaries(owner).items[0]["runtime_proposal"]["approval_required"] is True
    with pytest.raises(FileNotFoundError):
        service.detail(outsider, "job-1")
