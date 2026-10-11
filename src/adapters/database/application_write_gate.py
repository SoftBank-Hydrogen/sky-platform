"""Opt-in app-side control implementation; every writer must use write().

An authenticated, private HTTPS endpoint must wrap control(). This component
does not install that endpoint, secure arbitrary apps or coordinate replicas.
It is deliberately limited to a registered single-process writer.
"""

from contextlib import contextmanager
from threading import Condition


class ApplicationWriteGate:
    def __init__(
        self,
        controller_id,
        binding,
        allowed_bindings,
        *,
        rebind,
        read_probe,
        drain_timeout=30,
        initially_blocked=False,
    ):
        if (
            binding not in allowed_bindings
            or len(allowed_bindings) != 2
            or drain_timeout <= 0
            or type(initially_blocked) is not bool
        ):
            raise ValueError("Explicit registered bindings and bounded drain required")
        self.controller_id, self.binding = controller_id, dict(binding)
        self.allowed = [dict(item) for item in allowed_bindings]
        self.rebind, self.read_probe, self.drain_timeout = rebind, read_probe, drain_timeout
        self.condition = Condition()
        self.blocked, self.active, self.generation = initially_blocked, 0, 0
        self.verified, self.uncertain, self.controlling = False, False, False

    @contextmanager
    def write(self, expected_binding):
        with self.condition:
            if self.blocked or self.uncertain or expected_binding != self.binding:
                raise RuntimeError("Writes are unavailable for this binding")
            self.active += 1
        try:
            yield
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()

    def control(self, command):
        keys = {
            "schema_version",
            "controller_id",
            "request_id",
            "action",
            "expected_binding",
            "expected_generation",
            "target_binding",
        }
        with self.condition:
            if (
                not isinstance(command, dict)
                or set(command) != keys
                or type(command["schema_version"]) is not int
                or command["schema_version"] != 1
                or command["controller_id"] != self.controller_id
                or not isinstance(command["request_id"], str)
                or len(command["request_id"]) != 32
                or command["expected_binding"] != self.binding
                or self.uncertain
                or self.controlling
            ):
                raise ValueError("Control scope or execution state mismatch")
            action, generation = command["action"], command["expected_generation"]
            if (
                action not in {"quiesce", "activate", "probe", "resume"}
                or (
                    not (action == "quiesce" and generation is None)
                    and (type(generation) is not int or generation != self.generation)
                )
                or (action != "activate" and command["target_binding"] is not None)
                or (action == "activate" and command["target_binding"] not in self.allowed)
                or (action != "quiesce" and (not self.blocked or self.active))
                or (action == "resume" and not self.verified)
            ):
                raise ValueError("Control generation or write gate mismatch")
            self.controlling = True
            try:
                if action == "quiesce":
                    self.blocked, self.verified = True, False
                    if not self.condition.wait_for(lambda: self.active == 0, timeout=self.drain_timeout):
                        raise RuntimeError("Writers did not drain; gate remains closed")
                elif action == "activate":
                    self.verified = False
                    # Rebind must close old connection pools and install the
                    # registered secret reference. A partial failure is unknown.
                    self.uncertain = True
                    self.rebind(dict(command["target_binding"]))
                    self.binding = dict(command["target_binding"])
                    self.uncertain = False
                elif action == "probe":
                    self.verified = False
                    if self.read_probe(dict(self.binding)) is not True:
                        raise RuntimeError("Actual database read failed")
                    self.verified = True
                else:
                    self.blocked, self.verified = False, False
                self.generation += 1
                return {
                    "schema_version": 1,
                    "controller_id": self.controller_id,
                    "request_id": command["request_id"],
                    "action": action,
                    "binding": dict(self.binding),
                    "generation": self.generation,
                    "writes_blocked": self.blocked,
                    "active_writes": self.active,
                    "database_read_verified": action == "probe" and self.verified,
                }
            finally:
                self.controlling = False
