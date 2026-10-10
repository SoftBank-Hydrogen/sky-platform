"""Explicit allocation intent; never infer shared RDS from price or availability."""

import hashlib
import json
import re
from dataclasses import asdict

from domain.shared_database import PoolAllocationRequest, SharedDatabasePool


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def select_shared_database(
    job: dict, pool: SharedDatabasePool, *, config_digest: str, connection_limit=5
) -> dict:
    if job.get("target") != "aws-ecs-express" or job.get("status") not in {"planned", "queued", "failed"}:
        raise ValueError("Shared allocation requires an inactive AWS container deployment")
    if any(
        job.get(key) is not None
        for key in ("postgres", "cloud_sql", "sqlite_conversion", "prior_result", "result")
    ):
        raise ValueError("Existing bindings or data require a separate migration plan")
    profile = job.get("infrastructure_profile")
    ir = job.get("application_ir")
    aws = job.get("aws")
    if (
        not isinstance(profile, dict)
        or profile.get("database_engines") != ["postgresql"]
        or not profile.get("evidence")
        or not isinstance(ir, dict)
        or not isinstance(aws, dict)
        or aws.get("expected_account") != pool.account_id
        or aws.get("region") != pool.region
    ):
        raise ValueError("A source-analyzed PostgreSQL workload in the registered account/region is required")
    revision, source_digest = ir.get("source_revision"), job.get("source_digest")
    if any(
        not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
        for value in (revision, source_digest, config_digest)
    ):
        raise ValueError("Database choice requires source and registered configuration digests")
    if not isinstance(job.get("id"), str) or not re.fullmatch(r"[a-f0-9]{16}", job["id"]):
        raise ValueError("Invalid deployment identity")
    request = PoolAllocationRequest(
        pool, job.get("organization_id"), job.get("application_id"), connection_limit
    )
    result = {
        "schema_version": 1,
        "mode": "shared_workload",
        "selection_basis": "explicit_user_choice",
        "job_id": job["id"],
        "source_revision": revision,
        "source_digest": source_digest,
        "profile_digest": _digest(profile),
        "config_digest": config_digest,
        "allocation": asdict(request),
        "allocation_id": request.id,
        "status": "requires_allocation",
        "deployment_ready": False,
        "migration_status": "not_started",
    }
    return {**result, "selection_id": "DBS-" + _digest(result)[:24]}
