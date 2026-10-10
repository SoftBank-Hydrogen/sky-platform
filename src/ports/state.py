"""Persistence boundary for deployment metadata, independent of files or databases.

Implementations return detached JSON snapshots and commit each write atomically.
Missing optional records return None; failed reads/writes raise OSError, and
malformed stored JSON raises ValueError. The legacy port has unconditional writes;
the separate versioned port provides per-record optimistic concurrency. Neither
provides leases, transactions across records, or source storage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class StoredJob:
    record: object
    modified_at: str
    revision: int | None = None


class DeploymentRecordStore(Protocol):
    def list_job_ids(self) -> tuple[str, ...]: ...

    def load_job(self, job_id: str) -> StoredJob: ...

    def save_job(self, job_id: str, record: dict) -> None: ...

    def load_health(self, job_id: str) -> object | None: ...

    def save_health(self, job_id: str, history: list[dict]) -> None: ...

    def load_github_sources(self) -> object | None: ...

    def save_github_sources(self, records: list[dict]) -> None: ...


class RecordConflict(ValueError):
    """The stored revision differs from the caller's snapshot; no write occurred."""


@dataclass(frozen=True)
class StoredRecord:
    record: object
    modified_at: str
    revision: int


class VersionedDeploymentRecordStore(Protocol):
    """Explicit compare-and-swap writes; None means create only, never overwrite.

    Separate from the legacy unconditional-write port. Callers must retain the
    revision alongside each detached snapshot and explicitly resolve conflicts.
    """

    def list_job_ids(self) -> tuple[str, ...]: ...

    def load_job(self, job_id: str) -> StoredJob: ...

    def save_job(self, job_id: str, record: dict, *, expected_revision: int | None = None) -> int: ...

    def load_health_record(self, job_id: str) -> StoredRecord | None: ...

    def save_health(
        self, job_id: str, history: list[dict], *, expected_revision: int | None = None
    ) -> int: ...

    def load_github_sources_record(self) -> StoredRecord | None: ...

    def save_github_sources(self, records: list[dict], *, expected_revision: int | None = None) -> int: ...
