"""A lockfile may change only through the isolated, source-bound npm tool."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from application.consistency import check_source_change_scope
from application.npm_lockfile import sync_npm_lockfile


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, value: dict) -> bytes:
    data = (json.dumps(value) + "\n").encode()
    path.write_bytes(data)
    return data


def test_isolated_npm_lock_sync_is_bound_to_manifest_and_verified_source(tmp_path):
    source = tmp_path / "source"
    work = tmp_path / "work"
    source.mkdir()
    work.mkdir()
    before_manifest = {"name": "game", "version": "1.0.0", "dependencies": {"ws": "^8.22.0"}}
    after_manifest = {
        **before_manifest,
        "dependencies": {"ws": "^8.22.0", "pg": "^8.16.0"},
    }
    old_lock = {
        "name": "game",
        "lockfileVersion": 3,
        "packages": {
            "": {"dependencies": before_manifest["dependencies"]},
            "node_modules/ws": {"resolved": "https://registry.npmjs.org/ws/-/ws-8.22.0.tgz"},
        },
    }
    new_lock = {
        **old_lock,
        "packages": {
            **old_lock["packages"],
            "": {"dependencies": after_manifest["dependencies"]},
            "node_modules/pg": {"resolved": "https://registry.npmjs.org/pg/-/pg-8.16.0.tgz"},
        },
    }
    original_manifest_bytes = _write_json(source / "package.json", before_manifest)
    old_lock_bytes = _write_json(source / "package-lock.json", old_lock)
    manifest_bytes = _write_json(work / "package.json", after_manifest)
    (work / "package-lock.json").write_bytes(old_lock_bytes)

    def runner(args, **kwargs):
        assert args[:3] == ["docker", "run", "--rm"]
        assert "--read-only" in args
        assert "--ignore-scripts" in args[-1]
        assert "pg" in kwargs["input"]
        return SimpleNamespace(returncode=0, stdout=json.dumps(new_lock))

    marker = sync_npm_lockfile(source, work, runner=runner)
    assert marker["generator"] == "isolated_npm"
    assert json.loads((work / "package-lock.json").read_text()) == new_lock
    changes = [
        {
            "path": "package.json",
            "before_sha256": _sha(original_manifest_bytes),
            "after_sha256": _sha(manifest_bytes),
        },
        {
            "path": "package-lock.json",
            "before_sha256": _sha(old_lock_bytes),
            "after_sha256": marker["after_sha256"],
        },
    ]
    assert check_source_change_scope({"changes": changes}, source, npm_lock_sync=marker)["status"] == "pass"
    with pytest.raises(ValueError, match="CV-02"):
        check_source_change_scope({"changes": changes}, source)
    changes[0]["after_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="CV-02.*manifest"):
        check_source_change_scope({"changes": changes}, source, npm_lock_sync=marker)
    changes[0]["after_sha256"] = _sha(manifest_bytes)
    changes[1]["after_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="CV-02.*lockfile"):
        check_source_change_scope({"changes": changes}, source, npm_lock_sync=marker)


def test_npm_lock_sync_rejects_external_dependencies_before_running(tmp_path):
    source = tmp_path / "source"
    work = tmp_path / "work"
    source.mkdir()
    work.mkdir()
    manifest = {"dependencies": {"ws": "^8.22.0"}}
    lock = {"lockfileVersion": 3, "packages": {"": {"dependencies": manifest["dependencies"]}}}
    _write_json(source / "package.json", manifest)
    old_lock = _write_json(source / "package-lock.json", lock)
    _write_json(work / "package.json", {"dependencies": {"ws": "https://example.com/package.tgz"}})
    (work / "package-lock.json").write_bytes(old_lock)

    def forbidden_runner(*_args, **_kwargs):
        raise AssertionError("untrusted dependency reached npm")

    with pytest.raises(ValueError, match="registry packages"):
        sync_npm_lockfile(source, work, runner=forbidden_runner)
