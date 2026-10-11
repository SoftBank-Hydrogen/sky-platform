"""Two separately running, registered writer-gated apps behind one entrypoint.

Both app controllers and the router are operator registrations. Each revision
has a fixed database binding; no in-place app rebind is performed. Existing
connections and multiple writers require separately verified drain contracts.
"""

from copy import deepcopy
from domain.database import DatabaseBinding


class BlueGreenApplicationTraffic:
    def __init__(self, source, target, blue, green, router, *, guard):
        left, right = DatabaseBinding(**source), DatabaseBinding(**target)
        if source == target or blue is green:
            raise ValueError("Distinct Blue and Green executions required")
        if (
            left.role != "shared_workload"
            or right.role != "dedicated_workload"
            or (left.organization_id, left.application_id, left.account_id, left.region)
            != (right.organization_id, right.application_id, right.account_id, right.region)
        ):
            raise ValueError("Same-application Blue/Green bindings required")
        self.bindings, self.controllers = [deepcopy(source), deepcopy(target)], [blue, green]
        self.router, self.guard = router, guard
        self.closed, self.verified, self.uncertain = False, None, False

    def recovery_binding(self):
        self.guard()
        binding = self.router.current()
        self._index(binding)
        self.guard()
        return binding

    def _index(self, binding):
        if binding not in self.bindings:
            raise ValueError("Unregistered Blue/Green binding")
        return self.bindings.index(binding)

    def _route(self, binding):
        self.guard()
        if self.router.current() != binding:
            raise ValueError("Stable entrypoint route differs from expected binding")
        self.guard()

    def quiesce(self, expected_binding):
        if self.uncertain:
            raise RuntimeError("Blue/Green outcome requires reconciliation")
        active = self._index(expected_binding)
        self._route(expected_binding)
        self.uncertain = True
        # Standby must be closed too: its DB login will be enabled for probing.
        standby = self.controllers[1 - active].quiesce(self.bindings[1 - active])
        self.guard()
        live = self.controllers[active].quiesce(expected_binding)
        self.guard()
        self.closed, self.verified, self.uncertain = True, None, False
        return {"strategy": "blue_green", "writers_closed": [standby, live]}

    def activate(self, expected_binding, target_binding):
        if not self.closed or self.uncertain or expected_binding == target_binding:
            raise ValueError("Both Blue/Green writers must be quiesced")
        index = self._index(target_binding)
        self._route(expected_binding)
        self.uncertain = True
        # This read happens after final sync/target login enable and BEFORE any
        # traffic moves. Failed Green readiness leaves the entrypoint on Blue.
        readiness = self.controllers[index].probe(target_binding)
        self.guard()
        route = self.router.switch(expected_binding, target_binding)
        self._route(target_binding)
        self.uncertain = False
        return {"strategy": "blue_green", "standby_readiness": readiness, "route": route}

    def probe(self, expected_binding):
        if not self.closed or self.uncertain:
            raise ValueError("Quiesced Blue/Green route required")
        index = self._index(expected_binding)
        self._route(expected_binding)
        self.uncertain = True
        routed_read = self.router.probe(expected_binding)
        read = self.controllers[index].probe(expected_binding)
        self.guard()
        self.verified, self.uncertain = deepcopy(expected_binding), False
        return {"strategy": "blue_green", "entrypoint_read": routed_read, "app_read": read}

    def resume(self, expected_binding):
        if not self.closed or self.uncertain or self.verified != expected_binding:
            raise ValueError("Verified stable entrypoint required before opening writes")
        self._route(expected_binding)
        self.uncertain = True
        receipt = self.controllers[self._index(expected_binding)].resume(expected_binding)
        self.guard()
        self.closed, self.verified, self.uncertain = False, None, False
        return {"strategy": "blue_green", "active_writer": receipt, "standby_writes_blocked": True}
