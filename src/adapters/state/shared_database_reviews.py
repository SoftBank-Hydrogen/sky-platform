"""Atomic consumption of a source-bound DB review plus operation/outbox admission.

No workload DB or Secrets Manager calls here. Runtime never runs migrations.
The worker revalidates current membership and source again before allocation.
"""

import hashlib
import json
import re
from uuid import uuid4

from application.deployment_writes import authorize_job_snapshot
from application.shared_database_workflow import KIND, allocation_command, safe_receipt
from domain.access import Action, Principal, ResourceOwner, Role, permitted
from domain.shared_database import PoolAllocationRequest
from engine.database_placement import select_shared_database
from ports.operations import IdempotencyConflict
from ports.shared_database_reviews import (
    AdmittedDatabaseAllocation,
    DatabaseReviewUnavailable,
    SharedDatabaseReview,
)
from ports.state import StoredJob


class PostgresSharedDatabaseReviews:
    def __init__(self, operations, pool, config_digest):
        if not re.fullmatch(r"[a-f0-9]{64}", config_digest):
            raise ValueError("Registered pool digest required")
        self.operations, self.pool, self.config_digest = operations, pool, config_digest

    @staticmethod
    def initialize_schema(operations):
        """Explicit additive maintenance migration, serialized with other ledgers."""
        operations.initialize()
        with operations.records._connection() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (operations.records.MIGRATION_LOCK,))
            connection.execute("""CREATE TABLE IF NOT EXISTS sky_state.shared_database_review_schema_versions (
                version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())""")
            versions = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT version FROM sky_state.shared_database_review_schema_versions ORDER BY version"
                ).fetchall()
            )
            if versions not in ((), (1,)):
                raise ValueError("Unsupported shared database review schema version")
            if not versions:
                connection.execute("""CREATE TABLE sky_state.shared_database_reviews (
                    workspace text NOT NULL, id uuid NOT NULL,
                    organization_id text NOT NULL, reviewed_by text NOT NULL,
                    job_id text NOT NULL, application_id text NOT NULL,
                    command jsonb NOT NULL CHECK(jsonb_typeof(command)='object'),
                    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
                    expires_at timestamptz NOT NULL, revoked_at timestamptz,
                    consumed_key text, operation_id uuid,
                    PRIMARY KEY(workspace,id),
                    FOREIGN KEY(workspace,application_id,operation_id)
                        REFERENCES sky_state.operations(workspace,application_id,id),
                    CHECK(expires_at>created_at),
                    CHECK((consumed_key IS NULL)=(operation_id IS NULL)),
                    CHECK(revoked_at IS NULL OR operation_id IS NULL))""")
                connection.execute(
                    "INSERT INTO sky_state.shared_database_review_schema_versions(version) VALUES(1)"
                )

    def check_ready(self):
        from adapters.state.readiness import check_database_ready

        check_database_ready(self.operations.records.connection_factory, operations=True)
        with self.operations.records._connection() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            connection.execute("SET LOCAL statement_timeout=3000")
            versions = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT version FROM sky_state.shared_database_review_schema_versions ORDER BY version"
                ).fetchall()
            )
            if versions != (1,):
                raise ValueError("Shared database reviews must be migrated separately")
            connection.execute("""SELECT workspace,id,organization_id,reviewed_by,job_id,application_id,
                command,expires_at,revoked_at,consumed_key,operation_id
                FROM sky_state.shared_database_reviews LIMIT 0""")

    @staticmethod
    def _authorize(principal):
        if not isinstance(principal, Principal) or not permitted(
            principal, Action.DEPLOY, ResourceOwner(principal.organization_id, principal.user_id)
        ):
            raise PermissionError("Shared database review access denied")

    def _job(self, connection, principal, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{16}", job_id):
            raise ValueError("Invalid job identity")
        row = connection.execute(
            """SELECT document,modified_at,revision FROM sky_state.metadata_records
            WHERE workspace=%s AND kind='job' AND record_id=%s FOR UPDATE""",
            (self.operations.workspace, job_id),
        ).fetchone()
        if row is None:
            raise FileNotFoundError("Deployment not found")
        return authorize_job_snapshot(principal, job_id, StoredJob(row[0], row[1].isoformat(), row[2]))

    def review(self, principal, job_id, *, connection_limit=5, seconds=900):
        self._authorize(principal)
        seconds = self.operations._bounded(seconds, 1, 900)
        with self.operations.records._connection() as connection:
            snapshot = self._job(connection, principal, job_id)
            selection = select_shared_database(
                snapshot.record,
                self.pool,
                config_digest=self.config_digest,
                connection_limit=connection_limit,
            )
            command = self.operations._document(
                allocation_command(principal, job_id, snapshot, selection, self.pool, self.config_digest)
            )
            identity = str(uuid4())
            expires = connection.execute(
                """INSERT INTO sky_state.shared_database_reviews
                (workspace,id,organization_id,reviewed_by,job_id,application_id,command,expires_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,clock_timestamp()+(%s*interval '1 second'))
                RETURNING expires_at""",
                (
                    self.operations.workspace,
                    identity,
                    principal.organization_id,
                    principal.user_id,
                    job_id,
                    snapshot.record["application_id"],
                    self.operations._json(command),
                    seconds,
                ),
            ).fetchone()[0]
            return SharedDatabaseReview(identity, expires, snapshot.revision, selection)

    def _review(self, connection, principal, review_id, *, revoking=False):
        review_id = self.operations._uuid(review_id)
        row = connection.execute(
            """SELECT organization_id,reviewed_by,job_id,application_id,command,
            expires_at,revoked_at,consumed_key,operation_id,expires_at>clock_timestamp()
            FROM sky_state.shared_database_reviews WHERE workspace=%s AND id=%s FOR UPDATE""",
            (self.operations.workspace, review_id),
        ).fetchone()
        if (
            row is None
            or row[0] != principal.organization_id
            or (row[1] != principal.user_id and not (revoking and principal.role is Role.ADMIN))
        ):
            raise FileNotFoundError("Database review not found")
        return row

    def submit(self, principal, review_id, request_key):
        self._authorize(principal)
        review_id = self.operations._uuid(review_id)
        request_key = self.operations._text(request_key, "request key")
        key = hashlib.sha256(
            json.dumps(
                [
                    self.operations.workspace,
                    principal.organization_id,
                    principal.user_id,
                    review_id,
                    request_key,
                ],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        with self.operations.records._connection() as connection:
            row = self._review(connection, principal, review_id)
            snapshot = self._job(connection, principal, row[2])
            if row[8] is not None:
                if row[7] != key:
                    raise IdempotencyConflict("Review was consumed by a different request")
                operation = self.operations._select(connection, str(row[8]))
                return AdmittedDatabaseAllocation(row[2], operation.id, operation.attempt_id)
            if row[6] is not None or not row[9]:
                raise DatabaseReviewUnavailable("Database review expired or was revoked")
            command = allocation_command(
                principal, row[2], snapshot, row[4]["selection"], self.pool, self.config_digest
            )
            if command != row[4] or snapshot.record["application_id"] != row[3]:
                raise DatabaseReviewUnavailable("Reviewed source or configuration changed")
            operation = self.operations._admit_in_transaction(
                connection, row[3], KIND, "shared-db:" + key, command
            )
            consumed = connection.execute(
                """UPDATE sky_state.shared_database_reviews
                SET consumed_key=%s,operation_id=%s WHERE workspace=%s AND id=%s
                AND operation_id IS NULL AND revoked_at IS NULL AND expires_at>clock_timestamp()
                RETURNING id""",
                (key, operation.id, self.operations.workspace, review_id),
            ).fetchone()
            if consumed is None:
                raise DatabaseReviewUnavailable("Database review expired before consumption")
            return AdmittedDatabaseAllocation(row[2], operation.id, operation.attempt_id)

    def revoke(self, principal, review_id):
        self._authorize(principal)
        review_id = self.operations._uuid(review_id)
        with self.operations.records._connection() as connection:
            row = self._review(connection, principal, review_id, revoking=True)
            self._job(connection, principal, row[2])
            if row[8] is not None:
                raise DatabaseReviewUnavailable(
                    "An admitted allocation requires reconciliation, not revocation"
                )
            if row[6] is not None:
                return False
            connection.execute(
                """UPDATE sky_state.shared_database_reviews SET revoked_at=clock_timestamp()
                WHERE workspace=%s AND id=%s""",
                (self.operations.workspace, review_id),
            )
            return True

    def detail(self, principal, review_id):
        self._authorize(principal)
        with self.operations.records._connection() as connection:
            row = self._review(connection, principal, review_id)
            self._job(connection, principal, row[2])
            result = {
                "id": review_id,
                "job_id": row[2],
                "expires_at": row[5].isoformat(),
                "selection": row[4]["selection"],
                "deployment_ready": False,
                "remaining_gates": [
                    "schema_migration",
                    "app_secret_access",
                    "application_runtime",
                    "http_verification",
                ],
                "status": "revoked" if row[6] else "reviewed" if row[9] else "expired",
            }
            if row[8] is not None:
                operation = self.operations._select(connection, str(row[8]))
                result.update(
                    status=operation.status, operation_id=operation.id, attempt_id=operation.attempt_id
                )
                # Project only workflow status. Never return arbitrary persisted results or commands.
                if isinstance(operation.result, dict) and operation.status == "succeeded":
                    result["allocation_status"] = "unverified"
                    allocation = row[4]["selection"]["allocation"]
                    request = PoolAllocationRequest(self.pool, row[0], row[3], allocation["connection_limit"])
                    try:
                        receipt = safe_receipt(operation.external_receipt, request)
                    except ValueError:
                        pass
                    else:
                        if operation.result.get("database_allocation") == receipt:
                            result["allocation_status"] = "allocated"
            return result
