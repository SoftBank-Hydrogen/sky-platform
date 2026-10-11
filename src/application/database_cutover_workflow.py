"""Explicit approved cutover admission/execution for trusted registrations.

No public route or default hosted runtime enables this workflow. Registrations
and flow factories are operator-owned, never taken from an uploaded application
or a queue message. Preparation and cutover have separate operation kinds.
"""

import hashlib
import json
import re
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
from datetime import datetime

from application.database_cutover import OperationCutoverJournal
from application.database_promotion_workflow import PromotionPreparationWorker, approved_snapshot
from application.deployment_writes import DeploymentMetadataWriter
from ports.state import RecordConflict

KIND = "db_dedicated_cutover"
_EVIDENCE = {"target_ownership_tls", "schema_copy", "all_writers_controlled", "control_auth_tls"}
_BASELINE = {
    "plan_id",
    "source",
    "target",
    "schema_sha256",
    "tables",
    "verified_scope",
    "source_retained",
    "routing_changed",
    "ready_to_cutover",
    "requires_final_write_freeze",
}


def execution_registration(registration, plan, now):
    """Pin bounded, credential-free evidence before creating any external intent."""
    if (
        not isinstance(registration, dict)
        or set(registration)
        != {
            "plan_id",
            "source",
            "target",
            "baseline",
            "controller_id",
            "controller_revision",
            "verified_until",
            "evidence",
        }
        or any(registration[key] != plan[key] for key in ("plan_id", "source", "target"))
        or not isinstance(registration["controller_id"], str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", registration["controller_id"])
        or not isinstance(registration["controller_revision"], str)
        or not re.fullmatch(r"[a-f0-9]{64}", registration["controller_revision"])
        or not isinstance(registration["evidence"], dict)
        or set(registration["evidence"]) != _EVIDENCE
        or any(
            not isinstance(ref, str) or not re.fullmatch(r"evidence:[A-Za-z0-9/_-]{1,200}", ref)
            for ref in registration["evidence"].values()
        )
    ):
        raise ValueError("Verified cutover registration required")
    expires, current = datetime.fromisoformat(registration["verified_until"]), datetime.fromisoformat(now)
    if expires.tzinfo is None or current.tzinfo is None or expires <= current:
        raise ValueError("Cutover registration expired")
    baseline = registration["baseline"]
    if (
        not isinstance(baseline, dict)
        or set(baseline) != _BASELINE
        or any(baseline[key] != plan[key] for key in ("plan_id", "source", "target"))
        or baseline["verified_scope"] != "locked_table_snapshot_copy"
        or baseline["source_retained"] is not True
        or baseline["routing_changed"] is not False
        or baseline["ready_to_cutover"] is not False
        or baseline["requires_final_write_freeze"] is not True
        or not isinstance(baseline["schema_sha256"], str)
        or not re.fullmatch(r"[a-f0-9]{64}", baseline["schema_sha256"])
        or not isinstance(baseline["tables"], list)
        or not 1 <= len(baseline["tables"]) <= 100
    ):
        raise ValueError("Verified initial copy baseline required")
    names = set()
    for table in baseline["tables"]:
        if (
            not isinstance(table, dict)
            or set(table) != {"table", "rows", "sha256"}
            or not isinstance(table["table"], str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", table["table"])
            or table["table"] in names
            or type(table["rows"]) is not int
            or not 0 <= table["rows"] <= 100000
            or not isinstance(table["sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", table["sha256"])
        ):
            raise ValueError("Invalid copy table evidence")
        names.add(table["table"])
    encoded = json.dumps(registration, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > 65536:
        raise ValueError("Cutover registration too large")
    return hashlib.sha256(encoded).hexdigest()


class PromotionCutoverAdmission:
    def __init__(self, records, operations, policies, registrations, *, clock, enabled=False):
        self.records, self.operations = records, operations
        self.policies, self.registrations = policies, registrations
        self.clock, self.enabled = clock, enabled

    def admit(self, principal, job_id, proposal_id, request_key):
        if self.enabled is not True:
            raise ValueError("Cutover executor and transport are not registered")
        now = self.clock()
        snapshot, proposal = approved_snapshot(
            self.records, principal, job_id, proposal_id, self.policies.get(job_id), now
        )
        digest = execution_registration(self.registrations.get(job_id), proposal["plan"], now)
        approval = {
            "schema_version": 1,
            "job_id": job_id,
            "job_revision": snapshot.revision,
            "requested_by": {"user_id": principal.user_id, "organization_id": principal.organization_id},
            "proposal": proposal,
        }
        return self.operations.admit(
            snapshot.record["application_id"],
            KIND,
            request_key,
            {"schema_version": 1, "approval": approval, "registration_digest": digest},
        )


class PromotionCutoverWorker:
    def __init__(
        self,
        records,
        operations,
        policies,
        registrations,
        flow_factory,
        resolve_principal,
        *,
        owner,
        clock,
        ownership=None,
    ):
        self.records, self.operations, self.registrations = records, operations, registrations
        self.flow_factory, self.owner, self.clock, self.ownership = flow_factory, owner, clock, ownership
        self.validator = PromotionPreparationWorker(
            records, operations, policies, None, resolve_principal, owner=owner, clock=clock
        )

    def _validate(self, operation):
        command = operation.command
        if (
            not isinstance(command, dict)
            or set(command) != {"schema_version", "approval", "registration_digest"}
            or type(command["schema_version"]) is not int
            or command["schema_version"] != 1
        ):
            raise ValueError("Invalid cutover command")
        plan = self.validator._validate(replace(operation, command=command["approval"]))
        job_id = command["approval"]["job_id"]
        registration = deepcopy(self.registrations.get(job_id))
        if execution_registration(registration, plan, self.clock()) != command["registration_digest"]:
            raise ValueError("Cutover controller or evidence changed")
        return plan, registration

    def execute(self, operation_id, attempt_id):
        operation = self.operations.get(operation_id)
        if operation.kind != KIND:
            raise ValueError("Wrong cutover worker kind")
        if operation.attempt_id != attempt_id:
            return {"status": "stale_attempt"}
        if operation.status in {"succeeded", "failed", "needs_attention"}:
            return {"status": operation.status}
        lease = self.operations.claim(operation_id, attempt_id, self.owner, seconds=900)
        if lease is None:
            return {"status": "not_claimed"}
        journal = OperationCutoverJournal(self.operations, lease)
        context = self.ownership(lease) if self.ownership else nullcontext(lambda: None)
        with context as ownership_guard:
            if operation.external_pending or operation.external_intent is not None:
                journal.hold()
                return {"status": "needs_attention"}
            try:
                ownership_guard()
                plan, registration = self._validate(operation)
            except (ValueError, TypeError, KeyError, PermissionError, FileNotFoundError):
                saved = self.operations.complete(
                    lease, {"code": "cutover_revalidation_failed"}, succeeded=False
                )
                return {"status": "failed" if saved else "lease_lost"}
            except OSError:
                self.operations.interrupt(lease, {"stage": "cutover_source_unavailable"})
                return {"status": "interrupted"}
            approval = operation.command["approval"]
            actor = approval["requested_by"]
            principal = self.validator.resolve_principal(actor["user_id"], actor["organization_id"])
            revision = approval["job_revision"]
            metadata_written = False

            def guard():
                ownership_guard()
                journal.guard()
                current = self.validator.resolve_principal(actor["user_id"], actor["organization_id"])
                snapshot = DeploymentMetadataWriter(self.records).snapshot(current, approval["job_id"])
                if snapshot.revision != revision:
                    raise RecordConflict("Deployment changed during cutover")
                if not metadata_written:
                    self._validate(operation)

            try:
                guard()
                # Factory construction must be pure. All effectful adapter calls
                # occur only after the durable external intent below.
                flow = self.flow_factory(plan, registration, guard=guard, journal=journal)
                journal.begin(plan)
                receipt = flow.promote(plan, registration["baseline"])
                if (
                    not isinstance(receipt, dict)
                    or set(receipt) != {"plan_id", "binding", "baseline", "single_writer", "source_retained"}
                    or receipt["plan_id"] != plan["plan_id"]
                    or receipt["binding"] != plan["target"]
                    or receipt["single_writer"] is not True
                    or receipt["source_retained"] is not True
                ):
                    raise ValueError("Invalid cutover result")
                # Validate the final baseline's shape too; retain its real scope.
                checked = deepcopy(registration)
                checked["baseline"] = {**receipt["baseline"], "verified_scope": "locked_table_snapshot_copy"}
                if receipt["baseline"].get("verified_scope") != "final_locked_refresh":
                    raise ValueError("Final synchronization evidence required")
                execution_registration(checked, plan, self.clock())
                guard()
                snapshot = DeploymentMetadataWriter(self.records).snapshot(principal, approval["job_id"])
                projection = {
                    "operation_id": operation_id,
                    "registration_digest": operation.command["registration_digest"],
                    "controller_id": registration["controller_id"],
                    **receipt,
                }
                saved = DeploymentMetadataWriter(self.records).patch(
                    principal,
                    approval["job_id"],
                    revision,
                    {
                        "workload_database_binding": plan["target"],
                        "database_promotion": projection,
                        "result": {**snapshot.record["result"], "database_promotion": projection},
                    },
                )
                revision, metadata_written = saved.revision, True
                # Metadata and operation are separate transactions. Failure here
                # retains the scope for reconciliation, never reruns app cutover.
                journal.finish(projection)
                return {"status": "succeeded"}
            except Exception:  # noqa: BLE001 -- uncertain writes require durable reconciliation
                journal.hold()
                return {"status": "needs_attention"}
