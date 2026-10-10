"""Allocate isolated app DBs on an explicitly registered workload PostgreSQL.

Connection providers are worker-side dependencies. This adapter does not prove
AWS tags, TLS, secret ownership or network isolation; those are caller gates.
"""

import hashlib
import json
import re
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime

from domain.shared_database import PoolAllocationRequest, SharedDatabasePool
from ports.shared_database import (
    AllocationCredentials,
    PoolAllocationConflict,
    PoolAllocationError,
    PoolCapacityExceeded,
)


class PostgresSharedPool:
    def __init__(self, pool: SharedDatabasePool, connect_admin, connect_application):
        self.pool = pool
        self.connect_admin = connect_admin
        self.connect_application = connect_application
        identity = [pool.id, pool.account_id, pool.region, pool.instance_id, pool.control_database]
        digest = hashlib.sha256(json.dumps(identity).encode()).digest()
        self.lock_key = int.from_bytes(digest[:8], "big", signed=True)

    @contextmanager
    def _locked(self):
        with self.connect_admin(self.pool.control_database) as connection:
            connection.autocommit = True
            name = connection.execute("SELECT current_database()").fetchone()[0]
            state = connection.execute("SELECT to_regnamespace('sky_state') IS NOT NULL").fetchone()[0]
            if name != self.pool.control_database or state:
                raise PoolAllocationError("Workload pool must be separate from Sky state")
            connection.execute("SELECT pg_advisory_lock(%s)", (self.lock_key,))
            try:
                yield connection
            finally:
                if not connection.closed:
                    connection.execute("SELECT pg_advisory_unlock(%s)", (self.lock_key,))

    def initialize(self):
        """Explicitly register a dedicated workload cluster; never adopt unrelated DBs."""
        from psycopg import sql
        from psycopg.types.json import Jsonb

        with self._locked() as connection:
            with connection.transaction():
                connection.execute("CREATE SCHEMA IF NOT EXISTS sky_pool")
                connection.execute("REVOKE ALL ON SCHEMA sky_pool FROM PUBLIC")
                connection.execute("""CREATE TABLE IF NOT EXISTS sky_pool.identity (
                    singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), value jsonb NOT NULL)""")
                connection.execute("""CREATE TABLE IF NOT EXISTS sky_pool.allocations (
                    id text PRIMARY KEY, request jsonb NOT NULL, state text NOT NULL,
                    CHECK(state IN ('provisioning','ready','needs_attention')))""")
                saved = connection.execute("SELECT value FROM sky_pool.identity").fetchone()
                if saved and saved[0] != asdict(self.pool):
                    raise PoolAllocationConflict("Pool identity or budget differs from registration")
                self._inventory(connection, public_check=False)
                if not saved:
                    connection.execute(
                        "INSERT INTO sky_pool.identity(value) VALUES (%s)", (Jsonb(asdict(self.pool)),)
                    )
                for name in ("postgres", "template0", "template1", self.pool.control_database):
                    connection.execute(
                        sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name))
                    )
            self._inventory(connection)

    def _registration(self, connection):
        if connection.execute("SELECT to_regclass('sky_pool.identity')").fetchone()[0] is None:
            raise PoolAllocationError("Workload pool is not registered")
        value = connection.execute("SELECT value FROM sky_pool.identity").fetchone()
        if not value or value[0] != asdict(self.pool):
            raise PoolAllocationError("Workload pool is not registered")

    def _inventory(self, connection, *, public_check=True):
        rows = connection.execute("SELECT request FROM sky_pool.allocations").fetchall()
        allowed = {"postgres", "template0", "template1", self.pool.control_database}
        allowed.update(row[0]["database_name"] for row in rows)
        databases = connection.execute("""SELECT datname, datallowconn, EXISTS (
            SELECT 1 FROM aclexplode(COALESCE(datacl, acldefault('d', datdba)))
            WHERE grantee=0 AND privilege_type='CONNECT') FROM pg_database""").fetchall()
        if any(name not in allowed for name, _, _ in databases):
            raise PoolAllocationError("Unregistered database exists on the workload pool")
        if public_check and any(connectable and public for _, connectable, public in databases):
            raise PoolAllocationError("PUBLIC database access defeats tenant isolation")
        if public_check:
            for (request,) in rows:
                role = request["login_role"]
                if connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                    foreign = connection.execute(
                        """SELECT datname FROM pg_database WHERE datallowconn
                        AND datname<>%s AND has_database_privilege(%s,oid,'CONNECT')""",
                        (request["database_name"], role),
                    ).fetchone()
                    if foreign:
                        raise PoolAllocationError("An app role can access another pool database")

    def _marker(self, connection, kind, name):
        if kind == "role":
            return connection.execute(
                "SELECT shobj_description(oid,'pg_authid') FROM pg_roles WHERE rolname=%s", (name,)
            ).fetchone()
        return connection.execute(
            "SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=%s", (name,)
        ).fetchone()

    def _verify_role(self, connection, request):
        role = connection.execute(
            """SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication,
            rolbypassrls, rolinherit, rolconnlimit FROM pg_roles WHERE rolname=%s""",
            (request.login_role,),
        ).fetchone()
        memberships = connection.execute(
            """SELECT count(*) FROM pg_auth_members
            WHERE member=(SELECT oid FROM pg_roles WHERE rolname=%s)""",
            (request.login_role,),
        ).fetchone()[0]
        if role != (False, False, False, False, False, False, request.connection_limit) or memberships:
            raise PoolAllocationConflict("App role privileges differ from the allocation")

    def allocate(self, request: PoolAllocationRequest, credentials: AllocationCredentials) -> dict:
        from psycopg import Error

        try:
            return self._allocate(request, credentials)
        except (Error, OSError):
            raise PoolAllocationError(
                "Pool connection or allocation outcome requires reconciliation"
            ) from None

    def _allocate(self, request: PoolAllocationRequest, credentials: AllocationCredentials) -> dict:
        from psycopg import Error
        from psycopg.types.json import Jsonb

        if request.pool != self.pool:
            raise PoolAllocationError("Allocation pool differs from the registered adapter")
        prefix = f"arn:aws:secretsmanager:{self.pool.region}:{self.pool.account_id}:secret:sky-pool/{self.pool.id}/{request.id}-"
        if not re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9]{6}", credentials.secret_ref):
            raise PoolAllocationError("A separate app credential reference is required")
        payload = {
            **asdict(request),
            "database_name": request.database_name,
            "login_role": request.login_role,
            "secret_ref": credentials.secret_ref,
        }
        with self._locked() as connection:
            self._registration(connection)
            self._inventory(connection)
            row = connection.execute(
                "SELECT request,state FROM sky_pool.allocations WHERE id=%s", (request.id,)
            ).fetchone()
            if row and row[0] != payload:
                raise PoolAllocationConflict("Existing allocation has different settings")
            if row and row[1] == "needs_attention":
                raise PoolAllocationError("Uncertain allocation requires reconciliation")
            if not row:
                reserved = connection.execute(
                    "SELECT COALESCE(sum((request->>'connection_limit')::integer),0) FROM sky_pool.allocations"
                ).fetchone()[0]
                if reserved + request.connection_limit > self.pool.connection_budget:
                    raise PoolCapacityExceeded("Pool app connection reservations are exhausted")
                connection.execute(
                    "INSERT INTO sky_pool.allocations VALUES (%s,%s,'provisioning')",
                    (request.id, Jsonb(payload)),
                )
            try:
                if not row or row[1] != "ready":
                    self._prepare(connection, request, credentials)
                self._verify(connection, request, credentials)
            except PoolAllocationError:
                connection.execute(
                    "UPDATE sky_pool.allocations SET state='needs_attention' WHERE id=%s", (request.id,)
                )
                raise
            except (Error, OSError):
                connection.execute(
                    "UPDATE sky_pool.allocations SET state='needs_attention' WHERE id=%s", (request.id,)
                )
                raise PoolAllocationError("Allocation outcome requires reconciliation") from None
            connection.execute("UPDATE sky_pool.allocations SET state='ready' WHERE id=%s", (request.id,))
            return {
                "allocation_id": request.id,
                "binding": asdict(request.binding()),
                "login_role": request.login_role,
                "secret_ref": credentials.secret_ref,
                "connection_limit": request.connection_limit,
                "status": "ready",
                "checked_at": datetime.now(UTC).isoformat(),
                "verified_scope": "postgresql_role_and_database_acl",
            }

    def _prepare(self, connection, request, credentials):
        from psycopg import sql

        role, database = sql.Identifier(request.login_role), sql.Identifier(request.database_name)
        marker = self._marker(connection, "role", request.login_role)
        if marker is None:
            with connection.transaction():
                connection.execute(
                    sql.SQL(
                        "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT {}"
                    ).format(role, sql.Literal(request.connection_limit))
                )
                connection.execute(
                    sql.SQL("COMMENT ON ROLE {} IS {}").format(role, sql.Literal(request.ownership_marker))
                )
        elif marker[0] != request.ownership_marker:
            raise PoolAllocationConflict("App role is not owned by this allocation")
        self._verify_role(connection, request)
        marker = self._marker(connection, "database", request.database_name)
        if marker is None:
            # CREATE DATABASE cannot share a transaction with its ownership marker.
            # An unmarked DB after an interrupted request is preserved for review.
            connection.execute(
                sql.SQL(
                    "CREATE DATABASE {} TEMPLATE template0 ALLOW_CONNECTIONS false CONNECTION LIMIT {}"
                ).format(database, sql.Literal(request.connection_limit))
            )
            connection.execute(
                sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                    database, sql.Literal(request.ownership_marker)
                )
            )
        elif marker[0] != request.ownership_marker:
            raise PoolAllocationConflict("App DB is not owned by this allocation")
        owner = connection.execute(
            "SELECT datdba=(SELECT oid FROM pg_roles WHERE rolname=current_user) FROM pg_database WHERE datname=%s",
            (request.database_name,),
        ).fetchone()[0]
        if not owner:
            raise PoolAllocationConflict("App DB administrative owner changed")
        connection.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(database))
        connection.execute(sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {}").format(database, role))
        connection.execute(sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS true").format(database))
        with self.connect_admin(request.database_name) as tenant:
            tenant.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
            tenant.execute(sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {}").format(role))
        login = connection.execute(
            "SELECT rolcanlogin FROM pg_roles WHERE rolname=%s", (request.login_role,)
        ).fetchone()[0]
        if not login:
            verifier = connection.pgconn.encrypt_password(
                credentials.password.encode(), request.login_role.encode(), b"scram-sha-256"
            ).decode()
            connection.execute(sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(role, sql.Literal(verifier)))

    def _verify(self, connection, request, credentials):
        self._inventory(connection)
        if self._marker(connection, "role", request.login_role) != (
            request.ownership_marker,
        ) or self._marker(connection, "database", request.database_name) != (request.ownership_marker,):
            raise PoolAllocationConflict("Allocation resources are missing or ownership changed")
        self._verify_role(connection, request)
        database = connection.execute(
            """SELECT datdba=(SELECT oid FROM pg_roles WHERE rolname=current_user),
            datallowconn, datconnlimit FROM pg_database WHERE datname=%s""",
            (request.database_name,),
        ).fetchone()
        login = connection.execute(
            "SELECT rolcanlogin FROM pg_roles WHERE rolname=%s", (request.login_role,)
        ).fetchone()
        if database != (True, True, request.connection_limit) or login != (True,):
            raise PoolAllocationConflict("Allocation database or login settings changed")
        permitted = connection.execute(
            """SELECT datname FROM pg_database WHERE datallowconn
            AND has_database_privilege(%s,oid,'CONNECT') ORDER BY datname""",
            (request.login_role,),
        ).fetchall()
        if permitted != [(request.database_name,)]:
            raise PoolAllocationConflict("App credentials can access another database")
        with self.connect_application(
            request.database_name, request.login_role, credentials.password
        ) as tenant:
            identity = tenant.execute("SELECT current_database(),current_user").fetchone()
            if identity != (request.database_name, request.login_role):
                raise PoolAllocationError("App login does not match its allocation")
