"""Static checks that must agree before an adapter can mutate resources."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from adapters.aws.postgres import PostgresRequest
from application.deployment_core import DeploymentPlan
from engine.compatibility import InfrastructureProfile


class HealthResultMismatch(ValueError):
    retryable = False


def health_result_matches_plan(plan: dict, result: dict) -> bool:
    """Recognize an adapter's HTTP result for the selected health endpoint."""
    if not isinstance(plan, dict) or not isinstance(result, dict):
        return False
    port = plan.get("port")
    url = result.get("url")
    health_path = plan.get("health_path")
    try:
        parsed = urlsplit(url) if isinstance(url, str) else None
        valid_url = bool(
            parsed and parsed.scheme in {"http", "https"} and parsed.hostname
            and (parsed.port is None or parsed.port > 0)
            and not parsed.username and not parsed.password
            and not parsed.path and not parsed.query and not parsed.fragment
        )
    except ValueError:
        valid_url = False
    return bool(
        type(port) is int and 1024 <= port <= 65535
        and valid_url
        and isinstance(health_path, str) and health_path.startswith("/")
        and result.get("health_url") == url + health_path
    )


def require_health_result(plan: dict, result: dict) -> None:
    if not health_result_matches_plan(plan, result):
        raise HealthResultMismatch("CV-06: Adapter HTTP verification does not match the executable health endpoint")


def check_port_consistency(plan: DeploymentPlan) -> dict:
    """Compare the executable HTTP port with unambiguous final-image declarations.

    EXPOSE is metadata, not proof that the process listens on this port. The
    target's HTTP probe remains responsible for checking the running service.
    """
    result = {"id": "CV-06", "status": "unknown", "source": "executable_dockerfile"}
    if plan.dockerfile_source == "generated":
        # The generated image declares the planned port, but the application
        # may still bind another port or the loopback interface.
        return result
    if plan.dockerfile_source != "existing":
        return result

    # Only the final stage describes the deployed image. Avoid interpreting
    # variables, line continuations, or Dockerfile extensions as fixed ports.
    final_stage = None
    for line in plan.dockerfile.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        instruction = re.match(r"([A-Za-z]+)\s+(.+)", stripped)
        if instruction is None:
            continue
        command, arguments = instruction.groups()
        if command.upper() == "FROM":
            final_stage = []
        elif command.upper() == "EXPOSE" and final_stage is not None:
            final_stage.append(arguments)
    if not final_stage or any("\\" in item for item in final_stage):
        return result

    ports = set()
    for item in final_stage:
        for token in item.split():
            declaration = re.fullmatch(r"(\d{1,5})(?:/(tcp|udp))?", token, re.IGNORECASE)
            if declaration is None:
                return result
            if declaration.group(2) is None or declaration.group(2).lower() == "tcp":
                ports.add(int(declaration.group(1)))
    if plan.port not in ports:
        raise ValueError(f"CV-06: Final Dockerfile EXPOSE disagrees with HTTP port {plan.port}")
    return result


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
