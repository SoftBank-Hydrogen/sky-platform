"""Owned metadata writes over the explicit CAS port, with no runtime activation.

This is an internal application boundary, not a public arbitrary-patch endpoint.
It neither admits operations nor grants a worker lease. Callers must validate the
workflow and execution ownership before using it. Cross-record/operation atomicity
requires a later transaction port; never describe two calls here as one commit.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime

from domain.access import Action, Principal, ResourceOwner, owner_from_record, permitted
from ports.state import (
    RecordConflict,
    StoredJob,
    StoredRecord,
    VersionedDeploymentRecordStore,
)

_IMMUTABLE = frozenset(
    {
        "id",
        "organization_id",
        "created_by",
        "application_id",
        "created_at",
        "target",
        "source_digest",
        "source_ref",
        "project",
        "operation_id",
        "group_id",
        "group_order",
        "job_record_version",
    }
)


@dataclass(frozen=True)
class WriteReceipt:
    job_id: str
    revision: int


def _revision(value, *, optional=False):
    if optional and value is None:
        return
    if type(value) is not int or not 1 <= value < 9223372036854775807:
        raise ValueError("A valid expected metadata revision is required")


def _json_object(value):
    if not isinstance(value, dict):
        raise ValueError("Metadata update must be an object")  # noqa: TRY004 -- document contract uses ValueError
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False)
        if len(encoded.encode()) > 65536:
            raise ValueError()
        return json.loads(encoded)
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("Invalid metadata update document") from None


def _identity(job_id):
    if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{16}", job_id):
        raise ValueError("Invalid job identity")


class DeploymentMetadataWriter:
    def __init__(self, records: VersionedDeploymentRecordStore):
        self.records = records

    def _job(self, principal, job_id, action):
        _identity(job_id)
        if not isinstance(principal, Principal) or action not in {Action.DEPLOY, Action.RETIRE}:
            raise PermissionError("Metadata write access denied")
        if not permitted(principal, action, ResourceOwner(principal.organization_id, principal.user_id)):
            raise PermissionError("Metadata write access denied")
        snapshot = self.records.load_job(job_id)
        record = snapshot.record
        # Shared DB writes never use the local legacy-owner exception.
        owner = owner_from_record(record) if isinstance(record, dict) else None
        if owner is None or not permitted(principal, action, owner):
            raise FileNotFoundError("Deployment not found")
        if record.get("id") != job_id:
            raise ValueError("Invalid persisted deployment record")
        _revision(snapshot.revision)
        return snapshot

    def snapshot(self, principal, job_id, *, action=Action.DEPLOY) -> StoredJob:
        """Carry the revision with an authorized detached write snapshot."""
        return deepcopy(self._job(principal, job_id, action))

    def patch(self, principal, job_id, expected_revision, changes, *, action=Action.DEPLOY) -> WriteReceipt:
        _revision(expected_revision)
        snapshot = self._job(principal, job_id, action)
        if snapshot.revision != expected_revision:
            raise RecordConflict("Deployment changed since the supplied snapshot")
        changes = _json_object(changes)
        if not changes or set(changes) & _IMMUTABLE:
            raise ValueError("Immutable deployment identity cannot be patched")
        if (
            not isinstance(changes.get("status", snapshot.record.get("status")), str)
            or ("plan" in changes and changes["plan"] is not None and not isinstance(changes["plan"], dict))
            or (
                "result" in changes
                and changes["result"] is not None
                and not isinstance(changes["result"], dict)
            )
            or (
                "events" in changes
                and (
                    not isinstance(changes["events"], list)
                    or any(not isinstance(item, dict) for item in changes["events"])
                )
            )
            or ("deployment_state" in changes and not isinstance(changes["deployment_state"], str))
        ):
            raise ValueError("Invalid deployment projection update")
        if (
            changes.get("deployment_state") in {"deleting", "deleted", "delete_failed"}
            and action != Action.RETIRE
        ):
            raise PermissionError("Retirement metadata requires retirement permission")
        record = {**deepcopy(snapshot.record), **changes}
        revision = self.records.save_job(job_id, record, expected_revision=expected_revision)
        return WriteReceipt(job_id, revision)

    def health_snapshot(self, principal, job_id) -> StoredRecord | None:
        self._job(principal, job_id, Action.DEPLOY)
        snapshot = self.records.load_health_record(job_id)
        if snapshot is not None:
            _revision(snapshot.revision)
            if not isinstance(snapshot.record, list) or any(
                not isinstance(item, dict) for item in snapshot.record
            ):
                raise ValueError("Invalid persisted health history")
        return deepcopy(snapshot)

    def record_health(
        self, principal, job_id, expected_revision, observation, *, retention=20
    ) -> WriteReceipt:
        """Append a validated probe result; CAS prevents lost concurrent observations.

        This authorizes a metadata writer, not a probe target or worker lease.
        expected_revision=None means create-only. The job and health record are
        separate transactions; this never changes the job's deployment status.
        """
        _revision(expected_revision, optional=True)
        if type(retention) is not int or not 1 <= retention <= 100:
            raise ValueError("Invalid health retention limit")
        observation = _json_object(observation)
        if (
            type(observation.get("healthy")) is not bool
            or not isinstance(observation.get("checked_at"), str)
            or not observation["checked_at"]
            or len(observation["checked_at"]) > 128
            or not isinstance(observation.get("reason"), str)
            or not observation["reason"]
            or len(observation["reason"]) > 2000
        ):
            raise ValueError("A checked health observation is required")
        try:
            checked_at = datetime.fromisoformat(observation["checked_at"])
            if checked_at.tzinfo is None:
                raise ValueError()
        except ValueError:
            raise ValueError("Health observation requires a timezone-aware check time") from None
        snapshot = self.health_snapshot(principal, job_id)
        current_revision = snapshot.revision if snapshot is not None else None
        if current_revision != expected_revision:
            raise RecordConflict("Health history changed since the supplied snapshot")
        history = (deepcopy(snapshot.record) if snapshot is not None else []) + [observation]
        revision = self.records.save_health(job_id, history[-retention:], expected_revision=expected_revision)
        return WriteReceipt(job_id, revision)
