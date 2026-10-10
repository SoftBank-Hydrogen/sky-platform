"""Consume one admitted operation; never redispatch an uncertain remote build."""
import threading
import time
from contextlib import contextmanager
from ports.remote_builds import digest, request_for, validate_result


class OwnershipLost(OSError):
    pass


class BuildConsumer:
    def __init__(self, store, queue, builder, objects, verifier, settings, protection, owner,
                 *, poll_seconds=5, timeout_seconds=2700, heartbeat_seconds=30, clock=time.monotonic):
        self.store, self.queue, self.builder, self.objects = store, queue, builder, objects
        self.verifier, self.settings, self.protection, self.owner = verifier, settings, protection, owner
        self.poll_seconds, self.timeout_seconds, self.heartbeat_seconds, self.clock = poll_seconds, timeout_seconds, heartbeat_seconds, clock

    @contextmanager
    def ownership(self, lease, delivery):
        stop, lost = threading.Event(), threading.Event()
        def heartbeat():
            while not stop.wait(self.heartbeat_seconds):
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
                raise OwnershipLost("Build ownership lost")
        try:
            yield guard
        finally:
            stop.set()
            thread.join(timeout=30)

    def consume_once(self, stop=None):
        stop = stop or threading.Event()
        self.store.recover_expired(limit=10)
        delivery = self.queue.receive()
        if delivery is None:
            return "idle"
        message = delivery.message
        if message is None or message["workspace"] != self.store.workspace:
            self.queue.extend(delivery, seconds=60)
            return "invalid"
        operation = self.store.get(message["operation_id"])
        if operation.application_id != message["application_id"]:
            self.queue.extend(delivery, seconds=60)
            return "invalid"
        if operation.attempt_id != message["attempt_id"] or operation.status in {"failed", "succeeded", "needs_attention"}:
            self.queue.delete(delivery)
            return "duplicate"
        if operation.status == "running":
            self.queue.extend(delivery, seconds=30)
            return "busy"
        request = request_for(operation, self.store.workspace, self.settings)
        checkpoint = operation.checkpoint if operation.checkpoint.get("stage") in {"build_ready", "build_failed"} else {
            "stage": "building", "request": request, "request_digest": digest(request)}
        self.protection.set(True)
        try:
            lease = self.store.claim(operation.id, operation.attempt_id, self.owner, seconds=90)
            if lease is None:
                self.queue.extend(delivery, seconds=30)
                return "busy"
            with self.ownership(lease, delivery) as guard:
                try:
                    guard()
                    if not self.store.build_progress(lease, checkpoint, stage="building"):
                        raise OwnershipLost()
                    saved = operation.checkpoint
                    if saved.get("stage") in {"build_ready", "build_failed"}:
                        if saved.get("request_digest") != digest(request):
                            raise ValueError("Recovered build request changed")
                        run = self.builder.observe(request)
                        if run != saved.get("verified_run"):
                            raise ValueError("Recovered workflow evidence changed")
                        if saved["stage"] == "build_ready":
                            result = validate_result(saved["build_result"], request, run["run_id"])
                            self.verifier.verify(request, result)
                        if not self.store.build_progress(lease, saved, stage=("build_ready" if saved["stage"] == "build_ready" else "failed")):
                            raise OwnershipLost()
                        self.queue.delete(delivery)
                        return "build_ready" if saved["stage"] == "build_ready" else "failed"
                    if saved and (saved.get("stage") != "building" or saved.get("request_digest") != digest(request)):
                        raise ValueError("Build checkpoint requires explicit reconciliation")
                    self.objects.put_request(request)
                    guard()
                    intent = {"kind": "github_build", "build_id": request["build_id"], "request_digest": digest(request),
                              "repository": self.settings.repository, "workflow_sha": self.settings.workflow_sha}
                    if not self.store.begin_external(lease, intent):
                        raise OwnershipLost()
                    self.builder.dispatch(request)
                    checkpoint["stage"] = "waiting_build"
                    if not self.store.build_progress(lease, checkpoint, stage="waiting_build"):
                        raise OwnershipLost()
                    until = self.clock()+self.timeout_seconds
                    while not stop.is_set() and self.clock() < until:
                        guard()
                        try:
                            run = self.builder.observe(request)
                            if run is not None:
                                if not run["succeeded"]:
                                    checkpoint.update(stage="build_failed", verified_run=run)
                                    if not self.store.observe_external(lease, run, checkpoint):
                                        raise OwnershipLost()
                                    if not self.store.build_progress(lease, {**checkpoint, "reason": "remote_build_failed"}, stage="failed"):
                                        raise OwnershipLost()
                                    self.queue.delete(delivery)
                                    return "failed"
                                result = validate_result(self.objects.result(request), request, run["run_id"])
                                self.verifier.verify(request, result)
                                guard()
                                checkpoint.update(stage="build_ready", build_result=result, verified_run=run, reason="deployment_executor_required")
                                if not self.store.observe_external(lease, run, checkpoint):
                                    raise OwnershipLost()
                                if not self.store.build_progress(lease, checkpoint, stage="build_ready"):
                                    raise OwnershipLost()
                                self.queue.delete(delivery)
                                return "build_ready"
                        except OwnershipLost:
                            raise
                        except OSError:
                            # Read failures after dispatch never trigger a second workflow.
                            pass
                        stop.wait(self.poll_seconds)
                    guard()
                    if self.store.build_progress(lease, {**checkpoint, "reason": "build_observation_incomplete"}, stage="needs_attention"):
                        self.queue.delete(delivery)
                    return "needs_attention"
                except OwnershipLost:
                    return "ownership_lost"
                except (OSError, ValueError):
                    guard()
                    # Includes uncertain dispatch/commit. Preserve request intent and app lock.
                    if self.store.build_progress(lease, {**checkpoint, "reason": "build_requires_reconciliation"}, stage="needs_attention"):
                        self.queue.delete(delivery)
                    return "needs_attention"
        finally:
            try:
                self.protection.set(False)
            except OSError:
                pass
