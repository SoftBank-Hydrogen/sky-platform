"""Final refresh and reverse recovery of an explicitly verified table snapshot."""

import hashlib
import json

from psycopg import sql

from adapters.database.promotion_copy import PostgresPromotionCopy, _digest, _inventory
from domain.database import DatabaseBinding


class PostgresPromotionSynchronizer(PostgresPromotionCopy):
    def refresh(self, plan, *, expected_destination, guard, reverse=False):
        source, target = DatabaseBinding(**plan["source"]), DatabaseBinding(**plan["target"])
        if (
            source.role != "shared_workload"
            or target.role != "dedicated_workload"
            or source.instance_id == target.instance_id
            or (source.organization_id, source.application_id, source.account_id, source.region)
            != (target.organization_id, target.application_id, target.account_id, target.region)
            or any(b.database_name.startswith("sky_pool_") for b in (source, target))
            or expected_destination.get("plan_id") != plan["plan_id"]
            or expected_destination.get("source") != plan["source"]
            or expected_destination.get("target") != plan["target"]
        ):
            raise ValueError("Exact promotion binding and baseline required")
        read, write = (
            (self.target_connect, self.source_connect)
            if reverse
            else (self.source_connect, self.target_connect)
        )
        read_binding, write_binding = (target, source) if reverse else (source, target)
        guard()
        with read() as src, write() as dst:
            for connection, binding in ((src, read_binding), (dst, write_binding)):
                if connection.execute("SELECT current_database()").fetchone() != (binding.database_name,):
                    raise ValueError("Connected database differs from approved binding")
                connection.execute("SET LOCAL TIME ZONE 'UTC'")
                connection.execute("SET LOCAL datestyle='ISO, YMD'")
                connection.execute("SET LOCAL lock_timeout='5s'")
                connection.execute("SET LOCAL statement_timeout='60s'")
            tables = _inventory(src)
            if not tables or len(tables) > 100 or _inventory(dst) != tables:
                raise ValueError("Supported matching schemas required")
            names = sql.SQL(",").join(sql.Identifier("public", name) for name, _, _ in tables)
            src.execute(sql.SQL("LOCK TABLE {} IN SHARE MODE").format(names))
            dst.execute(sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(names))
            schema_hash = hashlib.sha256(json.dumps(tables, sort_keys=True).encode()).hexdigest()
            if (
                _inventory(src) != tables
                or _inventory(dst) != tables
                or schema_hash != expected_destination.get("schema_sha256")
            ):
                raise ValueError("Schema differs from verified baseline")
            destination = []
            baseline_rows, baseline_bytes = self.max_rows, self.max_bytes
            for name, _, _ in tables:
                value = _digest(dst, name, baseline_rows, baseline_bytes)
                used = dst.execute(
                    sql.SQL("SELECT COALESCE(sum(octet_length(row_to_json(t)::text)),0) FROM {} t").format(
                        sql.Identifier("public", name)
                    )
                ).fetchone()[0]
                destination.append({"table": name, **value})
                baseline_rows -= value["rows"]
                baseline_bytes -= used
            if destination != expected_destination.get("tables"):
                raise ValueError("Destination changed since baseline; refuse overwrite")
            guard()
            dst.execute(sql.SQL("TRUNCATE {}").format(names))
            evidence = []
            rows, size = self.max_rows, self.max_bytes
            for name, _, _ in tables:
                guard()
                before = _digest(src, name, rows, size)
                used = src.execute(
                    sql.SQL("SELECT COALESCE(sum(octet_length(row_to_json(t)::text)),0) FROM {} t").format(
                        sql.Identifier("public", name)
                    )
                ).fetchone()[0]
                with (
                    src.cursor().copy(
                        sql.SQL("COPY {} TO STDOUT").format(sql.Identifier("public", name))
                    ) as incoming,
                    dst.cursor().copy(
                        sql.SQL("COPY {} FROM STDIN").format(sql.Identifier("public", name))
                    ) as outgoing,
                ):
                    for chunk in incoming:
                        guard()
                        outgoing.write(chunk)
                if _digest(dst, name, self.max_rows, self.max_bytes) != before:
                    raise ValueError("Refresh integrity mismatch")
                rows -= before["rows"]
                size -= used
                evidence.append({"table": name, **before})
            guard()
            dst.commit()
        return {
            **expected_destination,
            "tables": evidence,
            "verified_scope": "reverse_recovery_copy" if reverse else "final_locked_refresh",
            "routing_changed": False,
            "ready_to_cutover": False,
        }
