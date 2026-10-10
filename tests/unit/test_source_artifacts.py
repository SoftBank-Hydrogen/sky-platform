"""Source normalization, ownership, immutable refs and disposable workspaces."""

import hashlib
import io
import stat
import zipfile
from dataclasses import replace
from unittest.mock import Mock

import pytest

from application.deployment_core import source_digest
from application.source_artifacts import SourceArtifactService
from domain.access import LoginSource, Principal, Role
from ports.artifacts import SourceArtifact


class MemoryStore:
    def __init__(self):
        self.objects = {}

    def put(self, artifact, data):
        assert self.objects.get(artifact.key, data) == data
        self.objects[artifact.key] = data

    def get(self, artifact):
        return self.objects[artifact.key]


def principal(org="org1", role=Role.DEPLOYER):
    return Principal("user1", org, role, LoginSource.EXTERNAL_IDP)


def upload(files=None, compression=zipfile.ZIP_DEFLATED):
    body = io.BytesIO()
    with zipfile.ZipFile(body, "w", compression=compression) as bundle:
        for name, value in (files or {"server.py": "print('hello')"}).items():
            bundle.writestr(name, value)
    return body.getvalue()


def capture(service, data=None, who=None):
    return service.capture_upload(who or principal(), "game1", "a" * 32, upload() if data is None else data)


def test_original_prepared_and_restored_context_are_distinct_and_cleaned():
    store = MemoryStore()
    service = SourceArtifactService(store)
    raw = upload(
        {
            "game/server.py": "print('hello')",
            "game/.env": "PRIVATE_PASSWORD=secret",
            "game/.git/config": "private git",
            "game/node_modules/private.js": "excluded",
        }
    )
    original = capture(service, raw)
    assert original.upload_sha256 == hashlib.sha256(raw).hexdigest()
    assert original.artifact.kind == "original"
    with service.restore(principal(), original.artifact) as project:
        assert (project / "server.py").read_text() == "print('hello')"
        assert not (project / ".env").exists()
        assert not (project / ".git").exists()
        (project / "Dockerfile").write_text("FROM python:3.12-slim")
        approved = source_digest(project)
        prepared = service.capture_prepared(principal(), original.artifact, project, expected_digest=approved)
        first_path = project
    assert not first_path.exists()
    assert prepared.kind == "prepared" and prepared.key != original.artifact.key
    with service.restore(principal(), prepared) as project:
        assert (project / "Dockerfile").is_file()
        assert source_digest(project) == approved
    assert not project.exists()
    with service.restore(principal(), original.artifact) as project:
        assert not (project / "Dockerfile").exists()


def test_cleanup_also_occurs_when_build_raises():
    service = SourceArtifactService(MemoryStore())
    original = capture(service)
    with pytest.raises(RuntimeError), service.restore(principal(), original.artifact) as project:
        raise RuntimeError("fake build failure")
    assert not project.exists()


def test_snapshots_are_deterministic_and_references_round_trip():
    service = SourceArtifactService(MemoryStore())
    first = capture(service)
    second = capture(service)
    assert first == second
    assert SourceArtifact.from_record(first.artifact.record()) == first.artifact
    assert first.artifact.key.startswith("sources/org1/game1/" + "a" * 32 + "/original/")


@pytest.mark.parametrize("role", [Role.VIEWER])
def test_viewer_cannot_capture_or_restore(role):
    store = MemoryStore()
    service = SourceArtifactService(store)
    with pytest.raises(PermissionError):
        capture(service, who=principal(role=role))
    assert not store.objects
    original = capture(service)
    with pytest.raises(PermissionError), service.restore(principal(role=role), original.artifact):
        pytest.fail("Foreign/unauthorized source read")


def test_foreign_admin_cannot_read_source_or_call_store():
    service = SourceArtifactService(MemoryStore())
    original = capture(service)
    service.store = Mock(get=Mock(side_effect=AssertionError("Foreign object fetch")))
    with pytest.raises(PermissionError), service.restore(principal("org2", Role.ADMIN), original.artifact):
        pytest.fail("Foreign source read")


@pytest.mark.parametrize(
    "path",
    [
        "../server.py",
        "/server.py",
        "a\\server.py",
        "./server.py",
        "a//server.py",
        "a/../server.py",
        "a/" * 33 + "server.py",
        "bad\n/server.py",
        "a" * 1025 + "/server.py",
    ],
)
def test_unsafe_zip_never_reaches_store(path):
    store = Mock()
    with pytest.raises(ValueError):
        capture(SourceArtifactService(store), upload({path: "bad", "server.py": "ok"}))
    store.put.assert_not_called()


def test_symlink_zip_is_rejected_before_store():
    body = io.BytesIO()
    with zipfile.ZipFile(body, "w") as bundle:
        item = zipfile.ZipInfo("server.py")
        item.external_attr = (stat.S_IFLNK | 0o777) << 16
        bundle.writestr(item, "/etc/passwd")
    with pytest.raises(ValueError):
        capture(SourceArtifactService(Mock()), body.getvalue())


@pytest.mark.parametrize("data", [b"", b"not a zip"])
def test_invalid_zip_rejected(data):
    with pytest.raises(ValueError):
        capture(SourceArtifactService(Mock()), data)


def test_upload_size_limit_precedes_extraction(monkeypatch):
    monkeypatch.setattr("application.source_artifacts.MAX_UPLOAD", 1)
    store = Mock()
    with pytest.raises(ValueError):
        capture(SourceArtifactService(store))
    store.put.assert_not_called()


def test_expansion_and_compression_limits(monkeypatch):
    monkeypatch.setattr("application.source_artifacts.MAX_EXTRACTED", 1)
    with pytest.raises(ValueError):
        capture(SourceArtifactService(Mock()))
    monkeypatch.setattr("application.source_artifacts.MAX_EXTRACTED", 100000)
    with pytest.raises(ValueError):
        capture(SourceArtifactService(Mock()), upload(compression=zipfile.ZIP_LZMA))


def test_duplicate_zip_paths_rejected():
    body = io.BytesIO()
    with zipfile.ZipFile(body, "w") as bundle:
        bundle.writestr("server.py", "first")
        with pytest.warns(UserWarning):
            bundle.writestr("server.py", "second")
    store = Mock()
    with pytest.raises(ValueError):
        capture(SourceArtifactService(store), body.getvalue())
    store.put.assert_not_called()


def test_expanded_directory_count_is_bounded():
    # Fewer than 5,000 ZIP entries can imply more than 5,000 filesystem paths.
    files = {f"d{i}/sub/sub2/server.py": "x" for i in range(1300)}
    with pytest.raises(ValueError, match="path count"):
        capture(SourceArtifactService(Mock()), upload(files))


def test_prepared_snapshot_cannot_include_environment_or_changed_plan(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "server.py").write_text("ok")
    service = SourceArtifactService(Mock())
    original = SourceArtifact("org1", "game1", "a" * 32, "original", "b" * 64, 10, "c" * 64)
    with pytest.raises(ValueError, match="approved plan"):
        service.capture_prepared(principal(), original, project, expected_digest="b" * 64)
    (project / ".env").write_text("private")
    with pytest.raises(ValueError, match="excluded"):
        service.capture_prepared(principal(), original, project, expected_digest=source_digest(project))
    service.store.put.assert_not_called()


def test_untrusted_store_bytes_and_tree_digest_rejected():
    service = SourceArtifactService(MemoryStore())
    original = capture(service).artifact
    service.store.objects[original.key] = b"wrong bytes"
    with pytest.raises(ValueError), service.restore(principal(), original):
        pytest.fail("Unverified content")
    original = capture(SourceArtifactService(MemoryStore())).artifact
    other = SourceArtifactService(MemoryStore())
    valid = capture(other).artifact
    wrong = replace(valid, source_digest="b" * 64)
    with pytest.raises(ValueError, match="approved digest"), other.restore(principal(), wrong):
        pytest.fail("Wrong source tree")


@pytest.mark.parametrize(
    "change",
    [
        {"organization_id": "../bad"},
        {"application_id": "/bad"},
        {"upload_id": "wrong"},
        {"kind": []},
        {"kind": "logs"},
        {"sha256": "bad"},
        {"source_digest": "bad"},
        {"size": True},
        {"size": 0},
        {"size": 129 * 1024 * 1024},
    ],
)
def test_invalid_artifact_references(change):
    values = {
        "organization_id": "org1",
        "application_id": "game1",
        "upload_id": "a" * 32,
        "kind": "original",
        "sha256": "b" * 64,
        "size": 10,
        "source_digest": "c" * 64,
    }
    values.update(change)
    with pytest.raises(ValueError):
        SourceArtifact(**values)


@pytest.mark.parametrize(
    "change", [{"version": True}, {"version": 2}, {"bucket": "foreign"}, {"url": "https://evil.example"}]
)
def test_reference_record_rejects_extra_fields_and_unknown_version(change):
    artifact = SourceArtifact("org1", "game1", "a" * 32, "original", "b" * 64, 10, "c" * 64)
    with pytest.raises(ValueError):
        SourceArtifact.from_record({**artifact.record(), **change})


def test_capture_verifies_actual_snapshot_bytes_even_if_live_tree_hash_races(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "server.py"
    source.write_text("old source")
    approved = source_digest(project)
    real_digest = source_digest

    def racing_digest(path):
        if path == project:
            source.write_text("changed while snapshotting")
            return approved
        return real_digest(path)

    monkeypatch.setattr("application.source_artifacts.source_digest", racing_digest)
    store = Mock()
    original = SourceArtifact("org1", "game1", "a" * 32, "original", "b" * 64, 10, approved)
    with pytest.raises(ValueError, match="changed during capture"):
        SourceArtifactService(store).capture_prepared(
            principal(), original, project, expected_digest=approved
        )
    store.put.assert_not_called()


def test_corrupt_zip_payload_rejected_without_persistence():
    # Keep the central directory intact, but corrupt a stored entry's data/CRC.
    raw = bytearray(upload({"server.py": "unique-source-marker"}, compression=zipfile.ZIP_STORED))
    offset = raw.index(b"unique-source-marker")
    raw[offset] ^= 1
    store = Mock()
    with pytest.raises(ValueError, match="ZIP payload"):
        capture(SourceArtifactService(store), bytes(raw))
    store.put.assert_not_called()
