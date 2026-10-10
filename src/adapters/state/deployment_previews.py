"""Durable previews with fenced preparation and atomically linked approvals."""

import json
from uuid import uuid4

from domain.access import Action, Principal, ResourceOwner, permitted
from ports.artifacts import SourceArtifact
from ports.deployment_approvals import DeploymentApproval
from ports.deployment_previews import PreviewBusy, PreviewLease, PreviewUnavailable
from ports.operations import IdempotencyConflict


class PostgresDeploymentPreviews:
    def __init__(self, approvals):
        self.approvals = approvals
        self.operations = approvals.operations
        self.admission = approvals.admission

    def initialize(self):
        self.approvals.initialize()
        with self.operations.records._connection() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (self.operations.records.MIGRATION_LOCK,))
            connection.execute("""CREATE TABLE IF NOT EXISTS sky_state.preview_schema_versions(
                version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())""")
            versions = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT version FROM sky_state.preview_schema_versions ORDER BY version"
                ).fetchall()
            )
            if versions not in ((), (1,)):
                raise ValueError("Unsupported preview schema version")
            if not versions:
                connection.execute("""CREATE TABLE sky_state.deployment_previews(
                    workspace text NOT NULL, id uuid NOT NULL, request_key text NOT NULL, fingerprint text NOT NULL,
                    organization_id text NOT NULL, created_by text NOT NULL, application_id text NOT NULL,
                    account_id text NOT NULL, region text NOT NULL, options jsonb NOT NULL,
                    status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','ready')),
                    lease_owner text, lease_epoch bigint NOT NULL DEFAULT 0, lease_until timestamptz,
                    source_ref jsonb, prepared_ref jsonb, plan text, inspection jsonb, blockers jsonb,
                    preview_digest text, approval_id uuid,
                    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
                    expires_at timestamptz NOT NULL DEFAULT (clock_timestamp()+interval '24 hours'),
                    PRIMARY KEY(workspace,id),UNIQUE(workspace,request_key),
                    FOREIGN KEY(workspace,application_id) REFERENCES sky_state.application_owners(workspace,application_id),
                    FOREIGN KEY(workspace,approval_id) REFERENCES sky_state.deployment_approvals(workspace,id),
                    CHECK((lease_owner IS NULL)=(lease_until IS NULL)),
                    CHECK(status='pending' OR (source_ref IS NOT NULL AND prepared_ref IS NOT NULL
                        AND plan IS NOT NULL AND inspection IS NOT NULL AND blockers IS NOT NULL AND preview_digest IS NOT NULL)),
                    CHECK(approval_id IS NULL OR status='ready'))""")
                connection.execute("INSERT INTO sky_state.preview_schema_versions(version) VALUES(1)")

    @staticmethod
    def _authorize(principal, action=Action.DEPLOY):
        if not isinstance(principal, Principal) or not permitted(
            principal, action, ResourceOwner(principal.organization_id, principal.user_id)
        ):
            raise PermissionError("Deployment preview access denied")

    def _load(self, connection, principal, identity, *, lock=False):
        identity = self.operations._uuid(identity)
        row = connection.execute(
            """SELECT id,organization_id,created_by,application_id,status,options,
            source_ref,prepared_ref,plan,inspection,blockers,preview_digest,approval_id,
            created_at,expires_at,account_id,region,fingerprint
            FROM sky_state.deployment_previews WHERE workspace=%s AND id=%s"""
            + (" FOR UPDATE" if lock else ""),
            (self.operations.workspace, identity),
        ).fetchone()
        if row is None or (row[1], row[2]) != (principal.organization_id, principal.user_id):
            raise FileNotFoundError("Deployment preview not found")
        return row

    @staticmethod
    def _view(row):
        return {
            "id": str(row[0]),
            "organization_id": row[1],
            "created_by": row[2],
            "application_id": row[3],
            "status": row[4],
            "options": row[5],
            "source_ref": row[6],
            "prepared_ref": row[7],
            "plan": json.loads(row[8]) if row[8] is not None else None,
            "inspection": row[9],
            "blockers": row[10],
            "preview_digest": row[11],
            "approval_id": str(row[12]) if row[12] else None,
            "created_at": row[13].isoformat(),
            "expires_at": row[14].isoformat(),
            "account_id": row[15],
            "region": row[16],
        }

    def get(self, principal, identity):
        self._authorize(principal, Action.READ)
        with self.operations.records._connection() as connection:
            return self._view(self._load(connection, principal, identity))

    def reserve(self, principal, application_id, request_key, upload_sha256, options):
        self._authorize(principal)
        SourceArtifact(principal.organization_id, application_id, "a" * 32, "original", "a" * 64, 1, "a" * 64)
        request_key = self.operations._text(request_key, "request key")
        fingerprint = self.admission._digest(
            [application_id, upload_sha256, options, self.admission.account_id, self.admission.region]
        )
        scoped = self.admission._digest([principal.organization_id, principal.user_id, request_key])
        identity, owner = str(uuid4()), str(uuid4())
        with self.operations.records._connection() as connection:
            self.admission._owner(connection, principal, application_id)
            connection.execute(
                """INSERT INTO sky_state.deployment_previews
                (workspace,id,request_key,fingerprint,organization_id,created_by,application_id,account_id,region,options)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(workspace,request_key) DO NOTHING""",
                (
                    self.operations.workspace,
                    identity,
                    scoped,
                    fingerprint,
                    principal.organization_id,
                    principal.user_id,
                    application_id,
                    self.admission.account_id,
                    self.admission.region,
                    self.operations._json(options),
                ),
            )
            identity = connection.execute(
                "SELECT id FROM sky_state.deployment_previews WHERE workspace=%s AND request_key=%s",
                (self.operations.workspace, scoped),
            ).fetchone()[0]
            identity = str(identity)
            row = self._load(connection, principal, identity, lock=True)
            if row[17] != fingerprint:
                raise IdempotencyConflict("Upload request belongs to different content or configuration")
            if row[4] == "ready":
                return None, self._view(row)
            claimed = connection.execute(
                """UPDATE sky_state.deployment_previews SET lease_owner=%s,
                lease_epoch=lease_epoch+1,lease_until=clock_timestamp()+interval '120 seconds'
                WHERE workspace=%s AND id=%s AND expires_at>clock_timestamp()
                AND (lease_until IS NULL OR lease_until<=clock_timestamp()) RETURNING lease_epoch""",
                (owner, self.operations.workspace, identity),
            ).fetchone()
            if claimed is None:
                raise PreviewBusy("Preview preparation is busy or expired")
            return PreviewLease(str(identity), owner, claimed[0], self.operations.workspace), self._view(row)

    def release(self, lease):
        if lease.workspace != self.operations.workspace:
            return
        with self.operations.records._connection() as connection:
            connection.execute(
                """UPDATE sky_state.deployment_previews SET lease_owner=NULL,lease_until=NULL
                WHERE workspace=%s AND id=%s AND lease_owner=%s AND lease_epoch=%s""",
                (self.operations.workspace, lease.id, lease.owner, lease.epoch),
            )

    def finish(self, principal, lease, original, prepared, plan, inspection, blockers):
        self._authorize(principal)
        if lease.workspace != self.operations.workspace:
            raise PreviewUnavailable("Preparation workspace differs")
        plan = self.operations._document(plan)
        payload = self.operations._document(
            {
                "source_ref": original.record(),
                "prepared_ref": prepared.record(),
                "plan": plan,
                "inspection": inspection,
                "blockers": blockers,
            }
        )
        if (
            original.kind != "original"
            or prepared.kind != "prepared"
            or (original.organization_id, original.application_id, original.upload_id)
            != (prepared.organization_id, prepared.application_id, prepared.upload_id)
            or original.organization_id != principal.organization_id
            or prepared.upload_id != lease.id.replace("-", "")
            or plan.get("source_digest") != prepared.source_digest
        ):
            raise ValueError("Preview source identity or plan differs from prepared source")
        digest = self.admission._digest(payload)
        canonical = json.dumps(
            plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        with self.operations.records._connection() as connection:
            row = self._load(connection, principal, lease.id, lock=True)
            if row[3] != prepared.application_id or (row[15], row[16]) != (
                self.admission.account_id,
                self.admission.region,
            ):
                raise PreviewUnavailable("Preview execution scope changed")
            updated = connection.execute(
                """UPDATE sky_state.deployment_previews SET status='ready',source_ref=%s,
                prepared_ref=%s,plan=%s,inspection=%s,blockers=%s,preview_digest=%s,lease_owner=NULL,lease_until=NULL
                WHERE workspace=%s AND id=%s AND status='pending' AND lease_owner=%s AND lease_epoch=%s
                AND lease_until>clock_timestamp() AND expires_at>clock_timestamp() RETURNING id""",
                (
                    self.operations._json(payload["source_ref"]),
                    self.operations._json(payload["prepared_ref"]),
                    canonical,
                    self.operations._json(inspection),
                    self.operations._json(blockers),
                    digest,
                    self.operations.workspace,
                    lease.id,
                    lease.owner,
                    lease.epoch,
                ),
            ).fetchone()
            if updated is None:
                raise PreviewUnavailable("Preview preparation ownership expired")
            return self._view(self._load(connection, principal, lease.id))

    def approve(self, principal, identity, expected_digest):
        self._authorize(principal)
        with self.operations.records._connection() as connection:
            row = self._load(connection, principal, identity, lock=True)
            if row[4] != "ready" or row[11] != expected_digest:
                raise PreviewUnavailable("Preview is not ready or has changed")
            stored_payload = {
                "source_ref": row[6],
                "prepared_ref": row[7],
                "plan": json.loads(row[8]),
                "inspection": row[9],
                "blockers": row[10],
            }
            if self.admission._digest(stored_payload) != row[11]:
                raise PreviewUnavailable("Stored preview no longer matches its digest")
            if row[10]:
                raise PreviewUnavailable("Preview requires preparation before approval")
            if row[12]:
                receipt = connection.execute(
                    "SELECT expires_at FROM sky_state.deployment_approvals WHERE workspace=%s AND id=%s",
                    (self.operations.workspace, row[12]),
                ).fetchone()
                return DeploymentApproval(str(row[12]), receipt[0])
            valid = connection.execute(
                "SELECT expires_at>clock_timestamp() FROM sky_state.deployment_previews WHERE workspace=%s AND id=%s",
                (self.operations.workspace, identity),
            ).fetchone()[0]
            if not valid or (row[15], row[16]) != (self.admission.account_id, self.admission.region):
                raise PreviewUnavailable("Preview expired or execution scope changed")
            artifact = SourceArtifact.from_record(row[7])
            plan = json.loads(row[8])
            digest = self.admission._digest(plan)
            _, _, _, command = self.admission._prepare(
                principal, artifact, "preview-approval", plan, digest, artifact.source_digest
            )
            approval_id = str(uuid4())
            self.operations._document({**command, "approval_id": approval_id, "approved_plan_json": row[8]})
            receipt = self.approvals._approve_in_transaction(
                connection, principal, artifact, plan, digest, approval_id, row[8], 900
            )
            linked = connection.execute(
                """UPDATE sky_state.deployment_previews SET approval_id=%s
                WHERE workspace=%s AND id=%s AND expires_at>clock_timestamp() RETURNING id""",
                (approval_id, self.operations.workspace, identity),
            ).fetchone()
            if linked is None:
                raise PreviewUnavailable("Preview expired during approval")
            return receipt

    def check_ready(self):
        self.approvals.check_ready()
        with self.operations.records._connection() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            connection.execute("SET LOCAL statement_timeout=3000")
            versions = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT version FROM sky_state.preview_schema_versions ORDER BY version"
                ).fetchall()
            )
            if versions != (1,):
                raise ValueError("Unsupported preview schema version")
            connection.execute(
                "SELECT workspace,id,source_ref,prepared_ref,plan,inspection,blockers,preview_digest,approval_id,lease_owner,lease_epoch,lease_until,options,account_id,region,fingerprint,expires_at FROM sky_state.deployment_previews LIMIT 0"
            )
