"""A queue message carries identities only; workers load commands from the DB."""

from dataclasses import dataclass
from typing import Protocol

from ports.operations import OutboxDelivery


class OperationQueue(Protocol):
    def publish(self, delivery: OutboxDelivery) -> None: ...


@dataclass(frozen=True)
class ReceivedOperation:
    receipt: str
    message: dict | None


class ConsumerQueue(Protocol):
    def receive(self) -> ReceivedOperation | None: ...
    def extend(self, delivery: ReceivedOperation, *, seconds: int = 300) -> None: ...
    def delete(self, delivery: ReceivedOperation) -> None: ...
