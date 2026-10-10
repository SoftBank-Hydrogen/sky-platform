"""Read snapshots, independent of metadata mutations and operation admission."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ReadCursor:
    organization_id: str
    application_id: str | None
    created_at: str
    record_id: str


@dataclass(frozen=True)
class DeploymentSnapshot:
    job: dict
    health: list
    revision: int


@dataclass(frozen=True)
class SnapshotPage:
    items: tuple[DeploymentSnapshot, ...]
    next_cursor: ReadCursor | None


class DeploymentReads(Protocol):
    def detail(self, organization_id: str, job_id: str) -> DeploymentSnapshot | None: ...
    def page(
        self,
        organization_id: str,
        *,
        application_id: str | None = None,
        limit: int = 50,
        cursor: ReadCursor | None = None,
    ) -> SnapshotPage: ...
