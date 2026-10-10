"""No DB connection: exercise application writes with an in-memory CAS test double."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from application.deployment_writes import DeploymentMetadataWriter
from domain.access import Action, LoginSource, Principal, Role
from ports.state import RecordConflict, StoredJob, StoredRecord

ID = "a" * 16


def principal(org="org1", role=Role.DEPLOYER):
    return Principal("user1", org, role, LoginSource.LOCAL)


class Records:
    def __init__(self):
        self.job = {
            "id": ID,
            "organization_id": "org1",
            "created_by": "author",
            "application_id": "game",
            "created_at": "2026-10-10T00:00:00Z",
            "status": "running",
            "events": [],
        }
        self.revision = 1
        self.health = None
        self.health_revision = None
        self.save_calls = 0
        self.before_save = None
        self.uncertain = False

    def load_job(self, job_id):
        if self.job is None:
            raise FileNotFoundError("Deployment not found")
        return StoredJob(deepcopy(self.job), "unused", self.revision)

    def save_job(self, job_id, record, *, expected_revision=None):
        self.save_calls += 1
        if self.before_save:
            self.before_save(self)
        if expected_revision != self.revision:
            raise RecordConflict("CAS conflict")
        self.job = deepcopy(record)
        self.revision += 1
        if self.uncertain:
            raise OSError("Commit response lost")
        return self.revision

    def load_health_record(self, job_id):
        return (
            StoredRecord(deepcopy(self.health), "unused", self.health_revision)
            if self.health is not None
            else None
        )

    def save_health(self, job_id, history, *, expected_revision=None):
        self.save_calls += 1
        if self.before_save:
            self.before_save(self)
        if expected_revision != self.health_revision:
            raise RecordConflict("CAS conflict")
        self.health = deepcopy(history)
        self.health_revision = (self.health_revision or 0) + 1
        if self.uncertain:
            raise OSError("Commit response lost")
        return self.health_revision


@pytest.fixture
def records():
    return Records()


def observation(healthy=True):
    return {"healthy": healthy, "reason": "checked", "checked_at": "2026-10-10T00:00:00Z"}


def test_patch_preserves_identity_and_returns_revision_not_a_new_read(records):
    writer = DeploymentMetadataWriter(records)
    changes = {"status": "succeeded", "result": {"url": "https://game.example"}}
    receipt = writer.patch(principal(), ID, 1, changes)
    changes["result"]["url"] = "mutated"
    assert receipt.job_id == ID and receipt.revision == 2
    assert records.job["result"]["url"] == "https://game.example"
    assert records.job["created_by"] == "author"
    assert records.job["application_id"] == "game"


def test_snapshot_does_not_mutate_store(records):
    writer = DeploymentMetadataWriter(records)
    snapshot = writer.snapshot(principal(), ID)
    snapshot.record["events"].append("local")
    assert records.job["events"] == [] and snapshot.revision == 1


@pytest.mark.parametrize("actor", [None, principal(role=Role.VIEWER)])
def test_unauthorized_actor_does_not_read_or_write_store(actor):
    records = Mock()
    with pytest.raises(PermissionError):
        DeploymentMetadataWriter(records).patch(actor, ID, 1, {"status": "succeeded"})
    records.load_job.assert_not_called()
    records.save_job.assert_not_called()


@pytest.mark.parametrize("role", list(Role))
def test_foreign_organization_cannot_modify(records, role):
    error = PermissionError if role is Role.VIEWER else FileNotFoundError
    with pytest.raises(error):
        DeploymentMetadataWriter(records).patch(principal("org2", role), ID, 1, {"status": "succeeded"})
    assert records.save_calls == 0


@pytest.mark.parametrize(
    "field",
    [
        "organization_id",
        "created_by",
        "id",
        "application_id",
        "created_at",
        "target",
        "source_digest",
        "source_ref",
        "project",
        "operation_id",
        "group_id",
        "group_order",
    ],
)
def test_immutable_identity_cannot_be_changed_even_by_admin(records, field):
    with pytest.raises(ValueError, match="Immutable"):
        DeploymentMetadataWriter(records).patch(principal(role=Role.ADMIN), ID, 1, {field: "changed"})
    assert records.save_calls == 0


@pytest.mark.parametrize("revision", [None, True, 0, -1, "1", 9223372036854775807])
def test_explicit_positive_revision_required(records, revision):
    with pytest.raises(ValueError):
        DeploymentMetadataWriter(records).patch(principal(), ID, revision, {"status": "succeeded"})
    assert records.save_calls == 0


def test_stale_snapshot_does_not_reach_save(records):
    records.revision = 2
    with pytest.raises(RecordConflict):
        DeploymentMetadataWriter(records).patch(principal(), ID, 1, {"status": "succeeded"})
    assert records.save_calls == 0


def test_write_racing_after_load_is_rejected_by_store_cas(records):
    def competitor(store):
        store.revision += 1
        store.job["events"].append("other writer")

    records.before_save = competitor
    with pytest.raises(RecordConflict):
        DeploymentMetadataWriter(records).patch(principal(), ID, 1, {"status": "succeeded"})
    assert records.job["status"] == "running" and records.job["events"] == ["other writer"]
    assert records.save_calls == 1


def test_uncertain_job_commit_is_not_retried(records):
    records.uncertain = True
    with pytest.raises(OSError):
        DeploymentMetadataWriter(records).patch(principal(), ID, 1, {"status": "succeeded"})
    assert records.save_calls == 1 and records.job["status"] == "succeeded"
    with pytest.raises(RecordConflict):
        DeploymentMetadataWriter(records).patch(principal(), ID, 1, {"status": "succeeded"})
    assert records.save_calls == 1


@pytest.mark.parametrize(
    "owner", [{}, {"organization_id": "org1"}, {"organization_id": "org1", "created_by": 4}]
)
def test_unowned_legacy_and_invalid_records_are_not_claimed(records, owner):
    records.job = {"id": ID, **owner}
    with pytest.raises(FileNotFoundError):
        DeploymentMetadataWriter(records).patch(principal(role=Role.ADMIN), ID, 1, {"status": "succeeded"})
    assert records.save_calls == 0


def test_retirement_requires_admin_and_cannot_use_read_action(records):
    writer = DeploymentMetadataWriter(records)
    with pytest.raises(PermissionError):
        writer.patch(principal(), ID, 1, {"deployment_state": "deleted"}, action=Action.RETIRE)
    with pytest.raises(PermissionError):
        writer.patch(principal(role=Role.ADMIN), ID, 1, {"status": "succeeded"}, action=Action.READ)
    assert (
        writer.patch(
            principal(role=Role.ADMIN), ID, 1, {"deployment_state": "deleted"}, action=Action.RETIRE
        ).revision
        == 2
    )


def test_legacy_nonversioned_port_is_not_accepted(records):
    records.revision = None
    with pytest.raises(ValueError):
        DeploymentMetadataWriter(records).patch(principal(), ID, 1, {"status": "succeeded"})
    assert records.save_calls == 0


@pytest.mark.parametrize("changes", [{}, [], {"result": float("nan")}, {"result": object()}])
def test_invalid_patch_does_not_write(records, changes):
    with pytest.raises(ValueError):
        DeploymentMetadataWriter(records).patch(principal(), ID, 1, changes)
    assert records.save_calls == 0


def test_health_create_append_retention_and_independent_job_revision(records):
    writer = DeploymentMetadataWriter(records)
    assert writer.record_health(principal(), ID, None, observation()).revision == 1
    writer.record_health(principal(), ID, 1, observation(False), retention=1)
    assert records.health == [observation(False)]
    assert records.revision == 1 and records.job["status"] == "running"
    snapshot = writer.health_snapshot(principal(), ID)
    snapshot.record.clear()
    assert len(records.health) == 1


def test_health_create_only_and_stale_updates_never_overwrite_history(records):
    writer = DeploymentMetadataWriter(records)
    writer.record_health(principal(), ID, None, observation())
    for stale in (None, 2):
        with pytest.raises(RecordConflict):
            writer.record_health(principal(), ID, stale, observation(False))
    assert records.save_calls == 1 and records.health == [observation()]


def test_health_race_is_rejected_by_store_cas(records):
    writer = DeploymentMetadataWriter(records)
    writer.record_health(principal(), ID, None, observation())

    def competitor(store):
        store.health.append({"source": "other"})
        store.health_revision += 1

    records.before_save = competitor
    with pytest.raises(RecordConflict):
        writer.record_health(principal(), ID, 1, observation(False))
    assert records.health[-1] == {"source": "other"}


def test_uncertain_health_commit_is_not_retried(records):
    records.uncertain = True
    with pytest.raises(OSError):
        DeploymentMetadataWriter(records).record_health(principal(), ID, None, observation())
    assert records.save_calls == 1 and records.health == [observation()]


@pytest.mark.parametrize(
    "bad", [{}, {"healthy": "true"}, {"healthy": True, "reason": "", "checked_at": "now"}]
)
def test_invalid_health_result_is_not_recorded(records, bad):
    with pytest.raises(ValueError):
        DeploymentMetadataWriter(records).record_health(principal(), ID, None, bad)
    assert records.save_calls == 0


def test_corrupt_persisted_health_is_not_discarded(records):
    records.health = {"private": "invalid"}
    records.health_revision = 1
    with pytest.raises(ValueError, match="Invalid persisted"):
        DeploymentMetadataWriter(records).record_health(principal(), ID, 1, observation())
    assert records.save_calls == 0


def test_deployer_cannot_mark_retirement_using_default_action(records):
    with pytest.raises(PermissionError):
        DeploymentMetadataWriter(records).patch(principal(), ID, 1, {"deployment_state": "deleted"})
    assert records.save_calls == 0


@pytest.mark.parametrize("time", ["now", "2026-10-10T00:00:00"])
def test_health_time_requires_an_explicit_timezone(records, time):
    value = {**observation(), "checked_at": time}
    with pytest.raises(ValueError):
        DeploymentMetadataWriter(records).record_health(principal(), ID, None, value)
    assert records.save_calls == 0


@pytest.mark.parametrize(
    "changes",
    [{"status": []}, {"deployment_state": {}}, {"result": []}, {"plan": []}, {"events": ["invalid"]}],
)
def test_patch_cannot_corrupt_read_projection_shapes(records, changes):
    with pytest.raises(ValueError, match="projection"):
        DeploymentMetadataWriter(records).patch(principal(), ID, 1, changes)
    assert records.save_calls == 0


def test_format_version_cannot_be_patched(records):
    with pytest.raises(ValueError, match="Immutable"):
        DeploymentMetadataWriter(records).patch(principal(), ID, 1, {"job_record_version": 0})
    assert records.save_calls == 0
