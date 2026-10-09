"""Missing, corrupt or replaced service state must not become a fresh server."""

import json
import os
import pytest

from adapters.state.service_volume import MARKER_NAME, initialize_service_state, require_service_state


def test_missing_directory_is_not_created(tmp_path):
    root = tmp_path / "missing"
    for operation in (require_service_state, initialize_service_state):
        with pytest.raises(ValueError, match="state directory is missing"):
            operation(root)
    assert not root.exists()


def test_uninitialized_directory_is_not_started(tmp_path):
    with pytest.raises(ValueError, match="state marker is missing"):
        require_service_state(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_initialized_state_survives_restart_without_overwrite(tmp_path):
    initialize_service_state(tmp_path)
    marker = (tmp_path / MARKER_NAME).read_bytes()
    jobs = tmp_path / "example-job"
    jobs.mkdir()
    job = jobs / "job.json"
    job.write_text('{"status":"succeeded"}')
    assert require_service_state(tmp_path) == tmp_path.resolve()
    assert require_service_state(tmp_path) == tmp_path.resolve()
    with pytest.raises(ValueError, match="empty directory"):
        initialize_service_state(tmp_path)
    assert (tmp_path / MARKER_NAME).read_bytes() == marker
    assert job.read_text() == '{"status":"succeeded"}'


def test_existing_unmarked_state_cannot_be_reinitialized(tmp_path):
    record = tmp_path / "existing-job.json"
    record.write_text("original ownership record")
    with pytest.raises(ValueError, match="empty directory"):
        initialize_service_state(tmp_path)
    assert record.read_text() == "original ownership record"
    assert not (tmp_path / MARKER_NAME).exists()


def test_oversized_marker_is_rejected(tmp_path):
    initialize_service_state(tmp_path)
    marker = tmp_path / MARKER_NAME
    marker.write_text(marker.read_text() + " " * 4096)
    with pytest.raises(ValueError, match="size limit"):
        require_service_state(tmp_path)


@pytest.mark.parametrize("content", ["", "not-json", "{}", "[]", "null", '"unexpected"'])
def test_corrupt_marker_is_rejected_without_repair(tmp_path, content):
    marker = tmp_path / MARKER_NAME
    marker.write_text(content)
    with pytest.raises(ValueError, match="state marker"):
        require_service_state(tmp_path)
    assert marker.read_text() == content


@pytest.mark.parametrize("change", [{"version": 2}, {"version": True}, {"id": "wrong"}, {"kind": "wrong"}])
def test_unknown_marker_is_rejected(tmp_path, change):
    initialize_service_state(tmp_path)
    marker = tmp_path / MARKER_NAME
    record = json.loads(marker.read_text())
    record.update(change)
    marker.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="state marker is invalid"):
        require_service_state(tmp_path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX no-follow behavior")
def test_symlink_marker_cannot_substitute_another_volume(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    initialize_service_state(other)
    root = tmp_path / "state"
    root.mkdir()
    (root / MARKER_NAME).symlink_to(other / MARKER_NAME)
    with pytest.raises(ValueError, match="state marker"):
        require_service_state(root)


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink behavior")
def test_symlink_state_root_is_rejected(tmp_path):
    initialize_service_state(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        require_service_state(link)


@pytest.mark.skipif(os.name != "posix", reason="POSIX FIFO behavior")
def test_fifo_marker_is_rejected_without_waiting(tmp_path):
    os.mkfifo(tmp_path / MARKER_NAME)
    with pytest.raises(ValueError, match="regular file"):
        require_service_state(tmp_path)
