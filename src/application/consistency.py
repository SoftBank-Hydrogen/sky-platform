"""Static checks that must agree before an adapter can mutate resources."""

from __future__ import annotations

from adapters.aws.postgres import PostgresRequest
from engine.compatibility import InfrastructureProfile


def check_database_consistency(
    plan: dict,
    final_profile: InfrastructureProfile,
    *,
    postgres_request: PostgresRequest | None = None,
    sqlite_conversion: dict | None = None,
    local_sqlite_binding: dict | None = None,
) -> dict:
    """Check the final source against the selected, executable database binding."""
    compatibility = plan.get("compatibility") or {}
    database = plan.get("database")
    resources = plan.get("resources") or []
    if bool(compatibility.get("postgres_binding")) != (postgres_request is not None):
        raise ValueError("CV-03: PostgreSQL target plan and execution binding disagree")
    if database is None:
        if postgres_request is not None:
            raise ValueError("CV-03: PostgreSQL execution has no target database plan")
    else:
        if not isinstance(database, dict) or database.get("binding") not in {"create", "existing"}:
            raise ValueError("CV-03: Invalid target database binding")
        if postgres_request is None or database.get("database_id") != postgres_request.database_id:
            raise ValueError("CV-03: Target database identity differs from execution binding")
        resource = "new RDS PostgreSQL" if database["binding"] == "create" else "existing RDS PostgreSQL"
        if resource not in resources or final_profile.database_engines != ("postgresql",):
            raise ValueError("CV-03: PostgreSQL resource or final source requirement is missing")

    conversion = plan.get("conversion_pending")
    if (conversion == "sqlite-to-postgresql") != (sqlite_conversion is not None):
        raise ValueError("CV-03: SQLite migration decision and execution input disagree")
    if sqlite_conversion is not None and (
        database is None
        or final_profile.database_engines != ("postgresql",)
        or "sqlite" in final_profile.requirements
    ):
        raise ValueError("CV-03: SQLite source was not transformed to PostgreSQL")

    if bool(compatibility.get("local_sqlite_binding")) != (local_sqlite_binding is not None):
        raise ValueError("CV-03: SQLite target plan and volume binding disagree")
    if local_sqlite_binding is None:
        if plan.get("sqlite_volume") is not None:
            raise ValueError("CV-03: SQLite volume exists without execution binding")
    elif (
        plan.get("sqlite_volume") != local_sqlite_binding
        or "sqlite" not in final_profile.requirements
        or postgres_request is not None
    ):
        raise ValueError("CV-03: SQLite volume or final source requirement differs")

    if postgres_request is not None or local_sqlite_binding is not None:
        status = "pass"
    else:
        # A scan without database signals does not prove the app is stateless.
        status = "unknown"
    return {"id": "CV-03", "status": status, "source": "final_working_copy"}
