"""Only worker-side allocators receive credentials; receipts contain references."""

from dataclasses import dataclass, field
from typing import Protocol

from domain.shared_database import PoolAllocationRequest


class PoolAllocationError(ValueError):
    """Pool registration or isolation could not be established."""


class PoolAllocationConflict(PoolAllocationError):
    """An existing allocation has different settings or unowned resources."""


class PoolCapacityExceeded(PoolAllocationError):
    """Configured connection reservations are exhausted."""


@dataclass(frozen=True)
class AllocationCredentials:
    secret_ref: str
    password: str = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.secret_ref, str) or not self.secret_ref or len(self.secret_ref) > 512:
            raise ValueError("A credential secret reference is required")
        if not isinstance(self.password, str) or len(self.password) < 24 or "\0" in self.password:
            raise ValueError("Invalid allocation credential")


class SharedDatabaseAllocator(Protocol):
    def allocate(self, request: PoolAllocationRequest, credentials: AllocationCredentials) -> dict: ...
