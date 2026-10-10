"""Lease-fenced build progress and job projection commit in one transaction."""
import json
from adapters.state.operations import PostgresOperationStore


class PostgresBuildExecutionStore(PostgresOperationStore):
    def _job(self, connection, operation):
        c = operation.command
        row = connection.execute("""SELECT j.document FROM sky_state.metadata_records j
            JOIN sky_state.deployment_approvals a ON a.workspace=j.workspace AND a.id=%s
            WHERE j.workspace=%s AND j.kind='job' AND j.record_id=%s
            AND a.operation_id=%s AND a.job_id=j.record_id AND a.revoked_at IS NULL
            AND a.organization_id=%s AND a.approved_by=%s AND a.application_id=%s
            AND a.plan=%s AND a.plan_digest=%s AND a.source_ref=%s
            FOR UPDATE OF j""", (c["approval_id"], self.workspace, c["job_id"], operation.id,
                c["organization_id"], c["created_by"], operation.application_id,
                c["approved_plan_json"], c["plan_digest"], self._json(c["source_ref"]))).fetchone()
        if row is None:
            raise ValueError("Build has no matching consumed approval/job")
        job = row[0]
        expected = {"id": c["job_id"], "operation_id": operation.id, "approval_id": c["approval_id"],
                    "application_id": operation.application_id, "organization_id": c["organization_id"],
                    "created_by": c["created_by"], "source_ref": c["source_ref"], "source_digest": c["source_digest"],
                    "plan_digest": c["plan_digest"], "plan": json.loads(c["approved_plan_json"]), "target": "aws"}
        if any(job.get(k) != v for k, v in expected.items()):
            raise ValueError("Build projection identity changed")
        return job

    def build_progress(self, lease, checkpoint, *, stage):
        if stage not in {"building", "waiting_build", "build_ready", "needs_attention", "failed"}:
            raise ValueError("Invalid build stage")
        checkpoint = self._document(checkpoint)
        with self.records._connection() as connection:
            if not self._owned(connection, lease):
                return False
            operation = self._select(connection, lease.operation_id)
            job = self._job(connection, operation)
            terminal = stage in {"build_ready", "needs_attention", "failed"}
            if stage in {"build_ready", "failed"} and operation.external_pending:
                raise ValueError("Verify the external build before settling")
            job.update(status=stage, deployment_state=("awaiting_deployment" if stage == "build_ready" else stage))
            if stage == "build_ready":
                job["build_result"] = checkpoint["build_result"]
            # No deploy URL or deployment success is synthesized from a built image.
            self._document(job)
            connection.execute("""UPDATE sky_state.metadata_records SET document=%s,
                revision=revision+1,modified_at=clock_timestamp()
                WHERE workspace=%s AND kind='job' AND record_id=%s""", (self._json(job), self.workspace, c_job_id(operation)))
            if terminal:
                state = "failed" if stage == "failed" else "needs_attention"
                connection.execute("""UPDATE sky_state.operations SET checkpoint=%s,status=%s,
                    lease_owner=NULL,lease_until=NULL,row_version=row_version+1 WHERE workspace=%s AND id=%s""",
                    (self._json(checkpoint), state, self.workspace, operation.id))
                if stage == "failed":
                    connection.execute("DELETE FROM sky_state.mutation_scopes WHERE workspace=%s AND operation_id=%s", (self.workspace, operation.id))
            else:
                connection.execute("""UPDATE sky_state.operations SET checkpoint=%s,row_version=row_version+1
                    WHERE workspace=%s AND id=%s""", (self._json(checkpoint), self.workspace, operation.id))
            self._event(connection, operation.id, stage)
            return True


def c_job_id(operation):
    return operation.command["job_id"]
