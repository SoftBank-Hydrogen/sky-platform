from copy import deepcopy

import pytest

from adapters.database.application_traffic import RegisteredApplicationTraffic
from adapters.database.application_write_gate import ApplicationWriteGate
from adapters.database.blue_green_traffic import BlueGreenApplicationTraffic
from tests.unit.test_database_cutover import fixture


class Router:
    def __init__(self, binding):
        self.binding = deepcopy(binding)
        self.switches = []
        self.fail_probe = False

    def current(self):
        return deepcopy(self.binding)

    def switch(self, expected, target):
        assert self.binding == expected
        self.binding = deepcopy(target)
        self.switches.append((expected, target))
        return {"binding": target}

    def probe(self, binding):
        if self.fail_probe:
            raise OSError("stable endpoint read failed")
        assert self.binding == binding
        return {"binding": binding, "http_status": 200}


def setup():
    from dataclasses import asdict

    flow, source, target, _, events = fixture()
    plan = {"plan_id": "test", "source": asdict(source.binding), "target": asdict(target.binding)}
    binds = [plan["source"], plan["target"]]
    reads, rebound = [], []
    healthy = [True, True]
    gates = []
    for index, binding in enumerate(binds):

        def probe(value, index=index):
            reads.append((index, value))
            return healthy[index]

        gates.append(
            ApplicationWriteGate(
                "control-" + str(index), binding, binds, rebind=rebound.append, read_probe=probe
            )
        )
    router = Router(binds[0])

    def fresh():
        controllers = [
            RegisteredApplicationTraffic(
                "https://private/control",
                "token",
                controller_id="control-" + str(index),
                source=binds[0],
                target=binds[1],
                guard=lambda: None,
                transport=gate.control,
            )
            for index, gate in enumerate(gates)
        ]
        return BlueGreenApplicationTraffic(*binds, *controllers, router, guard=lambda: None)

    flow.traffic = fresh()
    return flow, plan, gates, router, healthy, reads, rebound, events, fresh


def test_two_running_revisions_switch_then_reverse_without_in_place_rebind():
    flow, plan, gates, router, _, reads, rebound, _, _ = setup()
    result = flow.promote(plan, plan)
    assert router.binding == plan["target"] and gates[0].blocked and not gates[1].blocked
    assert router.switches == [(plan["source"], plan["target"])]
    assert reads[0] == (1, plan["target"]) and not rebound
    flow.rollback(plan, result["baseline"])
    assert router.binding == plan["source"] and not gates[0].blocked and gates[1].blocked
    assert router.switches[-1] == (plan["target"], plan["source"])
    assert gates[0].binding == plan["source"] and gates[1].binding == plan["target"]


@pytest.mark.parametrize("failure", ["green_read", "entrypoint_read"])
def test_failed_green_or_entrypoint_read_closed_writers_and_explicit_recovery(failure):
    flow, plan, gates, router, healthy, _, _, events, fresh = setup()
    if failure == "green_read":
        healthy[1] = False
    else:
        router.fail_probe = True
    with pytest.raises((RuntimeError, OSError)):
        flow.promote(plan, plan)
    assert all(gate.blocked for gate in gates)
    if failure == "green_read":
        assert router.binding == plan["source"] and not router.switches
    else:
        assert router.binding == plan["target"]
    final = next(
        event["receipt"]
        for event in events
        if event["stage"] == "final_sync" and event["status"] == "observed"
    )
    healthy[1], router.fail_probe = True, False
    # Recreate only after the operator inspects the persisted outcome and route.
    flow.traffic = fresh()
    flow.rollback(plan, final)
    assert router.binding == plan["source"] and not gates[0].blocked and gates[1].blocked


def test_external_route_change_cannot_open_new_writer():
    flow, plan, gates, router, _, _, _, _, _ = setup()
    traffic = flow.traffic
    traffic.quiesce(plan["source"])
    traffic.activate(plan["source"], plan["target"])
    traffic.probe(plan["target"])
    router.binding = plan["source"]
    with pytest.raises(ValueError, match="route"):
        traffic.resume(plan["target"])
    assert all(gate.blocked for gate in gates)
