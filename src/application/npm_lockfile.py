"""Regenerate an npm lockfile from a changed manifest in an isolated tool container."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path


_IMAGE = "node:22-bookworm-slim"
_PACKAGE = re.compile(r"^(?:@[a-z0-9._-]+/)?[a-z0-9._-]+$")
_VERSION = re.compile(r"^[~^]?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
_SECTIONS = ("dependencies", "devDependencies", "optionalDependencies")
_SCRIPT = r"""
const fs = require('node:fs');
const cp = require('node:child_process');
const root = '/tmp/sky-npm-lock';
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
fs.mkdirSync(root);
fs.writeFileSync(root + '/package.json', JSON.stringify(input.manifest));
fs.writeFileSync(root + '/package-lock.json', JSON.stringify(input.lock));
const result = cp.spawnSync('npm', ['install', '--package-lock-only', '--ignore-scripts',
  '--no-audit', '--no-fund', '--loglevel=error', '--registry=https://registry.npmjs.org'],
  {cwd: root, timeout: 120000, stdio: ['ignore', 'ignore', 'ignore'], env: {
    PATH: process.env.PATH, HOME: '/tmp', npm_config_cache: '/tmp/npm-cache',
    npm_config_userconfig: '/dev/null', npm_config_ignore_scripts: 'true'
  }});
if (result.status !== 0) process.exit(2);
process.stdout.write(fs.readFileSync(root + '/package-lock.json'));
"""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path, limit: int) -> tuple[bytes, dict]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError("CV-02: npm manifest or lockfile is missing or too large")
    content = path.read_bytes()
    try:
        parsed = json.loads(content)
    except (UnicodeDecodeError, ValueError):
        raise ValueError("CV-02: npm manifest or lockfile is invalid JSON") from None
    if not isinstance(parsed, dict):
        raise ValueError("CV-02: npm manifest or lockfile is not an object")
    return content, parsed


def _validate_registry_only(manifest: dict, lock: dict) -> None:
    for section in _SECTIONS:
        dependencies = manifest.get(section, {})
        if not isinstance(dependencies, dict) or any(
            not isinstance(name, str) or not _PACKAGE.fullmatch(name)
            or not isinstance(version, str) or not _VERSION.fullmatch(version)
            for name, version in dependencies.items()
        ):
            raise ValueError("CV-02: npm lock sync supports only registry packages with fixed semver ranges")
    packages = lock.get("packages")
    if not isinstance(packages, dict) or any(
        not isinstance(item, dict)
        or ("resolved" in item and (not isinstance(item["resolved"], str)
            or not item["resolved"].startswith("https://registry.npmjs.org/")))
        for item in packages.values()
    ):
        raise ValueError("CV-02: npm lock sync supports only the public npm registry")


def sync_npm_lockfile(original: Path, work: Path, *, runner=subprocess.run) -> dict:
    """Return hashes only; never expose npm output or accept arbitrary package scripts."""
    original_manifest, _ = _read_json(original / "package.json", 64 * 1024)
    original_lock, _ = _read_json(original / "package-lock.json", 2 * 1024 * 1024)
    manifest_bytes, manifest = _read_json(work / "package.json", 64 * 1024)
    lock_bytes, lock = _read_json(work / "package-lock.json", 2 * 1024 * 1024)
    if _sha(original_manifest) == _sha(manifest_bytes):
        raise ValueError("CV-02: Change package.json before syncing its lockfile")
    if lock_bytes != original_lock:
        raise ValueError("CV-02: npm lockfile was changed outside the sync tool")
    _validate_registry_only(manifest, lock)
    args = ["docker", "run", "--rm", "-i", "--read-only",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=128m", "--cap-drop=ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", "128",
            "--memory", "512m", "--cpus", "1", "--user", "65534:65534",
            _IMAGE, "node", "-e", _SCRIPT]
    try:
        result = runner(args, input=json.dumps({"manifest": manifest, "lock": lock}),
                        text=True, capture_output=True, timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("CV-02: Isolated npm lockfile sync could not finish") from None
    if result.returncode or len(result.stdout) > 2 * 1024 * 1024:
        raise ValueError("CV-02: Isolated npm lockfile sync failed")
    try:
        generated = json.loads(result.stdout)
    except ValueError:
        raise ValueError("CV-02: npm returned an invalid lockfile") from None
    if not isinstance(generated, dict) or generated.get("lockfileVersion") not in {2, 3}:
        raise ValueError("CV-02: npm returned an unsupported lockfile")
    _validate_registry_only(manifest, generated)
    root = generated["packages"].get("")
    if not isinstance(root, dict) or any(root.get(section, {}) != manifest.get(section, {}) for section in _SECTIONS):
        raise ValueError("CV-02: npm lockfile does not match the changed package.json")
    output = (json.dumps(generated, indent=2, ensure_ascii=False) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(prefix=".sky-package-lock-", dir=work)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(output)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, work / "package-lock.json")
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"generator": "isolated_npm", "manifest_sha256": _sha(manifest_bytes),
            "before_sha256": _sha(original_lock), "after_sha256": _sha(output)}
