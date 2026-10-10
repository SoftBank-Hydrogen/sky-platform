"""Legacy response projections over owned DB snapshots, not an HTTP handler.

Callers supply an authenticated Principal. This does not verify an ALB JWT.
Pagination is explicit; a future HTTP adapter must choose its envelope/version.
"""

from copy import deepcopy
from dataclasses import dataclass

from application.diagnosis import deployment_diagnosis
from domain.access import Action, Principal, ResourceOwner, owner_from_record, permitted
from ports.deployment_reads import DeploymentReads, ReadCursor


@dataclass(frozen=True)
class DeploymentPage:
    items: tuple[dict, ...]
    next_cursor: ReadCursor | None


def _organization(principal):
    if not isinstance(principal, Principal) or not permitted(
        principal, Action.READ, ResourceOwner(principal.organization_id, principal.user_id)
    ):
        raise PermissionError("Deployment read access denied")
    return principal.organization_id


def _owned(principal, snapshot):
    if not permitted(principal, Action.READ, owner_from_record(snapshot.job)):
        raise FileNotFoundError("Deployment not found")
    return snapshot


class DeploymentReadService:
    def __init__(self, reads: DeploymentReads):
        self.reads = reads

    def _detail(self, principal, job_id):
        snapshot = self.reads.detail(_organization(principal), job_id)
        if snapshot is None:
            raise FileNotFoundError("Deployment not found")
        return _owned(principal, snapshot)

    def detail(self, principal, job_id):
        snapshot = self._detail(principal, job_id)
        job, health = snapshot.job, snapshot.health
        return deepcopy(
            {
                **job,
                "diagnosis": deployment_diagnosis(job),
                "health_history": health,
                "last_health": health[-1] if health else None,
                "monitor_error": job.get("monitor_error"),
            }
        )

    def history(self, principal, job_id):
        return deepcopy(self._detail(principal, job_id).health)

    def summaries(self, principal, *, limit=50, cursor=None):
        page = self.reads.page(_organization(principal), limit=limit, cursor=cursor)
        items = []
        for snapshot in page.items:
            job = _owned(principal, snapshot).job
            items.append(
                {
                    "id": job["id"],
                    "status": job["status"],
                    "created_at": job.get("created_at"),
                    "application_id": job.get("application_id", job["id"]),
                    "deployment_state": job.get("deployment_state", "active"),
                    "release_rollback_state": job.get("release_rollback_state"),
                    "analyzer": (job.get("plan") or {}).get("analyzer", job.get("mode", "static")),
                    "result": job.get("result"),
                    "last_health": snapshot.health[-1] if snapshot.health else None,
                    "monitor_error": job.get("monitor_error"),
                }
            )
        return DeploymentPage(tuple(deepcopy(items)), page.next_cursor)

    def releases(self, principal, application_id, *, limit=50, cursor=None):
        page = self.reads.page(
            _organization(principal), application_id=application_id, limit=limit, cursor=cursor
        )
        items = []
        for snapshot in page.items:
            job = _owned(principal, snapshot).job
            items.append(
                {
                    "id": job["id"],
                    "status": job["status"],
                    "target": job.get("target", "local-docker"),
                    "created_at": job.get("created_at"),
                    "result": job.get("result"),
                    "deployment_state": job.get("deployment_state", "active"),
                    "release_rollback_state": job.get("release_rollback_state"),
                }
            )
        return DeploymentPage(tuple(deepcopy(items)), page.next_cursor)
