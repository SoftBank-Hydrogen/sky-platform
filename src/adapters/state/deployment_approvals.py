"""Persisted approval consumption in the same transaction as deployment admission.

Internal composition only: principal and validated plan come from trusted
application code, not caller-provided HTTP identity or arbitrary execution plans.
"""

import json
from uuid import uuid4

from domain.access import Action, Principal, ResourceOwner, Role, permitted
from ports.artifacts import SourceArtifact
from ports.deployment_admission import AdmittedDeployment
from ports.deployment_approvals import ApprovalUnavailable, DeploymentApproval
from ports.operations import IdempotencyConflict


class PostgresDeploymentApprovals:
    def __init__(self, admission):
        self.admission = admission
        self.operations = admission.operations

    def initialize(self):
        """Explicit maintenance migration; no service startup or runtime DDL."""
        self.admission.initialize()
        with self.operations.records._connection() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (self.operations.records.MIGRATION_LOCK,))
            connection.execute("""CREATE TABLE IF NOT EXISTS sky_state.approval_schema_versions (
                version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())""")
            versions = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT version FROM sky_state.approval_schema_versions ORDER BY version"
                ).fetchall()
            )
            if versions not in ((), (1,)):
                raise ValueError("Unsupported approval schema version")
            if not versions:
                connection.execute("""CREATE TABLE sky_state.deployment_approvals (
                    workspace text NOT NULL, id uuid NOT NULL,
                    organization_id text NOT NULL, approved_by text NOT NULL,
                    application_id text NOT NULL, source_ref jsonb NOT NULL CHECK(jsonb_typeof(source_ref)='object'),
                    plan text NOT NULL CHECK(jsonb_typeof(plan::jsonb)='object'), plan_digest text NOT NULL,
                    source_digest text NOT NULL, account_id text NOT NULL, region text NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT clock_timestamp(), expires_at timestamptz NOT NULL,
                    revoked_at timestamptz, consumed_key text, job_id text, operation_id uuid,
                    PRIMARY KEY(workspace,id),
                    FOREIGN KEY(workspace,application_id) REFERENCES sky_state.application_owners(workspace,application_id),
                    FOREIGN KEY(workspace,operation_id) REFERENCES sky_state.operations(workspace,id),
                    CHECK(expires_at>created_at),
                    CHECK((consumed_key IS NULL AND job_id IS NULL AND operation_id IS NULL)
                       OR (consumed_key IS NOT NULL AND job_id IS NOT NULL AND operation_id IS NOT NULL)),
                    CHECK(revoked_at IS NULL OR operation_id IS NULL))""")
                connection.execute("INSERT INTO sky_state.approval_schema_versions(version) VALUES (1)")

    def check_ready(self):
        """Read-only readiness for the opt-in approval boundary, never migrates."""
        from adapters.state.readiness import check_database_ready

        check_database_ready(self.operations.records.connection_factory, operations=True)
        with self.operations.records._connection() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            connection.execute("SET LOCAL statement_timeout=3000")
            for ledger in ("admission_schema_versions", "approval_schema_versions"):
                versions = tuple(
                    row[0]
                    for row in connection.execute(
                        f"SELECT version FROM sky_state.{ledger} ORDER BY version"
                    ).fetchall()
                )
                if versions != (1,):
                    raise ValueError("Unsupported approval admission schema version")
            connection.execute(
                "SELECT workspace,application_id,organization_id,created_by FROM sky_state.application_owners LIMIT 0"
            )
            connection.execute("""SELECT workspace,id,organization_id,approved_by,application_id,source_ref,plan,
                plan_digest,source_digest,account_id,region,expires_at,revoked_at,consumed_key,job_id,operation_id
                FROM sky_state.deployment_approvals LIMIT 0""")

    @staticmethod
    def _authorize(principal):
        if not isinstance(principal, Principal) or not permitted(
            principal, Action.DEPLOY, ResourceOwner(principal.organization_id, principal.user_id)
        ):
            raise PermissionError("Deployment approval access denied")

    def approve(self, principal, artifact, validated_plan, *, seconds=900):
        self._authorize(principal)
        seconds = self.operations._bounded(seconds, 1, 86400)
        plan = self.operations._document(validated_plan)
        digest = self.admission._digest(plan)
        # Reuse all admission input validation and total command-size bounds.
        plan, _, _, command = self.admission._prepare(
            principal,
            artifact,
            "approval-validation",
            plan,
            digest,
            artifact.source_digest if isinstance(artifact, SourceArtifact) else None,
        )
        identity = str(uuid4())
        canonical_plan = json.dumps(
            plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        self.operations._document({**command, "approval_id": identity, "approved_plan_json": canonical_plan})
        with self.operations.records._connection() as connection:
            self.admission._owner(connection, principal, artifact.application_id)
            row = connection.execute(
                """INSERT INTO sky_state.deployment_approvals
                (workspace,id,organization_id,approved_by,application_id,source_ref,plan,plan_digest,
                 source_digest,account_id,region,expires_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp()+(%s*interval '1 second'))
                RETURNING expires_at""",
                (
                    self.operations.workspace,
                    identity,
                    principal.organization_id,
                    principal.user_id,
                    artifact.application_id,
                    self.operations._json(artifact.record()),
                    json.dumps(
                        plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
                    ),
                    digest,
                    artifact.source_digest,
                    self.admission.account_id,
                    self.admission.region,
                    seconds,
                ),
            ).fetchone()
            return DeploymentApproval(identity, row[0])

    def _locked(self, connection, principal, approval_id, *, revoking=False):
        approval_id = self.operations._uuid(approval_id)
        row = connection.execute(
            """SELECT organization_id,approved_by,application_id,source_ref,plan,
            plan_digest,source_digest,account_id,region,revoked_at,consumed_key,job_id,operation_id
            FROM sky_state.deployment_approvals WHERE workspace=%s AND id=%s FOR UPDATE""",
            (self.operations.workspace, approval_id),
        ).fetchone()
        if (
            row is None
            or row[0] != principal.organization_id
            or (row[1] != principal.user_id and not (revoking and principal.role is Role.ADMIN))
        ):
            raise FileNotFoundError("Deployment approval not found")
        return row

    def revoke(self, principal, approval_id):
        self._authorize(principal)
        with self.operations.records._connection() as connection:
            row = self._locked(connection, principal, approval_id, revoking=True)
            if row[12] is not None:
                raise ApprovalUnavailable(
                    "An admitted deployment requires cancellation, not approval revocation"
                )
            if row[9] is not None:
                return False
            connection.execute(
                """UPDATE sky_state.deployment_approvals SET revoked_at=clock_timestamp()
                WHERE workspace=%s AND id=%s""",
                (self.operations.workspace, approval_id),
            )
            return True

    def submit(self, principal, approval_id, request_key):
        self._authorize(principal)
        approval_id = self.operations._uuid(approval_id)
        request_key = self.operations._text(request_key, "request key")
        consumption_key = self.admission._digest(
            [self.operations.workspace, principal.organization_id, principal.user_id, request_key]
        )
        with self.operations.records._connection() as connection:
            row = self._locked(connection, principal, approval_id)
            if row[12] is not None:
                if row[10] != consumption_key:
                    raise IdempotencyConflict("Approval was consumed by a different request")
                # Receipt replay succeeds even after expiry. No new operation is created.
                return AdmittedDeployment(row[11], str(row[12]))
            usable = connection.execute(
                """SELECT revoked_at IS NULL AND expires_at>clock_timestamp()
                FROM sky_state.deployment_approvals WHERE workspace=%s AND id=%s""",
                (self.operations.workspace, approval_id),
            ).fetchone()[0]
            if not usable:
                raise ApprovalUnavailable("Approval expired or was revoked")
            if (row[7], row[8]) != (self.admission.account_id, self.admission.region):
                raise ApprovalUnavailable("Approval execution scope differs from deployment configuration")
            artifact = SourceArtifact.from_record(row[3])
            if artifact.application_id != row[2] or artifact.organization_id != row[0]:
                raise ApprovalUnavailable("Approval source identity is inconsistent")
            plan, job_id, key, command = self.admission._prepare(
                principal, artifact, request_key, json.loads(row[4]), row[5], row[6]
            )
            command = self.operations._document(
                {**command, "approval_id": approval_id, "approved_plan_json": row[4]}
            )
            result = self.admission._admit_in_transaction(
                connection, principal, artifact, plan, row[5], job_id, key, command
            )
            self._consume(connection, approval_id, consumption_key, result)
            return result

    def _consume(self, connection, approval_id, consumption_key, result):
        row = connection.execute(
            """UPDATE sky_state.deployment_approvals
            SET consumed_key=%s,job_id=%s,operation_id=%s WHERE workspace=%s AND id=%s
            AND operation_id IS NULL AND revoked_at IS NULL AND expires_at>clock_timestamp() RETURNING id""",
            (consumption_key, result.job_id, result.operation_id, self.operations.workspace, approval_id),
        ).fetchone()
        if row is None:
            raise ApprovalUnavailable("Approval expired or was revoked before consumption")
