"""App-scoped read-only capacity measurements through an audited admin connection.

No latency inference from SELECT 1 or pool-wide CPU. Caller audits AWS ownership,
TLS and collector identity. This adapter never authorizes a migration itself.
"""

import psycopg

from domain.database import DatabaseBinding


class PostgresWorkloadMetrics:
    def __init__(self, connect, *, pool_database, collector_id, clock):
        self.connect, self.pool_database, self.collector_id, self.clock = (
            connect,
            pool_database,
            collector_id,
            clock,
        )

    def collect(self, binding, *, storage_quota_bytes=None):
        try:
            return self._collect(binding, storage_quota_bytes=storage_quota_bytes)
        except psycopg.Error:
            raise OSError("Owned workload metrics unavailable") from None

    def _collect(self, binding, *, storage_quota_bytes=None):
        if (
            not isinstance(binding, DatabaseBinding)
            or binding.role != "shared_workload"
            or not binding.owner_ref.startswith("allocation-")
            or binding.database_name.startswith("sky_pool_")
        ):
            raise ValueError("Owned shared app database required")
        allocation_id = binding.owner_ref.removeprefix("allocation-")
        marker, role = "sky-pool-allocation-v1:" + allocation_id, "sky_login_" + allocation_id
        if binding.database_name != "sky_app_" + allocation_id:
            raise ValueError("App allocation identity differs from database")
        with self.connect() as db:
            if db.execute("SELECT current_database()").fetchone() != (self.pool_database,):
                raise ValueError("Registered pool administration connection required")
            db.execute("SET TRANSACTION READ ONLY")
            db.execute("SET LOCAL statement_timeout='10s'")
            metadata = db.execute(
                "SELECT shobj_description(oid,'pg_database'),datconnlimit,pg_database_size(oid) FROM pg_database WHERE datname=%s",
                (binding.database_name,),
            ).fetchone()
            login = db.execute(
                "SELECT shobj_description(oid,'pg_authid'),rolconnlimit FROM pg_roles WHERE rolname=%s",
                (role,),
            ).fetchone()
            if (
                not metadata
                or not login
                or metadata[0] != marker
                or login[0] != marker
                or metadata[1] <= 0
                or login[1] <= 0
            ):
                raise ValueError("App database/role ownership or quotas differ")
            connections = db.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname=%s AND usename=%s",
                (binding.database_name, role),
            ).fetchone()[0]
        values = {
            "connections": connections,
            "connection_limit": min(metadata[1], login[1]),
            "storage_bytes": metadata[2],
        }
        if storage_quota_bytes is not None:
            if type(storage_quota_bytes) is not int or not 1 <= storage_quota_bytes < 2**63:
                raise ValueError("Explicit app storage quota required")
            values["storage_limit_bytes"] = storage_quota_bytes
        return {
            "schema_version": 1,
            "collector_id": self.collector_id,
            "database_binding": binding.__dict__.copy(),
            "observed_at": self.clock(),
            "values": values,
        }
