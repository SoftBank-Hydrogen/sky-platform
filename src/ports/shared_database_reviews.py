"""Expiring, owner-bound reviews authorizing only a shared workload DB allocation."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from domain.access import Principal


class DatabaseReviewUnavailable(ValueError):
    """A reviewed choice expired, was revoked, changed or was consumed differently."""


@dataclass(frozen=True)
class SharedDatabaseReview:
    id: str
    expires_at: datetime
    job_revision: int
    selection: dict


@dataclass(frozen=True)
class AdmittedDatabaseAllocation:
    job_id: str
    operation_id: str
    attempt_id: str


class SharedDatabaseReviews(Protocol):
    def review(
        self, principal: Principal, job_id: str, *, connection_limit: int = 5, seconds: int = 900
    ) -> SharedDatabaseReview: ...

    def submit(
        self, principal: Principal, review_id: str, request_key: str
    ) -> AdmittedDatabaseAllocation: ...

    def revoke(self, principal: Principal, review_id: str) -> bool: ...

    def detail(self, principal: Principal, review_id: str) -> dict: ...
