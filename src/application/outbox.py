"""Bounded outbox publishing; remote sends happen after the claim transaction."""

from dataclasses import dataclass

from ports.operations import OperationStore
from ports.queue import OperationQueue


@dataclass(frozen=True)
class DispatchReport:
    confirmed: int
    deferred: int


class OutboxPublisher:
    def __init__(self, store: OperationStore, queue: OperationQueue, owner: str):
        self.store, self.queue, self.owner = store, queue, owner

    def dispatch_once(self, *, limit=10) -> DispatchReport:
        confirmed = deferred = 0
        for delivery in self.store.claim_outbox(self.owner, limit=limit):
            try:
                self.queue.publish(delivery)
            except OSError:
                # The queue may have accepted it. Preserve the attempt/dedup ID.
                # A DB failure here leaves the claim recoverable by lease expiry.
                self.store.release_outbox(delivery)
                deferred += 1
                continue
            # Never resend immediately after an ambiguous database commit.
            if self.store.confirm_outbox(delivery):
                confirmed += 1
            else:
                deferred += 1
        return DispatchReport(confirmed, deferred)
