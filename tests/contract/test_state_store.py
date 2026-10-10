"""Storage replacement must preserve restart, validation and write-failure behavior."""

import json
from copy import deepcopy
from unittest.mock import patch

import pytest

from adapters.state.records import DirectoryDeploymentRecordStore
from interfaces.http.server import App
from ports.state import StoredJob

JOB_ID = "a" * 16
MODIFIED_AT = "2026-10-10T00:00:00+00:00"


class MemoryRecordStore:
    """Test storage has no paths and never aliases the App's mutable records."""

    def __init__(self):
        self.jobs = {}
        self.health = {}
        self.github_sources = None

    def list_job_ids(self):
        return tuple(sorted(self.jobs))

    def load_job(self, job_id):
        return StoredJob(deepcopy(self.jobs[job_id]), MODIFIED_AT)

    def save_job(self, job_id, record):
        self.jobs[job_id] = deepcopy(record)

    def load_health(self, job_id):
        return deepcopy(self.health.get(job_id))

    def save_health(self, job_id, history):
        self.health[job_id] = deepcopy(history)

    def load_github_sources(self):
        return deepcopy(self.github_sources)

    def save_github_sources(self, records):
        self.github_sources = deepcopy(records)


def job(root, **overrides):
    return {
        "id": JOB_ID,
        "mode": "agent",
        "status": "succeeded",
        "project": str(root / JOB_ID / "source"),
        "plan": None,
        "events": [],
        "attempts": 1,
        "result": {"url": "http://127.0.0.1:12345"},
        **overrides,
    }


def app(root, store):
    return App(root, monitor_interval=0, github_poll_interval=0, record_store=store)


def test_injected_store_survives_restart_without_local_metadata(tmp_path):
    store = MemoryRecordStore()
    original = app(tmp_path, store)
    original.jobs[JOB_ID] = job(tmp_path)
    original.save(JOB_ID)
    original.jobs[JOB_ID]["result"]["url"] = "http://changed.invalid"
    restarted = app(tmp_path, store)
    assert restarted.jobs[JOB_ID]["result"]["url"] == "http://127.0.0.1:12345"
    assert restarted.jobs[JOB_ID]["created_at"] == MODIFIED_AT
    assert restarted.jobs[JOB_ID]["job_record_version"] == 1
    assert not (tmp_path / JOB_ID / "job.json").exists()


def test_committed_upload_is_not_deleted_when_record_is_injected(tmp_path):
    source = tmp_path / JOB_ID / "source"
    source.mkdir(parents=True)
    (source / "main.py").write_text("print('keep')")
    (source.parent / ".uncommitted-upload").touch()
    store = MemoryRecordStore()
    store.jobs[JOB_ID] = job(tmp_path)
    assert JOB_ID in app(tmp_path, store).jobs
    assert (source / "main.py").read_text() == "print('keep')"


def test_injected_restart_preserves_uncertain_release(tmp_path):
    store = MemoryRecordStore()
    store.jobs[JOB_ID] = job(
        tmp_path, status="running", release_rollback_state="running", release_rollback_submitted=True
    )
    restored = app(tmp_path, store)
    assert restored.jobs[JOB_ID]["status"] == "interrupted"
    assert restored.jobs[JOB_ID]["release_rollback_state"] == "needs_attention"
    assert restored.jobs[JOB_ID]["deployment_state"] == "needs_attention"
    assert store.jobs[JOB_ID]["release_rollback_state"] == "needs_attention"


def test_invalid_record_does_not_hide_other_jobs_or_get_overwritten(tmp_path):
    store = MemoryRecordStore()
    invalid_id = "b" * 16
    invalid = job(tmp_path, id=invalid_id, job_record_version=999)
    store.jobs.update({JOB_ID: job(tmp_path), invalid_id: invalid})
    restored = app(tmp_path, store)
    assert set(restored.jobs) == {JOB_ID}
    assert any(invalid_id in warning for warning in restored.recovery_warnings)
    assert store.jobs[invalid_id] == invalid


def test_injected_save_failure_removes_uncommitted_success(tmp_path):
    store = MemoryRecordStore()
    instance = app(tmp_path, store)
    instance.jobs[JOB_ID] = job(tmp_path)
    with (
        patch.object(store, "save_job", side_effect=OSError("unavailable")),
        pytest.raises(RuntimeError, match="저장에 실패"),
    ):
        instance.save(JOB_ID)
    assert instance.jobs[JOB_ID]["status"] == "failed"
    assert "result" not in instance.jobs[JOB_ID]
    assert store.jobs == {}


def test_health_and_github_settings_round_trip_through_injected_store(tmp_path):
    store = MemoryRecordStore()
    store.jobs[JOB_ID] = job(tmp_path)
    instance = app(tmp_path, store)
    finding = {"healthy": False, "checked_at": MODIFIED_AT, "reason": "offline"}
    with patch("application.monitoring.check_deployment", return_value=finding):
        instance.check_and_record_health(JOB_ID)
    source_id = "c" * 16
    instance.github_sources[source_id] = {
        "id": source_id,
        "repository_url": "https://github.com/team/game",
        "branch": "main",
        "enabled": True,
        "application_id": "my-game",
        "targets": ["local-docker"],
        "public": False,
        "last_revision": "d" * 40,
        "last_job_ids": [JOB_ID],
    }
    instance.save_github_sources()
    instance.set_github_source_enabled(source_id, False)
    restarted = app(tmp_path, store)
    assert restarted.health_history[JOB_ID] == [{**finding, "source": "manual"}]
    assert restarted.github_sources[source_id]["enabled"] is False
    assert restarted.jobs[JOB_ID]["status"] == "succeeded"
    assert not (tmp_path / "github-sources.json").exists()
    assert not (tmp_path / JOB_ID / "health.json").exists()


@pytest.mark.parametrize("kind", ["job", "health", "github_sources"])
def test_directory_store_atomic_failure_keeps_previous_record(tmp_path, kind):
    directory = tmp_path / JOB_ID
    directory.mkdir()
    store = DirectoryDeploymentRecordStore(tmp_path)
    if kind == "job":
        save = lambda value: store.save_job(JOB_ID, {"value": value})
        path = directory / "job.json"
    elif kind == "health":
        save = lambda value: store.save_health(JOB_ID, [{"value": value}])
        path = directory / "health.json"
    else:
        save = lambda value: store.save_github_sources([{"value": value}])
        path = tmp_path / "github-sources.json"
    save("original")
    original = path.read_bytes()
    with (
        patch("adapters.state.records.os.replace", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        save("replacement")
    assert path.read_bytes() == original
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.rglob("*.tmp"))


@pytest.mark.parametrize("record_name", ["health.json", "github-sources.json"])
@pytest.mark.parametrize("invalid", ["null", "{invalid"])
def test_bad_optional_json_is_reported_without_losing_job(tmp_path, record_name, invalid):
    source = tmp_path / JOB_ID / "source"
    source.mkdir(parents=True)
    (source.parent / "job.json").write_text(json.dumps(job(tmp_path)))
    path = source.parent / record_name if record_name == "health.json" else tmp_path / record_name
    path.write_text(invalid)
    restored = App(tmp_path, monitor_interval=0, github_poll_interval=0)
    assert JOB_ID in restored.jobs
    assert restored.recovery_warnings
    assert path.read_text() == invalid


@pytest.mark.parametrize("job_id", ["../escape", "", "a/b", ".", "..", "/absolute", "x\\y"])
def test_directory_store_rejects_invalid_job_ids(tmp_path, job_id):
    store = DirectoryDeploymentRecordStore(tmp_path)
    with pytest.raises(ValueError, match="Invalid job identity"):
        store.save_job(job_id, {})
    assert list(tmp_path.iterdir()) == []


def test_github_symlink_is_rejected_without_reading_target(tmp_path):
    target = tmp_path / "other.json"
    target.write_text("[]")
    (tmp_path / "github-sources.json").symlink_to(target)
    with pytest.raises(ValueError, match="unsafe source record"):
        DirectoryDeploymentRecordStore(tmp_path).load_github_sources()
