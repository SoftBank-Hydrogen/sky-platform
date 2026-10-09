"""Record the source actually handed to an adapter after agent edits."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from application.deployment_core import DeploymentPlan, source_digest


def executable_plan_digest(plan: DeploymentPlan) -> str:
    return hashlib.sha256(json.dumps(plan.__dict__, sort_keys=True).encode()).hexdigest()


def resolved_target_plan(target_plan: dict, plan: DeploymentPlan) -> dict:
    """Resolve the container HTTP endpoint only after the executable plan exists."""
    config = target_plan.get("execution_configuration")
    if (
        not isinstance(config, dict)
        or config.get("port_source") != "executable_deployment_plan"
        or config.get("service") != "source-bundle"
        or not isinstance(target_plan.get("id"), str)
        or target_plan.get("target") != plan.target
        or type(plan.port) is not int
        or not 1024 <= plan.port <= 65535
        or not isinstance(plan.health_path, str)
        or len(plan.health_path) > 200
        or not re.fullmatch(r"/[A-Za-z0-9/_.-]*", plan.health_path)
        or "//" in plan.health_path
        or ".." in plan.health_path
    ):
        raise ValueError("CV-06: Executable HTTP endpoint disagrees with the compiled target")
    return {
        "target_plan_id": target_plan["id"],
        "service": config["service"],
        "container_protocol": "http",
        "container_port": plan.port,
        "health_path": plan.health_path,
    }


def _files(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Source transformation cannot contain symbolic links")
        if path.is_file():
            with path.open("rb") as source:
                result[path.relative_to(root).as_posix()] = hashlib.file_digest(source, "sha256").hexdigest()
    return result


def source_transform_record(
    compilation: dict, original: Path, work: Path, plan: DeploymentPlan, *, legacy: bool = False
) -> dict:
    """Bind applied file changes and the executable plan to the compiled target."""
    source_revision = compilation["source_revision"]
    if source_digest(original) != source_revision:
        raise ValueError("Compiled source revision no longer matches the uploaded source")
    work_revision = source_digest(work)
    if work_revision != plan.source_digest:
        raise ValueError("Executable plan does not match the transformed source")
    if plan.target != compilation["target_plan"]["target"]:
        raise ValueError("Executable plan target differs from compiled target")
    if plan.dockerfile_source == "generated" and (
        f"EXPOSE {plan.port}\n" not in plan.dockerfile
        or not re.search(rf"^ENV\s+[^\n]*\bPORT={plan.port}(?:\s|$)", plan.dockerfile, re.MULTILINE)
    ):
        raise ValueError("Generated image port differs from executable plan")
    before = _files(original)
    after = _files(work)
    changes = [
        {"path": path, "before_sha256": before.get(path), "after_sha256": after.get(path)}
        for path in sorted(before.keys() | after.keys())
        if before.get(path) != after.get(path)
    ]
    record = {
        "compilation_id": compilation["compilation_id"],
        "decision_revision": compilation["decision_revision"],
        "source_revision": source_revision,
        "transformed_source_revision": work_revision,
        "target_plan_id": compilation["target_plan"]["id"],
        "executable_plan_digest": executable_plan_digest(plan),
        "changes": changes,
    }
    if compilation.get("schema_version") == 2 and not legacy:
        record["schema_version"] = 2
        record["resolved_target"] = resolved_target_plan(compilation["target_plan"], plan)
    return record


def verify_source_transform(
    record: dict, compilation: dict, original: Path, work: Path, plan: DeploymentPlan
) -> None:
    legacy = isinstance(record, dict) and "schema_version" not in record
    expected = source_transform_record(compilation, original, work, plan, legacy=legacy)
    if not isinstance(record, dict) or record != expected:
        raise ValueError("Stored source transformation does not match the executable plan")
