"""Durable, opt-in shared allocation before deployment; no traffic cutover.

Queue messages supply IDs only. Commands come from the operation store and are
revalidated against the current job, pool registration and current membership.
"""

import re
from dataclasses import asdict

from application.deployment_writes import DeploymentMetadataWriter
from application.shared_database import (
    ManagedSharedDatabaseService,
    authorize_allocation,
)
from domain.access import Principal
from domain.shared_database import PoolAllocationRequest, SharedDatabasePool
from engine.database_placement import select_shared_database
from ports.operations import OperationStore
from ports.shared_database import ManagedSharedDatabaseAllocator
from ports.state import RecordConflict, VersionedDeploymentRecordStore

KIND = "db_shared_allocate"


class SharedDatabaseAdmission:
    def __init__(
        self,
        records: VersionedDeploymentRecordStore,
        operations: OperationStore,
        pool: SharedDatabasePool,
        config_digest: str,
    ):
        self.writer = DeploymentMetadataWriter(records)
        self.operations, self.pool, self.config_digest = operations, pool, config_digest

    def choose(self, principal: Principal, job_id: str, *, connection_limit=5) -> dict:
        snapshot = self.writer.snapshot(principal, job_id)
        return select_shared_database(
            snapshot.record, self.pool, config_digest=self.config_digest, connection_limit=connection_limit
        )

    def admit(self, principal: Principal, job_id: str, selection: dict, request_key: str):
        snapshot = self.writer.snapshot(principal, job_id)
        if not isinstance(selection, dict) or not isinstance(selection.get("allocation"), dict):
            raise ValueError("Reviewed database choice is required")
        expected = select_shared_database(
            snapshot.record,
            self.pool,
            config_digest=self.config_digest,
            connection_limit=selection["allocation"].get("connection_limit"),
        )
        if selection != expected:
            raise ValueError("Database choice changed; review it again")
        command = {
            "schema_version": 1,
            "job_id": job_id,
            "job_revision": snapshot.revision,
            "requested_by": {"user_id": principal.user_id, "organization_id": principal.organization_id},
            "selection": expected,
        }
        # The existing app reservation prevents concurrent deployment/retirement.
        return self.operations.admit(snapshot.record["application_id"], KIND, request_key, command)


class SharedDatabaseWorker:
    def __init__(
        self,
        records: VersionedDeploymentRecordStore,
        operations: OperationStore,
        pool: SharedDatabasePool,
        config_digest: str,
        allocator: ManagedSharedDatabaseAllocator,
        resolve_principal,
        *,
        owner: str,
    ):
        self.writer = DeploymentMetadataWriter(records)
        self.operations, self.pool, self.config_digest = operations, pool, config_digest
        self.service = ManagedSharedDatabaseService(allocator)
        self.resolve_principal, self.owner = resolve_principal, owner

    def _validate(self, operation):
        command = operation.command
        if (
            not isinstance(command, dict)
            or set(command) != {"schema_version", "job_id", "job_revision", "requested_by", "selection"}
            or type(command["schema_version"]) is not int
            or command["schema_version"] != 1
        ):
            raise ValueError("Invalid shared allocation command")
        actor = command["requested_by"]
        if not isinstance(actor, dict) or set(actor) != {"user_id", "organization_id"}:
            raise ValueError("Invalid allocation requester")
        principal = self.resolve_principal(actor["user_id"], actor["organization_id"])
        if (
            not isinstance(principal, Principal)
            or principal.user_id != actor["user_id"]
            or principal.organization_id != actor["organization_id"]
        ):
            raise PermissionError("Allocation requester no longer has access")
        snapshot = self.writer.snapshot(principal, command["job_id"])
        if type(command["job_revision"]) is not int or snapshot.revision != command["job_revision"]:
            raise RecordConflict("Deployment changed before database allocation")
        selection = command["selection"]
        if not isinstance(selection, dict) or not isinstance(selection.get("allocation"), dict):
            raise ValueError("Invalid allocation selection")
        expected = select_shared_database(
            snapshot.record,
            self.pool,
            config_digest=self.config_digest,
            connection_limit=selection["allocation"].get("connection_limit"),
        )
        if selection != expected or operation.application_id != snapshot.record["application_id"]:
            raise ValueError("Allocation command differs from the source or registered pool")
        request = PoolAllocationRequest(
            self.pool,
            principal.organization_id,
            operation.application_id,
            selection["allocation"]["connection_limit"],
        )
        authorize_allocation(principal, snapshot.record, request)
        return principal, snapshot.record, request, expected

    def execute(self, operation_id: str, attempt_id: str) -> dict:
        operation = self.operations.get(operation_id)
        if operation.kind != KIND:
            raise ValueError("This worker only accepts shared database allocations")
        if operation.attempt_id != attempt_id:
            return {"status": "stale_attempt", "operation_id": operation_id}
        if operation.status in {"succeeded", "failed", "needs_attention"}:
            return {"status": operation.status, "operation_id": operation_id}
        lease = self.operations.claim(operation_id, attempt_id, self.owner, seconds=900)
        if lease is None:
            return {"status": "not_claimed", "operation_id": operation_id}
        try:
            principal, job, request, selection = self._validate(operation)
        except (ValueError, PermissionError, FileNotFoundError, RecordConflict):
            completed = self.operations.complete(
                lease, {"code": "allocation_revalidation_failed", "deployment_ready": False}, succeeded=False
            )
            return {"status": "failed" if completed else "lease_lost", "operation_id": operation_id}
        except OSError:
            self.operations.interrupt(lease, {"stage": "source_read_unavailable"})
            return {"status": "interrupted", "operation_id": operation_id}
        intent = {
            "kind": KIND,
            "allocation_id": request.id,
            "selection_id": selection["selection_id"],
            "config_digest": self.config_digest,
        }
        if operation.external_receipt is not None:
            # Resume after a recorded observation without asking AWS to allocate again.
            if operation.external_intent != intent or operation.checkpoint != {"stage": "database_allocated"}:
                raise ValueError("Recorded external evidence requires reconciliation")
            safe = safe_receipt(operation.external_receipt, request)
        else:
            if not self.operations.begin_external(lease, intent):
                return {"status": "lease_lost", "operation_id": operation_id}
            try:
                receipt = self.service.allocate(principal, job, request)
                safe = safe_receipt(receipt, request, require_ready=True)
            except Exception:
                # The adapter may have created a secret/DB. Preserve the intent and reservation.
                self.operations.interrupt(lease, {"stage": "allocation_outcome_uncertain"})
                return {"status": "needs_attention", "operation_id": operation_id}
            # Never retry an ambiguous state commit. Recovery reads the durable receipt.
            if not self.operations.observe_external(lease, safe, {"stage": "database_allocated"}):
                return {"status": "lease_lost", "operation_id": operation_id}
        result = {
            "database_allocation": safe,
            "selection_id": selection["selection_id"],
            "deployment_ready": False,
            "deployment_status": "not_started",
            "remaining_gates": [
                "schema_migration",
                "app_secret_access",
                "application_runtime",
                "http_verification",
            ],
        }
        completed = self.operations.complete(lease, result)
        return {"status": "succeeded" if completed else "lease_lost", "operation_id": operation_id}


def safe_receipt(receipt, request, *, require_ready=False):
    expected_binding = asdict(request.binding())
    prefix = f"arn:aws:secretsmanager:{request.pool.region}:{request.pool.account_id}:secret:sky-pool/{request.pool.id}/{request.id}-"
    if (
        not isinstance(receipt, dict)
        or receipt.get("allocation_id") != request.id
        or receipt.get("binding") != expected_binding
        or receipt.get("verified_scope") != "postgresql_role_and_database_acl"
        or not isinstance(receipt.get("secret_ref"), str)
        or not re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9]{6}", receipt["secret_ref"])
        or (require_ready and receipt.get("status") != "ready")
    ):
        raise ValueError("Allocation receipt does not match the reviewed request")
    return {
        "allocation_id": request.id,
        "binding": expected_binding,
        "secret_ref": receipt["secret_ref"],
        "verified_scope": receipt["verified_scope"],
    }
