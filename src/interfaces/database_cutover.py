"""Operator registrations and owned asynchronous cutover intake, without AWS access."""

import hashlib
import json
import os
import re
from dataclasses import asdict

from adapters.aws.job_queue import SqsOperationQueue
from application.database_cutover_workflow import KIND, execution_registration
from domain.database import DatabaseBinding


def load_cutover_api_configuration(*, policies, account_id, region, now):
    enabled = os.environ.get("SKY_DATABASE_CUTOVER_ENABLED", "false")
    raw = os.environ.get("SKY_DATABASE_CUTOVER_REGISTRATIONS_JSON")
    if enabled not in {"true", "false"}:
        raise ValueError("Explicit cutover enable flag required")
    if enabled == "false":
        if raw is not None:
            raise ValueError("Cutover registrations require explicit activation")
        return None
    if not policies or raw is None:
        raise ValueError("Registered cutover policies and controllers required")
    url = os.environ.get("SKY_DATABASE_CUTOVER_QUEUE_URL", "")
    if url in {
        os.environ.get(name, "")
        for name in ("SKY_JOB_QUEUE_URL", "SKY_SHARED_DATABASE_QUEUE_URL", "SKY_DEDICATED_DATABASE_QUEUE_URL")
    }:
        raise ValueError("Cutover queue must be separate")
    SqsOperationQueue(url, region=region, account_id=account_id, client=object())
    return load_cutover_registrations(raw, policies=policies, now=now)


def load_cutover_registrations(raw, *, policies, now):
    if not isinstance(raw, str) or len(raw.encode()) > 256 * 1024:
        raise ValueError("Bounded operator cutover registration required")

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate cutover registration field")
            value[key] = item
        return value

    document = json.loads(raw, object_pairs_hook=unique)
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "registrations"}
        or type(document["schema_version"]) is not int
        or document["schema_version"] != 1
        or not isinstance(document["registrations"], dict)
        or not 1 <= len(document["registrations"]) <= 50
    ):
        raise ValueError("Invalid operator cutover registrations")
    for job_id, registration in document["registrations"].items():
        if not re.fullmatch(r"[a-f0-9]{16}", job_id) or job_id not in policies:
            raise ValueError("Cutover job requires a registered operating policy")
        if not isinstance(registration, dict):
            raise TypeError("Invalid cutover registration")
        plan = {key: registration.get(key) for key in ("plan_id", "source", "target")}
        execution_registration(registration, plan, now)
        if registration["target"] != asdict(policies[job_id].target):
            raise ValueError("Cutover target differs from operating policy")
        source, target = (DatabaseBinding(**registration[key]) for key in ("source", "target"))
        if (
            source.role != "shared_workload"
            or target.role != "dedicated_workload"
            or source.instance_id == target.instance_id
            or (source.account_id, source.region, source.organization_id, source.application_id)
            != (target.account_id, target.region, target.organization_id, target.application_id)
        ):
            raise ValueError("Same application shared-to-dedicated registration required")
    return document["registrations"]


class DatabaseCutoverController:
    def __init__(self, reviews, admission):
        self.reviews, self.admission = reviews, admission

    def detail(self, principal, job_id):
        review = self.reviews.detail(principal, job_id)
        registration = self.admission.registrations.get(job_id)
        available = False
        try:
            proposal = review["proposal"]
            if self.admission.enabled is True and isinstance(proposal, dict):
                execution_registration(registration, proposal["plan"], self.admission.clock())
                available = proposal["status"] == "approved"
        except (ValueError, TypeError, KeyError):
            pass
        return {
            "job_id": job_id,
            "execution_enabled": available,
            "requires_revalidation": True,
            "operation_kind": KIND,
            "execution_mode": "durable_worker",
            "automatic_execution": False,
        }

    def submit(self, principal, job_id, *, proposal_id, request_key):
        # Check write permission before disclosing registration/operation state.
        self.reviews._policy(principal, job_id)
        if not isinstance(request_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_key):
            raise ValueError("Bounded idempotency request key required")
        scoped = hashlib.sha256(
            json.dumps(
                [principal.organization_id, principal.user_id, job_id, request_key], separators=(",", ":")
            ).encode()
        ).hexdigest()
        operation = self.admission.admit(principal, job_id, proposal_id, "cutover-" + scoped)
        return self._receipt(operation, job_id)

    def status(self, principal, job_id, operation_id):
        self.reviews.detail(principal, job_id)
        record = self.reviews.records.load_job(job_id).record
        operation = self.admission.operations.get(operation_id)
        approval = operation.command.get("approval", {})
        if (
            operation.kind != KIND
            or operation.application_id != record.get("application_id")
            or approval.get("job_id") != job_id
            or approval.get("requested_by", {}).get("organization_id") != principal.organization_id
        ):
            raise FileNotFoundError("Cutover operation not found")
        return self._receipt(operation, job_id)

    @staticmethod
    def _receipt(operation, job_id):
        return {
            "job_id": job_id,
            "operation_id": operation.id,
            "status": operation.status,
            "requires_attention": operation.status == "needs_attention",
            "status_url": f"/api/jobs/{job_id}/database-cutover/{operation.id}",
        }
