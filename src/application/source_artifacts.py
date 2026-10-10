"""Authorized source capture and ephemeral restoration for future B admission/builds.

Raw ZIP uploads are validated and filtered before storage. The original artifact
is the normalized source before transformations, not a byte-for-byte raw upload.
No deployment admission, source execution, DB mutation or runtime activation occurs.
"""

import hashlib
import io
import os
import re
import stat
import tempfile
import zipfile
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from application.deployment_core import MAX_EXTRACTED, MAX_UPLOAD, extract_project, ignored_source_path, source_digest
from domain.access import Action, Principal, ResourceOwner, permitted
from ports.artifacts import MAX_ARTIFACT_BYTES, SourceArtifact, SourceArtifactStore


@dataclass(frozen=True)
class CapturedSource:
    artifact: SourceArtifact
    upload_sha256: str


def _authorize(principal, organization_id):
    if (not isinstance(principal, Principal)
            or not permitted(principal, Action.DEPLOY, ResourceOwner(organization_id, principal.user_id))):
        raise PermissionError("Source artifact access denied")


def _path(raw):
    path = PurePosixPath(raw)
    if (not raw or len(raw.encode()) > 1024 or any(ord(char) < 32 for char in raw)
            or "\\" in raw or path.is_absolute() or ".." in path.parts
            or path.as_posix() != raw or len(path.parts) > 32):
        raise ValueError("Unsafe source path")
    return path


def _validate_zip(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            entries = bundle.infolist()
            if len(entries) > 5000 or sum(e.file_size for e in entries) > MAX_EXTRACTED:
                raise ValueError("ZIP exceeds source limits")
            paths = set()
            for entry in entries:
                path = _path(entry.filename.rstrip("/") if entry.is_dir() else entry.filename)
                paths.add(path.as_posix())
                paths.update(parent.as_posix() for parent in path.parents if parent.as_posix() != ".")
                if len(paths) > 5000:
                    raise ValueError("ZIP exceeds expanded path count limit")
                mode = entry.external_attr >> 16
                if (entry.flag_bits & 1 or entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                        or stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}):
                    raise ValueError("Encrypted ZIP or special files are unsupported")
    except (zipfile.BadZipFile, UnicodeError):
        raise ValueError("Invalid source ZIP") from None


def _extract(archive, destination):
    try:
        return extract_project(archive, destination)
    except (zipfile.BadZipFile, RuntimeError, zlib.error):
        raise ValueError("Invalid source ZIP payload") from None


def _snapshot(project):
    if not project.is_dir() or project.is_symlink():
        raise ValueError("Source project must be a plain directory")
    entries = []
    for directory, directories, names in os.walk(project, followlinks=False):
        for name in sorted(directories + names):
            path = Path(directory) / name
            relative = _path(path.relative_to(project).as_posix())
            if path.is_symlink() or ignored_source_path(relative):
                raise ValueError("Source contains links or excluded files")
            entries.append(path)
            if len(entries) > 5000:
                raise ValueError("Source snapshot exceeds path count limit")
    files, total = [], 0
    for path in sorted(entries):
        relative = _path(path.relative_to(project).as_posix())
        if path.is_symlink():
            raise ValueError("Source symbolic links are unsupported")
        if ignored_source_path(relative):
            raise ValueError("Prepared source contains excluded files")
        mode = path.stat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValueError("Source special files are unsupported")
        total += path.stat().st_size
        files.append((path, relative.as_posix()))
        if len(files) > 5000 or total > MAX_EXTRACTED:
            raise ValueError("Source snapshot exceeds limits")
    before = source_digest(project)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as bundle:
        for path, name in files:
            item = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            item.external_attr = (stat.S_IFREG | 0o644) << 16
            with path.open("rb") as source, bundle.open(item, "w") as target:
                while part := source.read(1024 * 1024):
                    target.write(part)
                    if output.tell() > MAX_ARTIFACT_BYTES:
                        raise ValueError("Source snapshot exceeds archive limit")
    data = output.getvalue()
    if len(data) > MAX_ARTIFACT_BYTES or source_digest(project) != before:
        raise ValueError("Source changed during capture or exceeds limits")
    # Verify the bytes that will be stored, independently of the live input tree.
    with tempfile.TemporaryDirectory(prefix="sky-source-verify-") as temporary:
        archive = Path(temporary) / "source.zip"
        archive.write_bytes(data)
        restored = _extract(archive, Path(temporary) / "source")
        if source_digest(restored) != before:
            raise ValueError("Source changed during capture")
    return data, before


class SourceArtifactService:
    def __init__(self, store: SourceArtifactStore):
        self.store = store

    def capture_upload(self, principal, application_id, upload_id, upload):
        if not isinstance(principal, Principal):
            raise PermissionError("Source artifact access denied")
        _authorize(principal, principal.organization_id)
        if not isinstance(upload, bytes) or not 0 < len(upload) <= MAX_UPLOAD:
            raise ValueError("ZIP upload exceeds 20 MiB limit")
        # Validate identities before allocating/extracting an untrusted archive.
        SourceArtifact(principal.organization_id, application_id, upload_id, "original", "0" * 64, 1, "0" * 64)
        _validate_zip(upload)
        with tempfile.TemporaryDirectory(prefix="sky-source-upload-") as temporary:
            archive = Path(temporary) / "upload.zip"
            archive.write_bytes(upload)
            project = _extract(archive, Path(temporary) / "source")
            artifact = self._capture_project(principal, application_id, upload_id, project, kind="original")
        return CapturedSource(artifact, hashlib.sha256(upload).hexdigest())

    def capture_prepared(self, principal, original, project, *, expected_digest):
        if not isinstance(original, SourceArtifact) or original.kind != "original":
            raise ValueError("Prepared source requires an original artifact")
        _authorize(principal, original.organization_id)
        if not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
            raise ValueError("Prepared source requires an approved digest")
        return self._capture_project(principal, original.application_id, original.upload_id, project,
                                     kind="prepared", expected_digest=expected_digest)

    def _capture_project(self, principal, application_id, upload_id, project, *, kind, expected_digest=None):
        if not isinstance(principal, Principal):
            raise PermissionError("Source artifact access denied")
        _authorize(principal, principal.organization_id)
        SourceArtifact(principal.organization_id, application_id, upload_id, kind, "0" * 64, 1, "0" * 64)
        data, digest = _snapshot(Path(project))
        if expected_digest is not None and digest != expected_digest:
            raise ValueError("Source differs from the approved plan")
        artifact = SourceArtifact(principal.organization_id, application_id, upload_id, kind,
                                  hashlib.sha256(data).hexdigest(), len(data), digest)
        self.store.put(artifact, data)
        return artifact

    @contextmanager
    def restore(self, principal, artifact):
        if not isinstance(artifact, SourceArtifact):
            raise ValueError("Invalid source artifact")
        _authorize(principal, artifact.organization_id)
        data = self.store.get(artifact)
        if (not isinstance(data, bytes) or len(data) != artifact.size
                or hashlib.sha256(data).hexdigest() != artifact.sha256):
            raise ValueError("Source object size or digest mismatch")
        _validate_zip(data)
        with tempfile.TemporaryDirectory(prefix="sky-source-restore-") as temporary:
            archive = Path(temporary) / "source.zip"
            archive.write_bytes(data)
            project = _extract(archive, Path(temporary) / "project")
            if source_digest(project) != artifact.source_digest:
                raise ValueError("Restored source differs from the approved digest")
            yield project
