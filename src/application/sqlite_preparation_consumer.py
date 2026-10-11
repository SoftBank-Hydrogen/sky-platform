"""Lease-owned, bounded SQLite preparation on a dedicated queue; no DB SQL execution."""

import threading
from contextlib import contextmanager

from application.sqlite_preparation_jobs import validate_command
from ports.remote_builds import digest


class PreparationOwnershipLost(OSError):
    pass


class PreparationStopped(Exception):
    pass


class SqlitePreparationConsumer:
    def __init__(self, store, queue, preparation, protection, owner, *, heartbeat_seconds=30):
        self.store, self.queue, self.preparation = store, queue, preparation
        self.protection, self.owner, self.heartbeat_seconds = protection, owner, heartbeat_seconds

    @contextmanager
    def ownership(self, lease, delivery, stop):
        finished, lost = threading.Event(), threading.Event()
        def heartbeat():
            while not finished.wait(self.heartbeat_seconds):
                try:
                    if not self.store.heartbeat(lease, seconds=90):
                        lost.set()
                        return
                    self.queue.extend(delivery, seconds=300)
                    self.protection.set(True)
                except OSError:
                    lost.set()
                    return
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        def guard():
            if lost.is_set() or not self.store.heartbeat(lease, seconds=90):
                raise PreparationOwnershipLost()
            if stop.is_set():
                raise PreparationStopped()
        try:
            yield guard
        finally:
            finished.set()
            thread.join(timeout=30)

    def consume_once(self, stop=None):
        stop = stop or threading.Event()
        if stop.is_set():
            return "stopped"
        self.store.recover_expired(limit=10)
        delivery = self.queue.receive()
        if delivery is None:
            return "idle"
        message = delivery.message
        if message is None or message["workspace"] != self.store.workspace:
            self.queue.extend(delivery, seconds=60)
            return "invalid"
        try:
            operation = self.store.get(message["operation_id"])
        except FileNotFoundError:
            self.queue.extend(delivery, seconds=60)
            return "invalid"
        if operation.application_id != message["application_id"] or operation.kind != "prepare_sqlite":
            self.queue.extend(delivery, seconds=60)
            return "invalid"
        if operation.attempt_id != message["attempt_id"] or operation.status in {"succeeded", "failed", "needs_attention"}:
            self.queue.delete(delivery)
            return "duplicate"
        lease = self.store.claim(operation.id, operation.attempt_id, self.owner, seconds=90)
        if lease is None:
            self.queue.extend(delivery, seconds=30)
            return "busy"
        saved = operation.checkpoint
        checkpoint = {"stage": "preparing_sqlite", "command_digest": digest(operation.command),
                      "retry_count": saved.get("retry_count", 0)}
        try:
            self.protection.set(True)
            with self.ownership(lease, delivery, stop) as guard:
                try:
                    guard()
                    if type(checkpoint["retry_count"]) is not int or not 0 <= checkpoint["retry_count"] < 3:
                        raise ValueError("Invalid preparation retry checkpoint")
                    principal, original = validate_command(operation.command, operation.application_id)
                    if saved and (saved.get("stage") not in {"preparing_sqlite", "sqlite_prepared"}
                                  or saved.get("command_digest") != checkpoint["command_digest"]):
                        raise ValueError("Preparation checkpoint differs from command")
                    if not self.store.checkpoint(lease, checkpoint):
                        raise PreparationOwnershipLost()
                    # Content-addressed S3 writes are safe to replay after a lost acknowledgement.
                    result = self.preparation.prepare(principal, original, operation.command["sqlite_conversion"],
                                                      expected_source_digest=operation.command["source_digest"],
                                                      conversion_profile=operation.command.get("conversion_profile", "sqlite-data"))
                    guard()
                    if saved.get("stage") == "sqlite_prepared" and saved.get("result") != result:
                        raise ValueError("Replayed preparation result differs")
                    checkpoint.update(stage="sqlite_prepared", result=result)
                    if not self.store.checkpoint(lease, checkpoint) or not self.store.complete(lease, result):
                        raise PreparationOwnershipLost()
                except PreparationOwnershipLost:
                    return "ownership_lost"
                except PreparationStopped:
                    if not self.store.interrupt(lease, checkpoint):
                        return "ownership_lost"
                    self.queue.delete(delivery)
                    return "stopped"
                except (ValueError, PermissionError):
                    if not self.store.complete(lease, {"status": "failed", "reason": "invalid_source_or_review"}, succeeded=False):
                        return "ownership_lost"
                    self.queue.delete(delivery)
                    return "failed"
                except OSError:
                    retries = checkpoint["retry_count"] + 1
                    if retries >= 3:
                        if not self.store.complete(lease, {"status": "failed", "reason": "preparation_retry_exhausted"}, succeeded=False):
                            return "ownership_lost"
                        self.queue.delete(delivery)
                        return "failed"
                    if not self.store.interrupt(lease, {**checkpoint, "retry_count": retries}):
                        return "ownership_lost"
                    self.queue.delete(delivery)
                    return "retry"
            self.queue.delete(delivery)
            return "prepared"
        finally:
            try:
                self.protection.set(False)
            except OSError:
                pass
