"""Authorize a workload allocation before invoking a privileged DB adapter."""

from domain.access import Action, Principal, owner_from_record, permitted
from domain.shared_database import PoolAllocationRequest
from ports.shared_database import AllocationCredentials, SharedDatabaseAllocator


class SharedDatabaseService:
    def __init__(self, allocator: SharedDatabaseAllocator):
        self.allocator = allocator

    def allocate(
        self,
        principal: Principal,
        application: dict,
        request: PoolAllocationRequest,
        credentials: AllocationCredentials,
    ) -> dict:
        owner = owner_from_record(application)
        if not permitted(principal, Action.DEPLOY, owner) or request.organization_id != owner.organization_id:
            raise PermissionError("Workload database allocation denied")
        if application.get("application_id") != request.application_id:
            raise PermissionError("Allocation does not match the registered application")
        return self.allocator.allocate(request, credentials)
