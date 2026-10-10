"""Explicit single-writer cutover for registered, quiesceable app controllers.

No generic ECS/HTTP app is presumed quiesceable. Callers provide ownership/TLS
checks, exclusive execution guard and a durable journal. An unresolved intent
requires reconciliation rather than automatic replay. No schema changes or CDC.
"""

from dataclasses import asdict

from domain.database import DatabaseBinding


class DatabaseCutover:
    def __init__(self, synchronizer, source_fence, target_fence, traffic, *, guard, journal):
        self.sync, self.source, self.target, self.traffic = synchronizer, source_fence, target_fence, traffic
        self.guard, self.journal = guard, journal

    def _step(self, name, action):
        self.guard()
        self.journal({"stage": name, "status": "intent"})
        result = action()
        self.guard()
        self.journal({"stage": name, "status": "observed", "receipt": result})
        return result

    def _login(self, fence, enabled):
        return fence.set_login(enabled, guard=self.guard)

    def _validate(self, plan, baseline):
        source, target = DatabaseBinding(**plan["source"]), DatabaseBinding(**plan["target"])
        if (
            source.role != "shared_workload"
            or target.role != "dedicated_workload"
            or source.instance_id == target.instance_id
            or (source.organization_id, source.application_id, source.account_id, source.region)
            != (target.organization_id, target.application_id, target.account_id, target.region)
            or asdict(self.source.binding) != plan["source"]
            or asdict(self.target.binding) != plan["target"]
            or baseline.get("plan_id") != plan["plan_id"]
            or baseline.get("source") != plan["source"]
            or baseline.get("target") != plan["target"]
        ):
            raise ValueError("Exact registered cutover bindings and baseline required")

    def promote(self, plan, baseline):
        self._validate(plan, baseline)
        self._step("quiesce", lambda: self.traffic.quiesce(plan["source"]))
        self._step("fence_source", lambda: self._login(self.source, False))
        self._step("fence_target", lambda: self._login(self.target, False))
        final = self._step(
            "final_sync", lambda: self.sync.refresh(plan, expected_destination=baseline, guard=self.guard)
        )
        self._step("enable_target", lambda: self._login(self.target, True))
        self._step("activate_target", lambda: self.traffic.activate(plan["source"], plan["target"]))
        self._step("probe_target", lambda: self.traffic.probe(plan["target"]))
        self._step("resume_target", lambda: self.traffic.resume(plan["target"]))
        return {
            "plan_id": plan["plan_id"],
            "binding": plan["target"],
            "baseline": final,
            "single_writer": True,
            "source_retained": True,
        }

    def rollback(self, plan, final_baseline):
        self._validate(plan, final_baseline)
        # Required even if target health failed before resume: there may be writes
        # from a prior attempt. Guard/journal failures deliberately leave it closed.
        self._step("quiesce_target", lambda: self.traffic.quiesce(plan["target"]))
        self._step("fence_target_recovery", lambda: self._login(self.target, False))
        self._step("fence_source_recovery", lambda: self._login(self.source, False))
        recovered = self._step(
            "reverse_sync",
            lambda: self.sync.refresh(
                plan, expected_destination=final_baseline, guard=self.guard, reverse=True
            ),
        )
        self._step("enable_source", lambda: self._login(self.source, True))
        self._step("activate_source", lambda: self.traffic.activate(plan["target"], plan["source"]))
        self._step("probe_source", lambda: self.traffic.probe(plan["source"]))
        self._step("resume_source", lambda: self.traffic.resume(plan["source"]))
        return {
            "plan_id": plan["plan_id"],
            "binding": plan["source"],
            "baseline": recovered,
            "target_retained": True,
            "single_writer": True,
        }


class OperationCutoverJournal:
    """Bind step intents/observations to an already claimed exclusive operation.

    Admission/approval and controller registration remain caller gates. If an
    intent already exists, refuse replay until an operator reconciles it. This
    adapter never claims that a DB lease can revoke a remote AWS request.
    """

    def __init__(self, store, lease):
        self.store, self.lease = store, lease
        self.events = []

    def guard(self):
        if not self.store.heartbeat(self.lease, seconds=900):
            raise RuntimeError("Cutover execution ownership lost")

    def begin(self, plan):
        self.guard()
        operation = self.store.get(self.lease.operation_id)
        if operation.external_pending or operation.external_intent is not None:
            raise ValueError("Existing external intent requires reconciliation")
        if not self.store.begin_external(self.lease, {"action": "database_cutover", "plan": plan}):
            raise RuntimeError("Cutover intent could not be persisted")

    def __call__(self, event):
        self.events.append(event)
        if not self.store.checkpoint(self.lease, {"cutover_steps": list(self.events)}):
            raise RuntimeError("Cutover checkpoint ownership lost")

    def finish(self, receipt):
        self.guard()
        if not self.store.observe_external(self.lease, receipt, {"cutover_steps": list(self.events)}):
            raise RuntimeError("Cutover observation ownership lost")
        if not self.store.complete(self.lease, receipt):
            raise RuntimeError("Cutover completion ownership lost")

    def hold(self):
        # Preserve the external intent and app reservation on unknown effects.
        if not self.store.require_attention(self.lease, {"cutover_steps": list(self.events)}):
            raise RuntimeError("Cutover recovery ownership lost")
