"""Prepare reviewed SQLite data in an isolated copy; never execute SQL or app code."""

import hashlib
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from adapters.database.migrations import collect_sql_migrations
from adapters.database.sqlite_snapshot import compile_sqlite_snapshot
from application.deployment_core import source_digest
from ports.artifacts import SourceArtifact

MIGRATION_NAME = "0000_sky_sqlite_import.sql"


def migration_manifest(snapshot):
    return {"name": MIGRATION_NAME, "sha256": hashlib.sha256(snapshot.sql.encode("utf-8")).hexdigest()}


def _database_path(project, raw):
    if not isinstance(raw, str):
        raise ValueError("Invalid reviewed SQLite path")
    relative = PurePosixPath(raw)
    if (not raw or relative.is_absolute() or ".." in relative.parts
            or relative.as_posix() != raw or "\\" in raw):
        raise ValueError("Invalid reviewed SQLite path")
    path = project.joinpath(*relative.parts)
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("SQLite path contains a symbolic link")
    return path


def stage_sqlite_migration(original, work, conversion):
    """Reusable local/worker stage. Caller owns authorization and the isolated workspace.

    Replays validate the original snapshot, row counts and exact generated SQL.
    A prepared migration is not proof of PostgreSQL application compatibility.
    """
    original, work = Path(original), Path(work)
    if any(path.is_symlink() or not path.is_dir() for path in (original, work)):
        raise ValueError("SQLite preparation requires plain project directories")
    a, b = original.resolve(), work.resolve()
    if a == b or a in b.parents or b in a.parents:
        raise ValueError("SQLite preparation requires separate original and working copies")
    if not isinstance(conversion, dict):
        raise ValueError("Reviewed SQLite conversion required")
    source = _database_path(work, conversion.get("path"))
    snapshot = compile_sqlite_snapshot(_database_path(original, conversion.get("path")))
    if any(conversion.get(key) != getattr(snapshot, key) for key in ("source_sha256", "row_counts", "schema")):
        raise ValueError("SQLite snapshot differs from review")
    manifest = migration_manifest(snapshot)
    # Old local-agent reviews remain readable; worker preparation requires this field.
    if "migration" in conversion and conversion["migration"] != manifest:
        raise ValueError("SQLite migration differs from review")
    directory = work / "migrations"
    migration = directory / MIGRATION_NAME
    if directory.is_symlink() or migration.is_symlink():
        raise ValueError("Migration path contains a symbolic link")
    resumed = directory.exists() and not source.exists()
    if resumed:
        bundle = collect_sql_migrations(work)
        if (len(bundle.migrations) != 1 or bundle.migrations[0].name != MIGRATION_NAME
                or migration.read_bytes() != snapshot.sql.encode("utf-8")):
            raise ValueError("SQLite migration changed after preparation")
    else:
        if directory.exists() or not source.is_file():
            raise ValueError("Unexpected SQLite working copy or migration directory")
        current = compile_sqlite_snapshot(source)
        if current != snapshot:
            raise ValueError("Working SQLite snapshot differs from review")
        directory.mkdir(mode=0o700)
        try:
            with migration.open("x", encoding="utf-8") as output:
                output.write(snapshot.sql)
            bundle = collect_sql_migrations(work)
            source.unlink()
        except Exception:
            migration.unlink(missing_ok=True)
            directory.rmdir()
            raise
    return {"prepared": True, "resumed": resumed, "row_counts": snapshot.row_counts,
            "source_sha256": snapshot.source_sha256, "migration": manifest,
            "migration_digest": bundle.digest,
            "next": "앱의 SQLite 코드와 의존성을 PostgreSQL로 바꾸고 다시 설정하세요."}


class SqliteSourcePreparation:
    """Worker-callable artifact stage, separate from approved build/deployment execution.

    Restore a source-bound review, stage SQL in a temporary copy and save that copy.
    The result still requires application conversion, DB binding and a new review.
    """

    def __init__(self, sources):
        self.sources = sources

    def prepare(self, principal, original, conversion, *, expected_source_digest):
        if not isinstance(original, SourceArtifact) or original.kind != "original":
            raise ValueError("SQLite preparation requires an original artifact")
        if expected_source_digest != original.source_digest:
            raise ValueError("SQLite source differs from review")
        if not isinstance(conversion, dict) or "migration" not in conversion:
            raise ValueError("Reviewed SQLite migration manifest required")
        with self.sources.restore(principal, original) as project:
            with tempfile.TemporaryDirectory(prefix="sky-sqlite-preparation-") as temporary:
                work = Path(temporary) / "work"
                shutil.copytree(project, work)
                result = stage_sqlite_migration(project, work, conversion)
                prepared = self.sources.capture_prepared(
                    principal, original, work, expected_digest=source_digest(work))
        return {"original_ref": original.record(), "prepared_ref": prepared.record(),
                "conversion": result, "status": "application_conversion_required",
                "blockers": ["postgres_application_conversion_required", "database_binding_required"],
                "requires_new_review": True}
