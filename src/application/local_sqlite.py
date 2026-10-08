"""Explicit, bounded SQLite storage contract for one local Docker container."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path, PurePosixPath

from engine.compatibility import SQLITE_FILES, InfrastructureProfile

_WORKDIR = re.compile(r"(?im)^\s*WORKDIR\s+(/[^\s#]+)\s*(?:#.*)?$")
_APP_ID = re.compile(r"[a-z][a-z0-9-]{2,30}\Z")
_MAX_SEED = 2 * 1024 * 1024


def preflight_local_sqlite(
    project: Path, profile: InfrastructureProfile, application_id: str, mount_path: str
) -> dict:
    """Resolve a user-confirmed image path before creating a persistent volume.

    The database location is not inferred from arbitrary application code. The
    user confirms it, and Sky checks it against the uploaded DB and Dockerfile.
    """
    if not _APP_ID.fullmatch(application_id):
        raise ValueError("SQLite 볼륨에 사용할 앱 ID가 올바르지 않습니다.")
    if "sqlite" not in profile.requirements or set(profile.requirements) - {"sqlite"}:
        raise ValueError("Local SQLite 볼륨은 단일 SQLite DB 외의 영속·워커 요구를 지원하지 않습니다.")
    files = sorted(
        path for path in project.rglob("*") if path.is_file() and path.suffix.lower() in SQLITE_FILES
    )
    if len(files) != 1 or files[0].is_symlink():
        raise ValueError("Local SQLite 볼륨은 ZIP의 데이터베이스 파일 하나만 지원합니다.")
    database = files[0]
    if database.stat().st_size > _MAX_SEED or database.stat().st_size < 100:
        raise ValueError("Local SQLite 초기 파일은 2 MiB 이하의 유효한 DB여야 합니다.")
    if any(database.with_name(database.name + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("SQLite WAL·저널 파일이 있는 ZIP은 일관된 스냅샷으로 다시 업로드하세요.")
    dockerfile = project / "Dockerfile"
    if not dockerfile.is_file():
        raise ValueError("Local SQLite 볼륨에는 절대 WORKDIR가 있는 Dockerfile이 필요합니다.")
    workdirs = _WORKDIR.findall(dockerfile.read_text(encoding="utf-8"))
    if not workdirs:
        raise ValueError("Dockerfile에 절대 WORKDIR가 필요합니다.")
    workdir = PurePosixPath(workdirs[-1])
    relative = database.relative_to(project)
    if relative.parent == Path(".") or not relative.parent.parts:
        raise ValueError("SQLite DB는 앱 루트가 아닌 전용 하위 디렉터리에 두세요.")
    expected_mount = (workdir / PurePosixPath(relative.parent.as_posix())).as_posix()
    selected = PurePosixPath(mount_path)
    if (
        not mount_path.startswith("/")
        or ".." in selected.parts
        or selected.as_posix() != mount_path
        or mount_path != expected_mount
    ):
        raise ValueError(f"볼륨 위치는 Dockerfile WORKDIR와 DB 경로에 맞는 {expected_mount}이어야 합니다.")
    with database.open("rb") as source:
        if source.read(16) != b"SQLite format 3\x00":
            raise ValueError("업로드한 DB 파일이 SQLite 형식이 아닙니다.")
    try:
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("SQLite DB 무결성 검사를 통과하지 못했습니다.")
    except sqlite3.DatabaseError:
        raise ValueError("SQLite DB를 읽거나 무결성을 확인할 수 없습니다.") from None
    return {
        "version": 1,
        "application_id": application_id,
        "volume_name": "sky-data-" + application_id,
        "source_path": relative.as_posix(),
        "source_sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
        "mount_path": mount_path,
        "lifecycle": "preserve",
    }
