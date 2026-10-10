"""Bounded table-only copy under a source write lock; never changes app routing.

Callers must verify AWS ownership/TLS and provide connections for the exact
approved bindings. Unsupported schema objects stop the copy rather than vanish.
The source lock ends after verification: later writes require a fresh final copy
or another synchronization strategy before any cutover.
"""

import hashlib
import json

from psycopg import sql

from domain.database import DatabaseBinding

_TYPES = {
    16: "boolean",
    17: "bytea",
    20: "bigint",
    21: "smallint",
    23: "integer",
    25: "text",
    700: "real",
    701: "double precision",
    1082: "date",
    1114: "timestamp without time zone",
    1184: "timestamp with time zone",
    1700: "numeric",
    2950: "uuid",
    3802: "jsonb",
}


def _inventory(connection):
    # Views, sequences, generated/default expressions, functions, triggers,
    # foreign keys and non-primary indexes need a richer schema migration path.
    objects = connection.execute("""SELECT c.relname,c.relkind,c.relpersistence,c.relrowsecurity
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind NOT IN ('i') ORDER BY c.relname""").fetchall()
    if any(kind != "r" or persistence != "p" or rls for _, kind, persistence, rls in objects):
        raise ValueError("Unsupported relation, persistence or row security")
    if connection.execute("""SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname NOT IN
        ('public','information_schema') AND nspname NOT LIKE 'pg_%') OR
        EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public') OR
        EXISTS(SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n
        ON n.oid=c.relnamespace WHERE n.nspname='public' AND NOT t.tgisinternal) OR
        EXISTS(SELECT 1 FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public') OR
        EXISTS(SELECT 1 FROM pg_extension WHERE extname<>'plpgsql')""").fetchone()[0]:
        raise ValueError("Unsupported schema, routine, trigger or extension")
    tables = []
    for name, *_ in objects:
        columns = connection.execute(
            """SELECT a.attname,a.atttypid,a.atttypmod,a.attnotnull,
            a.atthasdef,a.attidentity,a.attgenerated,a.attcollation=t.typcollation
            FROM pg_attribute a JOIN pg_type t ON t.oid=a.atttypid
            WHERE a.attrelid=%s::regclass AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum""",
            (sql.Identifier("public", name).as_string(connection),),
        ).fetchall()
        if not columns or any(
            oid not in _TYPES or mod != -1 or default or identity or generated or not collation
            for _, oid, mod, _, default, identity, generated, collation in columns
        ):
            raise ValueError("Unsupported column type, precision, collation or default")
        constraints = connection.execute(
            """SELECT contype,conkey,condeferrable,condeferred FROM pg_constraint
            WHERE conrelid=%s::regclass""",
            (sql.Identifier("public", name).as_string(connection),),
        ).fetchall()
        if any(kind != "p" or deferred or deferrable for kind, _, deferrable, deferred in constraints):
            raise ValueError("Unsupported table constraint")
        indexes = connection.execute(
            """SELECT indisprimary FROM pg_index
            WHERE indrelid=%s::regclass""",
            (sql.Identifier("public", name).as_string(connection),),
        ).fetchall()
        if any(not primary for (primary,) in indexes):
            raise ValueError("Unsupported secondary index")
        # conkey uses physical attnums; refuse dropped columns instead of guessing.
        dropped = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid=%s::regclass AND attisdropped)",
            (sql.Identifier("public", name).as_string(connection),),
        ).fetchone()[0]
        if dropped:
            raise ValueError("Unsupported dropped column layout")
        primary = [columns[i - 1][0] for kind, keys, *_ in constraints for i in keys]
        tables.append(
            (name, [(name, _TYPES[oid], required) for name, oid, _, required, *_ in columns], primary)
        )
    return tables


def _digest(connection, name, max_rows, max_bytes):
    # Bound total bytes before returning row contents to the worker.
    count, size = connection.execute(
        sql.SQL("SELECT count(*),COALESCE(sum(octet_length(row_to_json(t)::text)),0) FROM {} t").format(
            sql.Identifier("public", name)
        )
    ).fetchone()
    if count > max_rows or size > max_bytes:
        raise ValueError("Copy row/byte limit exceeded")
    # Canonical server-side JSON handles bytea/date/decimal consistently across DBs.
    rows = connection.execute(
        sql.SQL("SELECT row_to_json(t)::text FROM {} t").format(sql.Identifier("public", name))
    ).fetchmany(max_rows + 1)
    if len(rows) > max_rows:
        raise ValueError("Copy row limit exceeded")
    values = sorted(row[0] for row in rows)
    if sum(len(v.encode()) for v in values) > max_bytes:
        raise ValueError("Copy byte limit exceeded")
    digest = hashlib.sha256()
    for value in values:
        raw = value.encode()
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return {"rows": len(values), "sha256": digest.hexdigest()}


class PostgresPromotionCopy:
    def __init__(self, source_connect, target_connect, *, max_rows=100000, max_bytes=64 * 1024 * 1024):
        if (
            type(max_rows) is not int
            or not 1 <= max_rows <= 100000
            or type(max_bytes) is not int
            or not 1 <= max_bytes <= 64 * 1024 * 1024
        ):
            raise ValueError("Explicit bounded copy limits required")
        self.source_connect, self.target_connect = source_connect, target_connect
        self.max_rows, self.max_bytes = max_rows, max_bytes

    def copy(self, plan, *, guard):
        source, target = DatabaseBinding(**plan["source"]), DatabaseBinding(**plan["target"])
        if (
            source.role != "shared_workload"
            or target.role != "dedicated_workload"
            or source.instance_id == target.instance_id
            or (source.organization_id, source.application_id, source.account_id, source.region)
            != (target.organization_id, target.application_id, target.account_id, target.region)
            or source.database_name.startswith("sky_pool_")
            or target.database_name.startswith("sky_pool_")
        ):
            raise ValueError("Copy must bind one workload to a distinct owned instance")
        guard()
        with self.source_connect() as src, self.target_connect() as dst:
            for connection, binding in [(src, source), (dst, target)]:
                if connection.execute("SELECT current_database()").fetchone() != (binding.database_name,):
                    raise ValueError("Connected database differs from approved binding")
                connection.execute("SET LOCAL TIME ZONE 'UTC'")
                connection.execute("SET LOCAL datestyle='ISO, YMD'")
                connection.execute("SET LOCAL lock_timeout='5s'")
                connection.execute("SET LOCAL statement_timeout='60s'")
            dst.execute("SELECT pg_advisory_xact_lock(hashtextextended(current_database(),0))")
            # SHARE blocks INSERT/UPDATE/DELETE/DDL, not reads. Locks remain until
            # the source transaction closes, including target verification/commit.
            tables = _inventory(src)
            if not tables or len(tables) > 100:
                raise ValueError("Source must contain 1..100 supported tables")
            src.execute(
                sql.SQL("LOCK TABLE {} IN SHARE MODE").format(
                    sql.SQL(",").join(sql.Identifier("public", name) for name, _, _ in tables)
                )
            )
            if _inventory(src) != tables:
                raise ValueError("Source schema changed before write freeze")
            if _inventory(dst):
                raise ValueError("Target must be empty; existing data is never overwritten")
            evidence = []
            remaining_rows, remaining_bytes = self.max_rows, self.max_bytes
            for name, columns, primary in tables:
                guard()
                before = _digest(src, name, remaining_rows, remaining_bytes)
                definitions = [
                    sql.SQL("{} {}{}").format(
                        sql.Identifier(column), sql.SQL(kind), sql.SQL(" NOT NULL" if required else "")
                    )
                    for column, kind, required in columns
                ]
                if primary:
                    definitions.append(
                        sql.SQL("PRIMARY KEY ({})").format(sql.SQL(",").join(map(sql.Identifier, primary)))
                    )
                dst.execute(
                    sql.SQL("CREATE TABLE {} ({})").format(
                        sql.Identifier("public", name), sql.SQL(",").join(definitions)
                    )
                )
                with (
                    src.cursor().copy(
                        sql.SQL("COPY {} TO STDOUT").format(sql.Identifier("public", name))
                    ) as read,
                    dst.cursor().copy(
                        sql.SQL("COPY {} FROM STDIN").format(sql.Identifier("public", name))
                    ) as write,
                ):
                    for chunk in read:
                        guard()
                        write.write(chunk)
                after = _digest(dst, name, self.max_rows, self.max_bytes)
                if before != after:
                    raise ValueError("Copied rows failed integrity verification")
                remaining_rows -= before["rows"]
                remaining_bytes -= src.execute(
                    sql.SQL("SELECT COALESCE(sum(octet_length(row_to_json(t)::text)),0) FROM {} t").format(
                        sql.Identifier("public", name)
                    )
                ).fetchone()[0]
                evidence.append({"table": name, **before})
            if _inventory(src) != tables:
                raise ValueError("Source schema changed during copy")
            if _inventory(dst) != tables:
                raise ValueError("Copied schema differs from source")
            guard()
            dst.commit()  # Source writes remain blocked until after this commit.
        return {
            "plan_id": plan["plan_id"],
            "source": plan["source"],
            "target": plan["target"],
            "schema_sha256": hashlib.sha256(json.dumps(tables, sort_keys=True).encode()).hexdigest(),
            "tables": evidence,
            "verified_scope": "locked_table_snapshot_copy",
            "source_retained": True,
            "routing_changed": False,
            "ready_to_cutover": False,
            "requires_final_write_freeze": True,
        }
