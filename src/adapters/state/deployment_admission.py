"""Atomic AWS intake foundation. No public HTTP route or runtime activation.

Only trusted application composition supplies verified principals, stored source
references and an approved plan. The DB cannot verify S3 existence or authenticate
an approval; workers must restore/verify sources and recheck execution scope.
"""

import hashlib
import json
import re

from adapters.state.operations import PostgresOperationStore
from domain.access import Action, Principal, ResourceOwner, permitted
from ports.artifacts import SourceArtifact
from ports.deployment_admission import AdmittedDeployment
from ports.state import RecordConflict


class PostgresDeploymentAdmission:
    def __init__(self, operations: PostgresOperationStore, *, account_id: str, region: str):
        if not isinstance(account_id, str) or not re.fullmatch(r"[0-9]{12}", account_id):
            raise ValueError("Invalid deployment account")
        if not isinstance(region, str) or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", region):
            raise ValueError("Invalid deployment region")
        self.operations = operations
        self.account_id = account_id
        self.region = region

    def initialize(self):
        """Explicit, additive migration; never called by admit or a service startup."""
        self.operations.initialize()
        with self.operations.records._connection() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (self.operations.records.MIGRATION_LOCK,))
            connection.execute("""CREATE TABLE IF NOT EXISTS sky_state.admission_schema_versions (
                version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())""")
            versions = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT version FROM sky_state.admission_schema_versions ORDER BY version"
                ).fetchall()
            )
            if versions not in ((), (1,)):
                raise ValueError("Unsupported admission schema version")
            if not versions:
                connection.execute("""CREATE TABLE sky_state.application_owners (
                    workspace text NOT NULL, application_id text NOT NULL,
                    organization_id text NOT NULL, created_by text NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
                    PRIMARY KEY(workspace, application_id))""")
                connection.execute("INSERT INTO sky_state.admission_schema_versions(version) VALUES (1)")

    @staticmethod
    def _digest(value):
        return hashlib.sha256(
            json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()

    def _owner(self, connection, principal, application_id):
        workspace = self.operations.workspace
        created = connection.execute(
            """INSERT INTO sky_state.application_owners
            (workspace,application_id,organization_id,created_by) VALUES (%s,%s,%s,%s)
            ON CONFLICT DO NOTHING RETURNING application_id""",
            (workspace, application_id, principal.organization_id, principal.user_id),
        ).fetchone()
        owner = connection.execute(
            """SELECT organization_id,created_by
            FROM sky_state.application_owners WHERE workspace=%s AND application_id=%s FOR UPDATE""",
            (workspace, application_id),
        ).fetchone()
        if not permitted(principal, Action.DEPLOY, ResourceOwner(*owner)):
            raise FileNotFoundError("Application not found")
        if created:
            # Old state is not auto-claimed, even by an admin of the same organization.
            # Import/migration must establish ownership under a coordinated maintenance window.
            existing = connection.execute(
                """SELECT EXISTS(
                SELECT 1 FROM sky_state.metadata_records WHERE workspace=%s AND kind='job'
                AND coalesce(document->>'application_id',record_id)=%s
                UNION ALL SELECT 1 FROM sky_state.operations
                WHERE workspace=%s AND application_id=%s)""",
                (workspace, application_id, workspace, application_id),
            ).fetchone()[0]
            if existing:
                raise FileNotFoundError("Application ownership requires explicit migration")

    def admit(
        self, principal, artifact, request_key, approved_plan, *, expected_plan_digest, expected_source_digest
    ):
        plan, job_id, key, command = self._prepare(
            principal, artifact, request_key, approved_plan, expected_plan_digest, expected_source_digest
        )
        with self.operations.records._connection() as connection:
            return self._admit_in_transaction(
                connection, principal, artifact, plan, expected_plan_digest, job_id, key, command
            )

    def _prepare(
        self, principal, artifact, request_key, approved_plan, expected_plan_digest, expected_source_digest
    ):
        if not isinstance(principal, Principal) or not permitted(
            principal, Action.DEPLOY, ResourceOwner(principal.organization_id, principal.user_id)
        ):
            raise PermissionError("Deployment admission denied")
        if not isinstance(artifact, SourceArtifact):
            raise ValueError("Invalid source artifact")
        if artifact.organization_id != principal.organization_id:
            raise FileNotFoundError("Source artifact not found")
        if artifact.kind != "prepared":
            raise ValueError("An approved prepared source is required")
        if expected_source_digest != artifact.source_digest:
            raise ValueError("Source no longer matches approval")
        request_key = self.operations._text(request_key, "request key")
        plan = self.operations._document(approved_plan)
        if not isinstance(expected_plan_digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", expected_plan_digest
        ):
            raise ValueError("Invalid approved plan digest")
        if self._digest(plan) != expected_plan_digest:
            raise ValueError("Plan no longer matches approval")
        scope = [self.operations.workspace, principal.organization_id, principal.user_id, request_key]
        key = "admission:" + self._digest(scope)
        job_id = self._digest(["job", *scope])[:16]
        command = self.operations._document(
            {
                "version": 1,
                "job_id": job_id,
                "organization_id": principal.organization_id,
                "created_by": principal.user_id,
                "source_ref": artifact.record(),
                "source_digest": artifact.source_digest,
                "plan": plan,
                "plan_digest": expected_plan_digest,
                "target": "aws",
                "account_id": self.account_id,
                "region": self.region,
            }
        )
        return plan, job_id, key, command

    def _admit_in_transaction(
        self, connection, principal, artifact, plan, expected_plan_digest, job_id, key, command
    ):
        """Caller owns the transaction; usable alongside locked approval consumption."""
        self._owner(connection, principal, artifact.application_id)
        operation = self.operations._admit_in_transaction(
            connection, artifact.application_id, "deploy", key, command
        )
        document = {
            "id": job_id,
            "organization_id": principal.organization_id,
            "created_by": principal.user_id,
            "application_id": artifact.application_id,
            "operation_id": operation.id,
            "approval_id": command.get("approval_id"),
            "status": "queued",
            "target": "aws",
            "source_ref": artifact.record(),
            "source_digest": artifact.source_digest,
            "plan": plan,
            "plan_digest": expected_plan_digest,
            "result": None,
            "events": [],
            "deployment_state": "pending",
        }
        connection.execute(
            """INSERT INTO sky_state.metadata_records
            (workspace,kind,record_id,document)
            VALUES (%s,'job',%s,%s || jsonb_build_object('created_at',clock_timestamp()))
            ON CONFLICT DO NOTHING""",
            (self.operations.workspace, job_id, self.operations._json(document)),
        )
        existing = connection.execute(
            """SELECT document FROM sky_state.metadata_records
            WHERE workspace=%s AND kind='job' AND record_id=%s FOR UPDATE""",
            (self.operations.workspace, job_id),
        ).fetchone()[0]
        # Existing progress is preserved on replay. A colliding/foreign record is never overwritten.
        immutable = (
            "id",
            "organization_id",
            "created_by",
            "application_id",
            "operation_id",
            "approval_id",
            "source_ref",
            "source_digest",
            "target",
            "plan",
            "plan_digest",
        )
        if not isinstance(existing, dict) or any(
            existing.get(field) != document[field] for field in immutable
        ):
            raise RecordConflict("Deployment record does not match admitted operation")
        return AdmittedDeployment(job_id, operation.id)
