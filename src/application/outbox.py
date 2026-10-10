"""Bounded outbox publishing; remote sends happen after the claim transaction."""

from dataclasses import dataclass

from ports.operations import OperationStore
from ports.queue import OperationQueue


@dataclass(frozen=True)
class DispatchReport:
    confirmed: int
    deferred: int


class OutboxPublisher:
    def __init__(self, store: OperationStore, queue: OperationQueue, owner: str, *, retry_delay=5):
        if type(retry_delay) is not int or not 0 <= retry_delay <= 3600:
            raise ValueError("Invalid outbox retry delay")
        self.store, self.queue, self.owner, self.retry_delay = store, queue, owner, retry_delay

    def dispatch_once(self, *, limit=10) -> DispatchReport:
        confirmed = deferred = 0
        for delivery in self.store.claim_outbox(self.owner, limit=limit):
            try:
                self.queue.publish(delivery)
            except OSError:
                # The queue may have accepted it. Preserve the attempt/dedup ID.
                # A DB failure here leaves the claim recoverable by lease expiry.
                self.store.release_outbox(delivery, delay=self.retry_delay)
                deferred += 1
                continue
            # Never resend immediately after an ambiguous database commit.
            if self.store.confirm_outbox(delivery):
                confirmed += 1
            else:
                deferred += 1
        return DispatchReport(confirmed, deferred)
