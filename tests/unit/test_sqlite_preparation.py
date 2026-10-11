"""Source-bound SQLite preparation without deployment or operational DB access."""

import copy
import io
import os
import shutil
import sqlite3
import zipfile
from pathlib import Path

import pytest

from application.deployment_core import source_digest
from application.infrastructure import inspect_infrastructure, preflight_sqlite_conversion
from application.source_artifacts import SourceArtifactService
from application.sqlite_preparation import SqliteSourcePreparation, stage_sqlite_migration
from domain.access import LoginSource, Principal, Role


class Objects:
    def __init__(self):
        self.data = {}

    def put(self, artifact, data):
        if artifact.key in self.data:
            assert self.data[artifact.key] == data
        self.data[artifact.key] = data

    def get(self, artifact):
        return self.data[artifact.key]


@pytest.fixture
def projects(tmp_path):
    original = tmp_path / "original"
    original.mkdir()
    (original / "data").mkdir()
    (original / "server.js").write_text("require('node:sqlite');", encoding="utf-8")
    with sqlite3.connect(original / "data/scores.db") as connection:
        connection.execute("CREATE TABLE rounds(id INTEGER PRIMARY KEY AUTOINCREMENT, winner TEXT NOT NULL)")
        connection.executemany("INSERT INTO rounds(winner) VALUES(?)", [("A",), ("B",)])
    (original / "package.json").write_text('{"scripts":{"start":"node server.js"}}', encoding="utf-8")
    conversion, _ = preflight_sqlite_conversion(original, inspect_infrastructure(original))
    work = tmp_path / "work"
    shutil.copytree(original, work)
    return original, work, conversion


def capture(original):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for file in original.rglob("*"):
            if file.is_file():
                archive.write(file, file.relative_to(original).as_posix())
    sources = SourceArtifactService(Objects())
    principal = Principal("alice", "team", Role.DEPLOYER, LoginSource.CORPORATE_SSO)
    artifact = sources.capture_upload(principal, "game", "a" * 32, output.getvalue()).artifact
    return sources, principal, artifact


def test_stage_preserves_original_and_replays_only_identical_migration(projects):
    original, work, conversion = projects
    before = source_digest(original)
    result = stage_sqlite_migration(original, work, conversion)
    assert result["row_counts"] == {"rounds": 2}
    assert result["migration"] == conversion["migration"]
    assert result["resumed"] is False
    assert not (work / "data/scores.db").exists()
    assert source_digest(original) == before
    replay = stage_sqlite_migration(original, work, conversion)
    assert replay == {**result, "resumed": True}
    migration = work / "migrations/0000_sky_sqlite_import.sql"
    migration.write_text(migration.read_text() + "SELECT 1;", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        stage_sqlite_migration(original, work, conversion)


@pytest.mark.parametrize("field", ["source_sha256", "row_counts", "schema", "migration"])
def test_modified_review_rejected_before_workspace_mutation(projects, field):
    original, work, conversion = projects
    conversion = {**conversion, field: None}
    before = source_digest(work)
    with pytest.raises(ValueError, match="review"):
        stage_sqlite_migration(original, work, conversion)
    assert source_digest(work) == before


def test_replay_checks_row_counts_and_rejects_extra_migrations(projects):
    original, work, conversion = projects
    stage_sqlite_migration(original, work, conversion)
    with pytest.raises(ValueError, match="review"):
        stage_sqlite_migration(original, work, {**conversion, "row_counts": {"rounds": 0}})
    (work / "migrations/0001_extra.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        stage_sqlite_migration(original, work, conversion)


def test_same_workspace_and_symlink_parent_rejected(projects, tmp_path):
    original, work, conversion = projects
    with pytest.raises(ValueError, match="separate"):
        stage_sqlite_migration(original, original, conversion)
    shutil.rmtree(work / "data")
    (work / "data").symlink_to(original / "data", target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        stage_sqlite_migration(original, work, conversion)
    assert (original / "data/scores.db").exists()


def test_changed_working_snapshot_rejected(projects):
    original, work, conversion = projects
    with sqlite3.connect(work / "data/scores.db") as connection:
        connection.execute("INSERT INTO rounds(winner) VALUES('C')")
    with pytest.raises(ValueError, match="snapshot differs"):
        stage_sqlite_migration(original, work, conversion)
    assert not (work / "migrations").exists()


def test_workspace_parent_alias_allowed_but_project_root_link_rejected(projects, tmp_path):
    original, work, conversion = projects
    alias = tmp_path / "workspace-alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    result = stage_sqlite_migration(alias / original.name, alias / work.name, conversion)
    assert result["prepared"] is True
    assert (original / "data/scores.db").is_file()
    root_link = tmp_path / "project-link"
    root_link.symlink_to(original, target_is_directory=True)
    with pytest.raises(ValueError, match="plain project"):
        stage_sqlite_migration(root_link, work, conversion)


def test_artifact_stage_is_deterministic_and_remains_blocked(projects):
    original, _, conversion = projects
    sources, principal, artifact = capture(original)
    stage = SqliteSourcePreparation(sources)
    before = sources.store.get(artifact)
    first = stage.prepare(principal, artifact, conversion, expected_source_digest=artifact.source_digest)
    assert (
        stage.prepare(principal, artifact, conversion, expected_source_digest=artifact.source_digest) == first
    )
    assert first["status"] == "application_conversion_required" and first["requires_new_review"] is True
    assert first["blockers"] == ["postgres_application_conversion_required", "database_binding_required"]
    assert sources.store.get(artifact) == before and len(sources.store.data) == 2
    assert "winner" not in str(first)  # SQL data is not returned in worker status.
    from ports.artifacts import SourceArtifact

    prepared = SourceArtifact.from_record(first["prepared_ref"])
    with sources.restore(principal, prepared) as project:
        assert not (project / "data/scores.db").exists()
        assert (project / "migrations/0000_sky_sqlite_import.sql").is_file()
        assert (project / "server.js").read_bytes() == (original / "server.js").read_bytes()


def test_artifact_review_and_cross_organization_rejected(projects):
    original, _, conversion = projects
    sources, principal, artifact = capture(original)
    stage = SqliteSourcePreparation(sources)
    with pytest.raises(ValueError, match="source differs"):
        stage.prepare(principal, artifact, conversion, expected_source_digest="0" * 64)
    legacy = copy.deepcopy(conversion)
    del legacy["migration"]
    with pytest.raises(ValueError, match="manifest"):
        stage.prepare(principal, artifact, legacy, expected_source_digest=artifact.source_digest)
    outsider = Principal("alice", "other", Role.DEPLOYER, LoginSource.CORPORATE_SSO)
    with pytest.raises(PermissionError):
        stage.prepare(outsider, artifact, conversion, expected_source_digest=artifact.source_digest)
    assert len(sources.store.data) == 1


def test_generated_sql_imports_into_disposable_postgres(projects):
    psycopg = pytest.importorskip("psycopg")
    dsn = os.environ.get("SKY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Disposable PostgreSQL DSN required")
    if psycopg.conninfo.conninfo_to_dict(dsn).get("host") not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("Only loopback databases are accepted")
    original, work, conversion = projects
    stage_sqlite_migration(original, work, conversion)
    from uuid import uuid4

    schema = "sqlite_stage_" + uuid4().hex
    with psycopg.connect(dsn) as connection:
        try:
            connection.execute('CREATE SCHEMA "' + schema + '"')
            connection.execute('SET LOCAL search_path TO "' + schema + '"')
            connection.execute((work / "migrations/0000_sky_sqlite_import.sql").read_text())
            assert connection.execute("SELECT id,winner FROM rounds ORDER BY id").fetchall() == [
                (1, "A"),
                (2, "B"),
            ]
            assert connection.execute("INSERT INTO rounds(winner) VALUES('C') RETURNING id").fetchone() == (
                3,
            )
        finally:
            connection.rollback()  # Drop schema and all data even on assertion failure.


def test_actual_tug_artifact_stage_when_supplied():
    path = os.environ.get("SKY_TEST_GAME_ZIP")
    if not path:
        pytest.skip("Pinned demo-game ZIP supplied only for local rehearsal")
    sources = SourceArtifactService(Objects())
    principal = Principal("alice", "team", Role.DEPLOYER, LoginSource.CORPORATE_SSO)
    original = sources.capture_upload(principal, "tug", "b" * 32, Path(path).read_bytes()).artifact
    with sources.restore(principal, original) as project:
        conversion, _ = preflight_sqlite_conversion(project, inspect_infrastructure(project))
    result = SqliteSourcePreparation(sources).prepare(
        principal, original, conversion, expected_source_digest=original.source_digest
    )
    assert result["conversion"]["row_counts"] == {"rounds": 13}
    assert result["requires_new_review"] is True
