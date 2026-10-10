"""Durable operation admission and execution ownership, separate from metadata.

Callers must validate source/plan/approval and account scope before admission.
Commands contain immutable references, never uploaded files or credential values.
Database ownership does not fence AWS: workers must use external idempotency keys
and reconcile uncertain effects before requesting them again.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class IdempotencyConflict(ValueError):
    """A request key was already used for a different immutable command."""


class ApplicationBusy(ValueError):
    """An unfinished operation owns this application's mutation scope."""


@dataclass(frozen=True)
class Operation:
    id: str
    application_id: str
    kind: str
    status: str
    attempt_id: str
    command: dict
    checkpoint: dict
    result: dict | None
    external_pending: bool
    row_version: int
    external_intent: dict | None
    external_receipt: dict | None


@dataclass(frozen=True)
class ExecutionLease:
    operation_id: str
    attempt_id: str
    owner: str
    epoch: int
    expires_at: datetime
    workspace: str


@dataclass(frozen=True)
class OutboxDelivery:
    id: str
    owner: str
    epoch: int
    operation_id: str
    attempt_id: str
    application_id: str
    workspace: str

    def message(self) -> dict:
        return {
            "version": 1,
            "workspace": self.workspace,
            "operation_id": self.operation_id,
            "attempt_id": self.attempt_id,
            "application_id": self.application_id,
        }


@dataclass(frozen=True)
class FailedOutbox:
    id: str
    operation_id: str
    attempt_id: str
    application_id: str
    workspace: str
    publish_attempts: int
    max_attempts: int
    failure_code: str
    failed_at: datetime


class OperationStore(Protocol):
    def admit(self, application_id: str, kind: str, request_key: str, command: dict) -> Operation: ...

    def get(self, operation_id: str) -> Operation: ...

    def claim(
        self, operation_id: str, attempt_id: str, owner: str, *, seconds: int = 90
    ) -> ExecutionLease | None: ...

    def heartbeat(self, lease: ExecutionLease, *, seconds: int = 90) -> bool: ...

    def checkpoint(self, lease: ExecutionLease, checkpoint: dict) -> bool: ...

    def begin_external(self, lease: ExecutionLease, intent: dict) -> bool: ...

    def observe_external(self, lease: ExecutionLease, receipt: dict, checkpoint: dict) -> bool: ...

    def complete(self, lease: ExecutionLease, result: dict, *, succeeded: bool = True) -> bool: ...

    def interrupt(self, lease: ExecutionLease, checkpoint: dict) -> bool: ...

    def recover_expired(self, *, limit: int = 100) -> tuple[str, ...]: ...

    def resolve_attention(
        self,
        operation_id: str,
        attempt_id: str,
        expected_version: int,
        expected_intent: dict,
        receipt: dict,
        checkpoint: dict,
        *,
        resolver: str,
        outcome: str,
        result: dict | None = None,
    ) -> bool: ...

    def claim_outbox(
        self, owner: str, *, seconds: int = 60, limit: int = 10
    ) -> tuple[OutboxDelivery, ...]: ...

    def confirm_outbox(self, delivery: OutboxDelivery) -> bool: ...

    def release_outbox(self, delivery: OutboxDelivery, *, delay: int | None = None) -> bool: ...

    def list_failed_outbox(self, *, limit: int = 100) -> tuple[FailedOutbox, ...]: ...
