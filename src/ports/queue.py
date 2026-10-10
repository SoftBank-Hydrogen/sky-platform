"""A queue message carries identities only; workers load commands from the DB."""

from typing import Protocol

from ports.operations import OutboxDelivery


class OperationQueue(Protocol):
    def publish(self, delivery: OutboxDelivery) -> None: ...
