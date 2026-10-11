"""App-scoped access to an audited dedicated DB, without changing traffic.

AWS ownership, TLS and Secret identity are caller gates. Credentials are passed
in memory, never returned. Existing unmarked roles/databases are not adopted.
"""

import hashlib
import json
from dataclasses import asdict

from psycopg import sql

from adapters.database.promotion_copy import _inventory
from domain.database import DatabaseBinding


def access_identity(binding):
    if not isinstance(binding, DatabaseBinding) or binding.role != "dedicated_workload":
        raise ValueError("An owned dedicated workload binding is required")
    if binding.database_name.startswith("sky_pool_"):
        raise ValueError("Control database cannot receive application access")
    digest = hashlib.sha256(json.dumps(asdict(binding), sort_keys=True).encode()).hexdigest()[:24]
    return "sky_dedicated_" + digest, "sky-dedicated-access-v1:" + digest


class PostgresDedicatedAccess:
    def __init__(self, admin_connect, application_connect):
        self.admin_connect, self.application_connect = admin_connect, application_connect

    def provision(self, binding, *, password, secret_ref, guard, connection_limit=5):
        role, marker = access_identity(binding)
        if (
            not isinstance(password, str)
            or not password
            or "\0" in password
            or not isinstance(secret_ref, str)
            or not secret_ref
            or type(connection_limit) is not int
            or not 1 <= connection_limit <= 50
        ):
            raise ValueError("Explicit in-memory credentials and bounded connection limit required")
        guard()
        with self.admin_connect() as db:
            if db.execute("SELECT current_database()").fetchone() != (binding.database_name,):
                raise ValueError("Connected database differs from binding")
            if db.execute(
                "SELECT to_regnamespace('sky_state') IS NOT NULL OR to_regnamespace('sky_pool') IS NOT NULL"
            ).fetchone()[0]:
                raise ValueError("Control-plane schema cannot receive app credentials")
            db.execute("SET LOCAL lock_timeout='5s'")
            db.execute("SET LOCAL statement_timeout='30s'")
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(current_database(),0))")
            tables = _inventory(db)
            if not tables:
                raise ValueError("Verified copied workload tables required")
            database_marker = db.execute(
                "SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=current_database()"
            ).fetchone()[0]
            if database_marker not in (None, marker):
                raise ValueError("Database ownership marker differs")
            saved = db.execute(
                "SELECT shobj_description(oid,'pg_authid') FROM pg_roles WHERE rolname=%s", (role,)
            ).fetchone()
            if saved is not None and saved != (marker,):
                raise ValueError("Existing unowned application role")
            if saved is not None:
                flags = db.execute(
                    """SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,
                    rolbypassrls,rolinherit,rolcanlogin,rolconnlimit FROM pg_roles WHERE rolname=%s""",
                    (role,),
                ).fetchone()
                members = db.execute(
                    "SELECT count(*) FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=%s)",
                    (role,),
                ).fetchone()[0]
                if flags != (False, False, False, False, False, False, True, connection_limit) or members:
                    raise ValueError("Existing application role privileges differ")
            guard()
            if saved is None:
                db.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT CONNECTION LIMIT {} PASSWORD {}"
                    ).format(sql.Identifier(role), sql.Literal(connection_limit), sql.Literal(password))
                )
                db.execute(
                    sql.SQL("COMMENT ON ROLE {} IS {}").format(sql.Identifier(role), sql.Literal(marker))
                )
            db.execute(
                sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                    sql.Identifier(binding.database_name), sql.Literal(marker)
                )
            )
            # Remove default public connections to the new workload and standard DBs.
            for name in (binding.database_name, "postgres", "template0", "template1"):
                db.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name)))
            db.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
            db.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(binding.database_name), sql.Identifier(role)
                )
            )
            db.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
            for name, _, _ in tables:
                db.execute(
                    sql.SQL("GRANT SELECT,INSERT,UPDATE,DELETE ON {} TO {}").format(
                        sql.Identifier("public", name), sql.Identifier(role)
                    )
                )
            if db.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_database WHERE datallowconn AND datname<>%s AND has_database_privilege(%s,oid,'CONNECT'))",
                (binding.database_name, role),
            ).fetchone()[0]:
                raise ValueError("Application role can connect to another database")
            guard()
        # An unsuccessful probe does not rotate an existing password or undo a
        # possibly committed role. Caller must reconcile the same Secret version.
        with self.application_connect(role, password) as db:
            if db.execute("SELECT current_database(),current_user").fetchone() != (
                binding.database_name,
                role,
            ):
                raise ValueError("App credential identity mismatch")
            for name, _, _ in tables:
                db.execute(
                    sql.SQL("SELECT 1 FROM {} LIMIT 1").format(sql.Identifier("public", name))
                ).fetchall()
            if db.execute("SELECT has_schema_privilege(current_user,'public','CREATE')").fetchone()[0]:
                raise ValueError("App role must not mutate the schema")
        guard()
        return {
            "binding": asdict(binding),
            "login_role": role,
            "secret_ref": secret_ref,
            "verified_scope": "dedicated_application_acl_and_login",
            "routing_changed": False,
        }
