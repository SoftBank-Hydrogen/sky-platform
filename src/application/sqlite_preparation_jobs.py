"""Admit source-bound preparation jobs separately from approved deployments."""

import re
from datetime import datetime, timezone

from domain.access import Action, LoginSource, Principal, ResourceOwner, Role, permitted
from ports.artifacts import SourceArtifact
from ports.deployment_previews import PreviewUnavailable
from ports.remote_builds import canonical, digest, identity


def validate_command(command, application_id):
    fields = {"version", "organization_id", "created_by", "role", "login_source", "source_ref",
              "source_digest", "sqlite_conversion", "preview_id", "preview_digest"}
    if (not isinstance(command, dict) or set(command) != fields
            or type(command["version"]) is not int or command["version"] != 1):
        raise ValueError("Invalid SQLite preparation command")
    canonical(command)
    artifact = SourceArtifact.from_record(command["source_ref"])
    principal = Principal(command["created_by"], command["organization_id"],
                          Role(command["role"]), LoginSource(command["login_source"]))
    if (artifact.kind != "original" or artifact.application_id != application_id
            or artifact.organization_id != principal.organization_id
            or artifact.source_digest != command["source_digest"]
            or not permitted(principal, Action.DEPLOY, ResourceOwner(principal.organization_id, principal.user_id))):
        raise ValueError("Invalid SQLite preparation scope")
    identity(command["preview_id"])
    if not isinstance(command["preview_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", command["preview_digest"]):
        raise ValueError("Invalid SQLite preview digest")
    conversion = command["sqlite_conversion"]
    if not isinstance(conversion, dict) or set(conversion) != {"path", "source_sha256", "schema", "row_counts", "migration"}:
        raise ValueError("Reviewed SQLite manifest required")
    return principal, artifact


class SqlitePreparationJobs:
    def __init__(self, previews, operations, *, clock=lambda: datetime.now(timezone.utc)):
        if operations.workspace == previews.operations.workspace:
            raise ValueError("Preparation needs a separate operation workspace")
        self.previews, self.operations, self.clock = previews, operations, clock

    def submit(self, principal, preview_id, expected_digest, request_key):
        if not isinstance(principal, Principal) or not permitted(
                principal, Action.DEPLOY, ResourceOwner(principal.organization_id, principal.user_id)):
            raise PermissionError("SQLite preparation access denied")
        view = self.previews.get(principal, preview_id)
        payload = {key: view[key] for key in ("source_ref", "prepared_ref", "plan", "inspection", "blockers")}
        if (view["status"] != "ready" or expected_digest != view["preview_digest"]
                or digest(payload) != expected_digest
                or datetime.fromisoformat(view["expires_at"]) <= self.clock()
                or "sqlite_conversion_required" not in {b["code"] for b in view["blockers"]}):
            raise PreviewUnavailable("SQLite preview expired, changed or unsupported")
        command = {"version": 1, "organization_id": principal.organization_id, "created_by": principal.user_id,
                   "role": principal.role.value, "login_source": principal.login_source.value,
                   "source_ref": view["source_ref"], "source_digest": view["source_ref"]["source_digest"],
                   "sqlite_conversion": view["inspection"]["sqlite_conversion"],
                   "preview_id": view["id"], "preview_digest": expected_digest}
        validate_command(command, view["application_id"])
        if not isinstance(request_key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_key):
            raise ValueError("Invalid preparation request key")
        scoped = digest([principal.organization_id, principal.user_id, request_key])
        operation = self.operations.admit(view["application_id"], "prepare_sqlite", scoped, command)
        return {"id": operation.id, "status": operation.status, "kind": "prepare_sqlite"}

    def detail(self, principal, operation_id):
        operation = self.operations.get(operation_id)
        command = operation.command
        if (operation.kind != "prepare_sqlite" or not isinstance(principal, Principal)
                or (command.get("organization_id"), command.get("created_by")) != (principal.organization_id, principal.user_id)):
            raise FileNotFoundError("Preparation job not found")
        if not permitted(principal, Action.READ, ResourceOwner(principal.organization_id, principal.user_id)):
            raise PermissionError("Preparation job access denied")
        return {"id": operation.id, "status": operation.status, "kind": operation.kind, "result": operation.result}
