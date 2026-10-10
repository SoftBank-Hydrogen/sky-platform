"""App-owned logical allocations inside a separately registered workload pool."""

import hashlib
import json
import re
from dataclasses import dataclass

from engine.database_promotion import DatabaseBinding


@dataclass(frozen=True)
class SharedDatabasePool:
    id: str
    account_id: str
    region: str
    instance_id: str
    control_database: str
    connection_budget: int
    role: str = "shared_workload"

    def __post_init__(self):
        DatabaseBinding(
            "pool",
            "registry",
            self.role,
            self.account_id,
            self.region,
            self.instance_id,
            self.control_database,
            self.id,
        )
        if self.role != "shared_workload" or not self.control_database.startswith("sky_pool_"):
            raise ValueError("Register a separate workload pool control database")
        if type(self.connection_budget) is not int or not 1 <= self.connection_budget <= 1000:
            raise ValueError("Invalid app connection budget")


@dataclass(frozen=True)
class PoolAllocationRequest:
    pool: SharedDatabasePool
    organization_id: str
    application_id: str
    connection_limit: int = 5

    def __post_init__(self):
        if not isinstance(self.pool, SharedDatabasePool):
            raise TypeError("A registered workload pool is required")
        for value in (self.organization_id, self.application_id):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
                raise ValueError("Invalid allocation ownership")
        if type(self.connection_limit) is not int or not 1 <= self.connection_limit <= 50:
            raise ValueError("App connection limit must be between 1 and 50")

    @property
    def id(self):
        identity = [
            self.pool.account_id,
            self.pool.region,
            self.pool.instance_id,
            self.pool.id,
            self.organization_id,
            self.application_id,
        ]
        return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()[:24]

    @property
    def database_name(self):
        return "sky_app_" + self.id

    @property
    def login_role(self):
        return "sky_login_" + self.id

    @property
    def ownership_marker(self):
        return "sky-pool-allocation-v1:" + self.id

    def binding(self):
        return DatabaseBinding(
            self.organization_id,
            self.application_id,
            "shared_workload",
            self.pool.account_id,
            self.pool.region,
            self.pool.instance_id,
            self.database_name,
            "allocation-" + self.id,
        )
