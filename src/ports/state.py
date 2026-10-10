"""Persistence boundary for deployment metadata, independent of files or databases.

Implementations return detached JSON snapshots and commit each write atomically.
Missing optional records return None; failed reads/writes raise OSError, and
malformed stored JSON raises ValueError. This port does not provide leases,
transactions across records, source storage, or distributed concurrency control.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class StoredJob:
    record: object
    modified_at: str


class DeploymentRecordStore(Protocol):
    def list_job_ids(self) -> tuple[str, ...]: ...

    def load_job(self, job_id: str) -> StoredJob: ...

    def save_job(self, job_id: str, record: dict) -> None: ...

    def load_health(self, job_id: str) -> object | None: ...

    def save_health(self, job_id: str, history: list[dict]) -> None: ...

    def load_github_sources(self) -> object | None: ...

    def save_github_sources(self, records: list[dict]) -> None: ...
