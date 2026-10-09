"""Static checks that must agree before an adapter can mutate resources."""

from __future__ import annotations

import re
import hashlib
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from adapters.aws.postgres import PostgresRequest
from adapters.database.migrations import MigrationBundle
from adapters.database.sqlite_snapshot import compile_sqlite_snapshot
from application.deployment_core import DeploymentPlan, SOURCE_FILENAMES, SOURCE_SUFFIXES
from engine.capability_registry import RESOURCE_CAPABILITY_IDS, target_capability_model
from engine.compatibility import InfrastructureProfile
from engine.deployment_policy import DeploymentPolicy


class HealthResultMismatch(ValueError):
    retryable = False


def check_source_change_scope(record: dict, original: Path, sqlite_conversion: dict | None = None,
                              npm_lock_sync: dict | None = None) -> dict:
    """Check verified source changes against edit scope and the reviewed SQLite exception."""
    changes = record.get("changes") if isinstance(record, dict) else None
    if not isinstance(changes, list):
        raise ValueError("CV-02: Applied source change record is missing")
    migration_path = "migrations/0000_sky_sqlite_import.sql"
    allowed_conversion = {}
    if sqlite_conversion is not None:
        path = sqlite_conversion.get("path") if isinstance(sqlite_conversion, dict) else None
        if not isinstance(path, str) or not path or path == migration_path:
            raise ValueError("CV-02: SQLite conversion path is invalid")
        source = PurePosixPath(path)
        if source.is_absolute() or ".." in source.parts or source.as_posix() != path:
            raise ValueError("CV-02: SQLite conversion path is invalid")
        try:
            snapshot = compile_sqlite_snapshot(original.joinpath(*source.parts))
        except ValueError:
            raise ValueError("CV-02: Approved SQLite snapshot is no longer valid") from None
        if (snapshot.source_sha256 != sqlite_conversion.get("source_sha256")
                or snapshot.row_counts != sqlite_conversion.get("row_counts")
                or snapshot.schema != sqlite_conversion.get("schema")):
            raise ValueError("CV-02: SQLite conversion approval differs from the uploaded source")
        allowed_conversion = {
            path: (snapshot.source_sha256, None),
            migration_path: (None, hashlib.sha256(snapshot.sql.encode()).hexdigest()),
        }

    allowed_lock = None
    if npm_lock_sync is not None:
        if not isinstance(npm_lock_sync, dict) or npm_lock_sync.get("generator") != "isolated_npm":
            raise ValueError("CV-02: npm lockfile sync record is invalid")
        manifest = original / "package.json"
        lock = original / "package-lock.json"
        if (manifest.is_symlink() or lock.is_symlink() or not manifest.is_file() or not lock.is_file()
                or hashlib.sha256(lock.read_bytes()).hexdigest() != npm_lock_sync.get("before_sha256")):
            raise ValueError("CV-02: npm lockfile sync source differs from the uploaded source")
        allowed_lock = (npm_lock_sync.get("before_sha256"), npm_lock_sync.get("after_sha256"))
        if not isinstance(allowed_lock[1], str) or not re.fullmatch(r"[0-9a-f]{64}", allowed_lock[1]):
            raise ValueError("CV-02: npm lockfile sync digest is invalid")

    observed = set()
    for item in changes:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("CV-02: Applied source change is invalid")
        name = item["path"]
        path = PurePosixPath(name)
        if (not name or path.is_absolute() or ".." in path.parts or "\\" in name
                or path.as_posix() != name or name in observed
                or any(part.startswith(".") or part in {"node_modules", "dist", "build", "vendor"}
                       for part in path.parts)):
            raise ValueError(f"CV-02: Source change path is outside the allowlist: {name}")
        observed.add(name)
        hashes = (item.get("before_sha256"), item.get("after_sha256"))
        if name in allowed_conversion:
            if hashes != allowed_conversion[name]:
                raise ValueError(f"CV-02: Approved SQLite conversion changed unexpectedly: {name}")
            continue
        if name == "package-lock.json" and allowed_lock is not None:
            if hashes != allowed_lock:
                raise ValueError("CV-02: npm lockfile differs from the isolated sync result")
            continue
        if (hashes[1] is None or name in {"package-lock.json", "Gemfile.lock", "poetry.lock",
                                         "go.sum", "Cargo.lock"}
                or (name == "Dockerfile" and not (original / name).is_file())
                or (name != "Dockerfile" and path.suffix not in SOURCE_SUFFIXES
                    and path.name not in SOURCE_FILENAMES)):
            raise ValueError(f"CV-02: Source change is outside the allowlist: {name}")
    if set(allowed_conversion) - observed:
        raise ValueError("CV-02: Approved SQLite conversion is incomplete")
    if allowed_lock is not None:
        manifest_change = next((item for item in changes if item["path"] == "package.json"), None)
        if ("package-lock.json" not in observed or manifest_change is None
                or manifest_change.get("before_sha256") != hashlib.sha256(manifest.read_bytes()).hexdigest()
                or manifest_change.get("after_sha256") != npm_lock_sync.get("manifest_sha256")):
            raise ValueError("CV-02: npm lockfile sync is not bound to the changed manifest")
    return {"id": "CV-02", "status": "pass", "source": "applied_source_transform"}


def check_sqlite_migration_consistency(
    plan: dict,
    final_profile: InfrastructureProfile,
    original: Path,
    conversion: dict,
    migrations: MigrationBundle | None,
    postgres_request: PostgresRequest | None,
    policy: DeploymentPolicy | None,
) -> dict:
    """Bind the approved SQLite snapshot to the exact SQL scheduled for PostgreSQL."""
    if policy is None or not policy.allow_data_migration:
        raise ValueError("CV-04: SQLite data migration was not approved")
    database = plan.get("database") if isinstance(plan, dict) else None
    resources = plan.get("resources") if isinstance(plan, dict) else None
    if (not isinstance(plan, dict)
            or plan.get("conversion_pending") != "sqlite-to-postgresql"
            or plan.get("target") != "aws-ecs-express"
            or not isinstance(database, dict)
            or database.get("binding") not in {"create", "existing"}
            or postgres_request is None
            or database.get("database_id") != postgres_request.database_id
            or not isinstance(resources, list)
            or "one-off SQL migration task" not in resources
            or ("new RDS PostgreSQL" if database.get("binding") == "create" else "existing RDS PostgreSQL")
            not in resources
            or final_profile.database_engines != ("postgresql",)
            or "sqlite" in final_profile.requirements):
        raise ValueError("CV-04: SQLite conversion and PostgreSQL execution plan disagree")
    path = conversion.get("path") if isinstance(conversion, dict) else None
    if not isinstance(path, str) or not path or "\\" in path:
        raise ValueError("CV-04: Reviewed SQLite source path is invalid")
    relative = PurePosixPath(path)
    if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != path:
        raise ValueError("CV-04: Reviewed SQLite source path is invalid")
    try:
        snapshot = compile_sqlite_snapshot(original.joinpath(*relative.parts))
    except ValueError:
        raise ValueError("CV-04: Reviewed SQLite snapshot is no longer valid") from None
    if (snapshot.source_sha256 != conversion.get("source_sha256")
            or snapshot.schema != conversion.get("schema")
            or snapshot.row_counts != conversion.get("row_counts")):
        raise ValueError("CV-04: SQLite snapshot differs from the reviewed source")
    expected_sql_hash = hashlib.sha256(snapshot.sql.encode()).hexdigest()
    if (not isinstance(migrations, MigrationBundle)
            or len(migrations.migrations) != 1
            or migrations.migrations[0].name != "0000_sky_sqlite_import.sql"
            or migrations.migrations[0].sha256 != expected_sql_hash):
        raise ValueError("CV-04: Scheduled SQL does not match the reviewed SQLite snapshot")
    return {"id": "CV-04", "status": "pass", "source": "reviewed_sqlite_migration"}


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
        and ingress_result_matches_target(plan, parsed)
        and isinstance(health_path, str) and health_path.startswith("/")
        and result.get("health_url") == url + health_path
    )


def ingress_result_matches_target(plan: dict, parsed) -> bool:
    """Check only the ingress properties the current adapters can prove."""
    target = plan.get("target")
    if parsed is None:
        return False
    if target in {"local-docker", "onprem-compose"}:
        return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"}
    if target in {"aws-ecs-express", "cloud-run"}:
        return parsed.scheme == "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
    return False


def require_health_result(plan: dict, result: dict) -> None:
    if isinstance(plan, dict) and isinstance(result, dict) and isinstance(result.get("url"), str):
        try:
            parsed = urlsplit(result["url"])
        except ValueError:
            parsed = None
        if parsed is not None and parsed.scheme in {"http", "https"} and parsed.hostname:
            if not ingress_result_matches_target(plan, parsed):
                raise HealthResultMismatch("CV-05: Adapter ingress contradicts the deployment target")
    if not health_result_matches_plan(plan, result):
        raise HealthResultMismatch("CV-06: Adapter HTTP verification does not match the executable health endpoint")


def check_target_resource_consistency(compilation: dict, infrastructure_plan: dict, target: str) -> dict:
    """Reject compiled resources without an implemented adapter path."""
    target_plan = compilation.get("target_plan") if isinstance(compilation, dict) else None
    resources = target_plan.get("resources") if isinstance(target_plan, dict) else None
    if (
        not isinstance(infrastructure_plan, dict)
        or not isinstance(resources, list)
        or not resources
        or target_plan.get("target") != target
        or infrastructure_plan.get("target") != target
        or not isinstance(infrastructure_plan.get("compatibility"), dict)
        or resources != infrastructure_plan.get("resources")
        or not all(isinstance(item, str) for item in resources)
        or len(set(resources)) != len(resources)
    ):
        raise ValueError("CV-09: Target resources disagree with the compiled execution target")
    try:
        capabilities = {item.id: item for item in target_capability_model(target).capabilities}
    except ValueError:
        raise ValueError("CV-09: Unsupported execution target") from None
    for resource in resources:
        capability_id = RESOURCE_CAPABILITY_IDS.get(resource)
        capability = capabilities.get(capability_id)
        if capability is None or capability.sky_adapter_support != "implemented":
            raise ValueError(f"CV-09: No implemented adapter capability for resource {resource}")
    access_mode = target_plan.get("access_mode")
    access_capability = capabilities.get(f"access_{access_mode}")
    if (
        access_mode != infrastructure_plan.get("compatibility", {}).get("access_mode")
        or access_capability is None
        or access_capability.sky_adapter_support != "implemented"
    ):
        raise ValueError("CV-09: Target access mode is not implemented by the adapter")
    return {"id": "CV-09", "status": "pass", "source": "compiled_target_plan"}


def check_websocket_state_consistency(compilation: dict) -> dict | None:
    """Keep process-local WebSocket state unresolved despite a one-replica plan."""
    deployment_ir = compilation.get("deployment_ir") if isinstance(compilation, dict) else None
    if not isinstance(deployment_ir, dict):
        raise ValueError("CV-08: Compiled deployment IR is missing")
    unknowns = deployment_ir.get("unknowns")
    if not isinstance(unknowns, (list, tuple)):
        raise ValueError("CV-08: Compiled state uncertainty is missing")
    if "session_affinity_behavior" not in unknowns:
        return None
    services = deployment_ir.get("services")
    if (not isinstance(services, list) or len(services) != 1
            or not isinstance(services[0], dict)
            or services[0].get("id") != "source-bundle"
            or type(services[0].get("replicas")) is not int
            or services[0]["replicas"] != 1):
        raise ValueError("CV-08: Process-local WebSocket state requires a one-replica plan")
    return {"id": "CV-08", "status": "unknown", "source": "websocket_state_and_replica_plan"}


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
