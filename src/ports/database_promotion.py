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
