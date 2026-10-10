"""Static checks that must agree before an adapter can mutate resources."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from adapters.aws.postgres import PostgresRequest
from adapters.database.migrations import MigrationBundle
from adapters.database.sqlite_snapshot import compile_sqlite_snapshot
from application.deployment_core import SOURCE_FILENAMES, SOURCE_SUFFIXES, DeploymentPlan, source_digest
from engine.capability_registry import RESOURCE_CAPABILITY_IDS, target_capability_model
from engine.compatibility import DATABASE_ENGINE_SOURCE, SQLITE_SOURCE, InfrastructureProfile
from engine.deployment_policy import DeploymentPolicy
from engine.target_lowering import lower_target_configuration


class HealthResultMismatch(ValueError):
    retryable = False


_ASYNC_METHOD = re.compile(r"\b([A-Za-z_$][\w$]*)\s*:\s*async\b|\basync\s+([A-Za-z_$][\w$]*)\s*\(")
# Extended form used by the Cloud Run path: also `async function name(` and async database factories.
_ASYNC_METHOD_EXTENDED = re.compile(
    r"\b([A-Za-z_$][\w$]*)\s*:\s*async\b|\basync\s+(?:function\s+)?([A-Za-z_$][\w$]*)\s*\(")
_NODE_POSTGRES_IMPORT = re.compile(r"\b(?:require\s*\(\s*|from\s+)['\"](pg|postgres)['\"]")

PG_NUMERIC_PARSERS = """const { types } = require('pg');
const toNumber = v => { const n = Number(v); return Number.isSafeInteger(n) ? n : v; };
types.setTypeParser(20, toNumber);    // BIGINT, COUNT(*)
types.setTypeParser(1700, v => { const n = Number(v); return Number.isFinite(n) && (Number.isSafeInteger(n) || !Number.isInteger(n)) ? n : v; });  // SUM/AVG 등"""

_JS_TRIVIA = re.compile(r"//[^\n]*|/\*[\s\S]*?\*/|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`")
_JS_NAME = r"[A-Za-z_$][\w$]*"


def _js_mask(source: str, *, strings: bool) -> str:
    """Keep offsets/lines while excluding comments and, optionally, string contents."""
    def mask(match):
        text = match[0]
        if not strings and not text.startswith(("//", "/*")):
            return text
        return re.sub(r"[^\n]", " ", text)
    return _JS_TRIVIA.sub(mask, source)


def _module_statement(code: str, offset: int) -> bool:
    # Require eager, standalone module initialization, never a function/conditional callback.
    prefix = code[:offset].rstrip()
    return (all(prefix.count(a) == prefix.count(b) for a, b in (("{", "}"), ("(", ")"), ("[", "]")))
            and (not prefix or prefix[-1] in ";}"))


def check_postgres_numeric_parsers(work: Path) -> None:
    """Conservative CV-04 guard for pg modules in approved SQLite conversions.

    Require both registrations in each importing module, at module scope before Pool/Client
    construction or queries. Cross-module initialization and deferred registration are not proven
    by this check: use the supplied local initialization pattern instead. Callback semantics still
    require runtime/API tests; this is not a general JavaScript execution analyser.
    """
    for source in sorted(work.rglob("*")):
        if (source.suffix not in {".js", ".cjs", ".mjs"} or source.is_symlink() or not source.is_file()
                or any(p in {"node_modules", "dist", "build", "vendor", "tests"}
                       for p in source.relative_to(work).parts)):
            continue
        raw = source.read_text(encoding="utf-8", errors="replace")
        imports = _js_mask(raw, strings=False)
        if not re.search(r"\b(?:require\s*\(\s*|from\s+)['\"]pg['\"]", imports):
            continue
        code = _js_mask(raw, strings=True)
        namespaces, types, constructors, ready = set(), set(), set(), {}
        # const pg = require('pg'); import pg from 'pg'; import * as pg from 'pg';
        for pattern in (
            rf"\b(?:const|let|var)\s+({_JS_NAME})\s*=\s*require\s*\(\s*['\"]pg['\"]\s*\)",
            rf"\bimport\s+({_JS_NAME})\s*(?:,\s*\{{[^}}]*\}}\s*)?from\s*['\"]pg['\"]",
            rf"\bimport\s+\*\s+as\s+({_JS_NAME})\s+from\s*['\"]pg['\"]",
        ):
            for match in re.finditer(pattern, imports):
                if _module_statement(code, match.start()):
                    namespaces.add(match[1])
                    ready[match[1] + '.types'] = match.end()
        # Named CJS/ESM bindings, including renamed bindings.
        bindings = list(re.finditer(r"\b(?:const|let|var)\s*\{([^}]+)\}\s*=\s*require\s*\(\s*['\"]pg['\"]", imports))
        bindings += list(re.finditer(r"\bimport\s*(?:" + _JS_NAME + r"\s*,\s*)?\{([^}]+)\}\s*from\s*['\"]pg['\"]", imports))
        for namespace in namespaces:
            bindings += list(re.finditer(r"\b(?:const|let|var)\s*\{([^}]+)\}\s*=\s*" + re.escape(namespace) + r"\b", imports))
        for binding in bindings:
            if not _module_statement(code, binding.start()):
                continue
            for part in binding[1].split(","):
                m = re.fullmatch(rf"\s*(types|Pool|Client)(?:\s*(?::|\bas\b)\s*({_JS_NAME}))?\s*", part)
                if m:
                    (types if m[1] == "types" else constructors).add(m[2] or m[1])
                    if m[1] == "types":
                        ready[m[2] or m[1]] = binding.end()
        receivers = [re.escape(n) + r"\s*\.\s*types" for n in namespaces] + [re.escape(t) for t in types]
        constructor_names = [re.escape(n) + r"\s*\.\s*(?:Pool|Client)" for n in namespaces]
        constructor_names += list(map(re.escape, constructors))
        uses = [m.start() for m in re.finditer(r"\.\s*(?:query|connect)\s*\(", code)]
        if constructor_names:
            uses += [m.start() for m in re.finditer(r"\bnew\s+(?:" + "|".join(constructor_names) + r")\s*\(", code)]
        cutoff = min(uses, default=len(code))
        registered = set()
        if receivers:
            receiver = r"(?:" + "|".join(receivers) + r")"
            pattern = (r"(?<![\w$.])(?P<receiver>" + receiver + r")\s*\.\s*setTypeParser\s*\(\s*"
                       r"(?P<oid>20|1700|" + receiver + r"\s*\.\s*builtins\s*\.\s*(?:INT8|NUMERIC))\s*,")
            for match in re.finditer(pattern, code):
                name = re.sub(r"\s", "", match['receiver'])
                if not _module_statement(code, match.start()) or match.start() < ready[name]:
                    continue
                # Registration must finish before any connection/query. A parser registered after
                # constructing a Pool is rejected even if a later query might happen to be safe.
                depth, end = 1, match.end()
                for end in range(match.end(), len(code)):
                    if code[end] == "(":
                        depth += 1
                    elif code[end] == ")":
                        depth -= 1
                        if depth == 0:
                            break
                if depth == 0 and end < cutoff and code[match.end():end].strip():
                    oid = re.sub(r"\s", "", match['oid'])
                    registered.add(20 if oid == "20" or oid.endswith(".INT8") else 1700)
        if registered != {20, 1700}:
            path = source.relative_to(work).as_posix()
            raise ValueError(
                f"CV-04: {path}: pg INT8(20) and NUMERIC(1700) parsers must be registered at module "
                "scope before Pool/Client construction and the first query. Register once in each pg "
                "connection module; do not patch individual JSON fields. Use this code (ESM: replace "
                "the require line with import { types } from 'pg';):\n" + PG_NUMERIC_PARSERS)


def check_postgres_node_dependency(work: Path) -> None:
    """Require the runtime manifest and existing lock to include imported PostgreSQL drivers."""
    manifest_path = work / "package.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return
    imported = set()
    for source in work.rglob("*.js"):
        if (source.is_symlink() or not source.is_file()
                or any(part in {"node_modules", "dist", "build", "vendor", "tests"}
                       for part in source.relative_to(work).parts)):
            continue
        imported.update(_NODE_POSTGRES_IMPORT.findall(source.read_text(encoding="utf-8", errors="replace")))
    if not imported:
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        dependencies = manifest["dependencies"]
    except (UnicodeDecodeError, ValueError, KeyError, TypeError):
        raise ValueError("CV-04: Node PostgreSQL runtime dependencies are missing") from None
    if not isinstance(dependencies, dict) or any(not isinstance(dependencies.get(name), str)
                                                   for name in imported):
        raise ValueError("CV-04: Imported PostgreSQL driver is missing from runtime dependencies")
    lock_path = work / "package-lock.json"
    if lock_path.is_file():
        if lock_path.is_symlink():
            raise ValueError("CV-04: npm lockfile must be a regular file")
        try:
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            locked = lock["packages"][""]["dependencies"]
        except (UnicodeDecodeError, ValueError, KeyError, TypeError):
            raise ValueError("CV-04: npm lockfile lacks runtime dependency metadata") from None
        if not isinstance(locked, dict) or any(locked.get(name) != dependencies[name] for name in imported):
            raise ValueError("CV-04: npm lockfile does not match the PostgreSQL runtime dependencies")


def check_async_database_callers(original: Path, work: Path, *, include_factories: bool = False) -> None:
    """Reject directly unhandled JS calls when SQLite methods become async.

    This is a narrow safety check, not proof that all runtime paths work.
    include_factories adds the Cloud Run checks: `async function` declarations, database factory calls
    that became async, and source excerpts in the error. The default keeps the AWS-verified behaviour.
    """
    async_method = _ASYNC_METHOD_EXTENDED if include_factories else _ASYNC_METHOD
    for module in original.rglob("*.js"):
        if (module.is_symlink() or not module.is_file()
                or any(part in {"node_modules", "dist", "build", "vendor", "tests"}
                       for part in module.relative_to(original).parts)):
            continue
        relative = module.relative_to(original)
        converted = work / relative
        if not converted.is_file() or converted.is_symlink():
            continue
        old = module.read_text(encoding="utf-8", errors="replace")
        new = converted.read_text(encoding="utf-8", errors="replace")
        if not SQLITE_SOURCE.search(old) or not DATABASE_ENGINE_SOURCE["postgresql"].search(new):
            continue
        old_async = {name for match in async_method.finditer(old) for name in match.groups() if name}
        new_async = {name for match in async_method.finditer(new) for name in match.groups() if name}
        gained = new_async - old_async
        if not gained:
            continue
        exported = re.search(r"\bmodule\.exports\s*=\s*\{([^}]*)\}", old)
        factories = (set(re.findall(r"(?:^|,)\s*([A-Za-z_$][\w$]*)\s*(?=,|:|$)", exported[1]))
                     if exported else set())
        factories.update(re.findall(r"\bexport\s+(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", old))
        if not factories:
            continue
        factory_pattern = "|".join(map(re.escape, sorted(factories)))
        for caller in original.rglob("*.js"):
            if caller == module or caller.is_symlink() or not caller.is_file():
                continue
            caller_relative = caller.relative_to(original)
            if any(part in {"node_modules", "dist", "build", "vendor", "tests"}
                   for part in caller_relative.parts):
                continue
            updated = work / caller_relative
            if not updated.is_file() or updated.is_symlink():
                continue
            source = updated.read_text(encoding="utf-8", errors="replace")
            # These are project-relative names, not paths relative to the server's process cwd.
            # Anchor both in a virtual POSIX root so a detached/deleted cwd cannot break this check.
            specifier = posixpath.relpath("/" + relative.with_suffix("").as_posix(),
                                          "/" + caller_relative.parent.as_posix())
            if not specifier.startswith("."):
                specifier = "./" + specifier
            import_pattern = (r"(?:require\s*\(\s*|from\s+)['\"]"
                              + re.escape(specifier) + r"(?:\.js)?['\"]")
            if not re.search(import_pattern, source):
                continue
            original_source = caller.read_text(encoding="utf-8", errors="replace")
            receivers = set(re.findall(
                r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=[^\n]*\b(?:"
                + factory_pattern + r")\s*\(",
                original_source,
            ))
            if not receivers and not (include_factories and gained.intersection(factories)):
                continue
            unhandled = []
            excerpts = []
            lines = source.splitlines()
            for name in sorted(gained):
                if include_factories and name in factories:
                    call = re.compile(r"\b" + re.escape(name) + r"\s*\(")
                elif receivers:
                    call = re.compile(r"\b(?:" + "|".join(map(re.escape, sorted(receivers)))
                                      + r")\." + re.escape(name) + r"\s*\(")
                else:
                    continue
                for line_number, line in enumerate(lines, 1):
                    match = call.search(line)
                    if match is None:
                        continue
                    before = line[:match.start()]
                    after = line[match.end():] + "\n" + "\n".join(lines[line_number:line_number + 2])
                    handled = bool(re.search(r"\bawait\b", before)
                                   or ("Promise.resolve(" in before and ".then(" in after)
                                   or re.search(r"\)\s*\.(?:then|catch)\s*\(", after)
                                   or re.search(r"\.then\s*\(.*=>\s*$", before))
                    if not handled:
                        unhandled.append(f"{name}@{line_number}")
                        excerpts.append(f"{line_number}: {line.strip()[:240]}")
            if unhandled:
                message = (f"CV-04: {caller_relative.as_posix()} calls async PostgreSQL methods without "
                           f"awaiting or handling promises: {', '.join(unhandled[:8])}")
                if include_factories:
                    message += (". Update each call and its enclosing callback/startup path, then read back "
                                "the file. Remaining source: " + " | ".join(excerpts[:8]))
                raise ValueError(message)


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
            or plan.get("target") not in {"aws-ecs-express", "cloud-run"}
            or not isinstance(database, dict)
            or database.get("binding") not in {"create", "existing"}
            or postgres_request is None
            or database.get("database_id") != postgres_request.database_id
            or not isinstance(resources, list)
            or "one-off SQL migration task" not in resources
            or ("existing Cloud SQL PostgreSQL" if plan.get('target') == 'cloud-run' else
                "new RDS PostgreSQL" if database.get("binding") == "create" else "existing RDS PostgreSQL")
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
    return {"id": "CV-04", "status": "pass", "source": "reviewed_sqlite_migration",
            "integrity": {"protocol": "sqlite-snapshot-multiset-v1",
                          "source_revision": source_digest(original),
                          "prepared_revision": source_digest(migrations.directory.parent),
                          "snapshot_sha256": snapshot.source_sha256,
                          "sql_sha256": expected_sql_hash,
                          "bundle_digest": migrations.digest,
                          "schema_sha256": hashlib.sha256(json.dumps(
                              snapshot.schema, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                          "row_counts": snapshot.row_counts}}


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
    if target == "onprem-vm":
        return parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
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
    if compilation.get("schema_version") == 2:
        expected = lower_target_configuration(infrastructure_plan, compilation.get("deployment_ir"))
        if target_plan.get("execution_configuration") != expected:
            raise ValueError("CV-09: Compiled execution configuration disagrees with the target")
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
        if plan.get('target') == 'cloud-run':
            if database['binding'] != 'existing':
                raise ValueError('CV-03: Cloud SQL creation is not implemented')
            resource = 'existing Cloud SQL PostgreSQL'
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
