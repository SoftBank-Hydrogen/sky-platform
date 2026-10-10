"""Consume reviewed allocations; queue bodies never carry allocation commands."""

import threading
from contextlib import contextmanager

from application.shared_database_workflow import KIND


class AllocationOwnershipLost(OSError):
    pass


class AllocationQueueRouter:
    """Route using durable operation kind, never fields supplied in queue payloads."""

    def __init__(self, operations, default_queue, allocation_queue):
        self.operations, self.default_queue, self.allocation_queue = operations, default_queue, allocation_queue

    def publish(self, delivery):
        operation = self.operations.get(delivery.operation_id)
        if delivery.workspace != self.operations.workspace or operation.application_id != delivery.application_id:
            raise OSError("Outbox scope does not match durable operation")
        queue = self.allocation_queue if operation.kind == KIND else self.default_queue
        if queue is None:
            raise OSError("Shared database queue is not configured")
        queue.publish(delivery)


class SharedDatabaseConsumer:
    def __init__(self, operations, queue, worker, *, heartbeat_seconds=30):
        self.operations, self.queue, self.worker = operations, queue, worker
        self.heartbeat_seconds = heartbeat_seconds

    @contextmanager
    def ownership(self, lease, delivery):
        stop, lost = threading.Event(), threading.Event()

        def renew():
            if lost.is_set() or not self.operations.heartbeat(lease, seconds=900):
                lost.set()
                raise AllocationOwnershipLost("Allocation execution ownership lost")
            self.queue.extend(delivery, seconds=300)

        def heartbeat():
            while not stop.wait(self.heartbeat_seconds):
                try:
                    renew()
                except OSError:
                    lost.set()
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            yield renew
        finally:
            stop.set()
            thread.join(timeout=30)

    def consume_once(self):
        self.operations.recover_expired(limit=10)
        delivery = self.queue.receive()
        if delivery is None:
            return "idle"
        message = delivery.message
        if message is None or message["workspace"] != self.operations.workspace:
            self.queue.extend(delivery, seconds=60)
            return "invalid"
        try:
            operation = self.operations.get(message["operation_id"])
        except FileNotFoundError:
            self.queue.extend(delivery, seconds=60)
            return "missing"
        if operation.application_id != message["application_id"]:
            self.queue.extend(delivery, seconds=60)
            return "invalid"
        # A shared queue must not let this consumer acknowledge another worker's job.
        if operation.kind != KIND:
            self.queue.extend(delivery, seconds=30)
            return "other_kind"
        if operation.attempt_id != message["attempt_id"] or operation.status in {
            "succeeded", "failed", "needs_attention"
        }:
            self.queue.delete(delivery)
            return "duplicate"
        if operation.status == "running":
            self.queue.extend(delivery, seconds=30)
            return "busy"
        self.worker.ownership = lambda lease: self.ownership(lease, delivery)
        try:
            result = self.worker.execute(operation.id, operation.attempt_id)
            # Only durable terminal states can acknowledge a delivery. An uncertain
            # commit/lease loss must remain observable for expiry recovery.
            saved = self.operations.get(operation.id)
            if saved.status in {"succeeded", "failed", "needs_attention"}:
                self.queue.delete(delivery)
            else:
                self.queue.extend(delivery, seconds=30)
            return result["status"]
        except OSError:
            return "unavailable"
        finally:
            self.worker.ownership = None
