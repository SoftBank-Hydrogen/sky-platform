"""Trusted dedicated-target preparation; no application traffic or data movement."""

from typing import Protocol


class DedicatedDatabasePreparer(Protocol):
    def prepare(self, plan: dict, *, operation_id: str, guard) -> dict:
        """Reconcile owned target by operation identity, then verify its runtime.

        The implementation must guard external mutations, use stable ownership
        identifiers, and never expose passwords. A retry must inspect existing
        resources rather than create a second RDS instance. This port itself
        does not implement AWS provisioning or grant a permission boundary.
        """
        ...


class QuiesceableApplicationTraffic(Protocol):
    """Registered app controller, not an assumed ability of arbitrary apps.

    quiesce drains in-flight writes and blocks new ones. activate is a CAS of
    the exact current binding while the gate is closed. probe must raise on
    failure and verify an actual app read through that binding. resume opens
    writes only for the verified binding. Receipts must contain no credentials.
    Repeated intents/unknown effects require operator reconciliation.
    """

    def quiesce(self, expected_binding: dict) -> dict: ...
    def activate(self, expected_binding: dict, target_binding: dict) -> dict: ...
    def probe(self, expected_binding: dict) -> dict: ...
    def resume(self, expected_binding: dict) -> dict: ...
