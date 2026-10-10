"""PostgreSQL metadata storage foundation; not a distributed job scheduler.

This adapter deliberately has no automatic runtime activation: existing App
startup recovery mutates running jobs and is unsafe across API replicas.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from ports.state import RecordConflict, StoredJob, StoredRecord


@dataclass(frozen=True)
class PostgresStateSettings:
    host: str
    port: int
    database: str
    secret_arn: str
    region: str
    sslrootcert: str

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None):
        values = os.environ if environment is None else environment
        names = ("SKY_DATABASE_HOST", "SKY_DATABASE_NAME", "SKY_DATABASE_SECRET_ARN", "SKY_AWS_REGION")
        if any(not values.get(name, "").strip() for name in names):
            raise ValueError("Incomplete Sky state database configuration")
        host, database, arn, region = (values[name].strip() for name in names)
        try:
            port = int(values.get("SKY_DATABASE_PORT", "5432"))
        except ValueError:
            raise ValueError("Invalid state database port") from None
        if not 1 <= port <= 65535 or not re.fullmatch(r"[A-Za-z0-9.-]+", host):
            raise ValueError("Invalid state database endpoint")
        if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", region):
            raise ValueError("Invalid state database region")
        if not re.fullmatch(rf"arn:aws:secretsmanager:{re.escape(region)}:\d{{12}}:secret:.+", arn):
            raise ValueError("Invalid state database secret ARN")
        ca = values.get("SKY_DATABASE_SSLROOTCERT", "/etc/ssl/certs/sky-rds-global-bundle.pem")
        if not Path(ca).is_file():
            raise ValueError("State database CA bundle is missing")
        return cls(host, port, database, arn, region, ca)


@dataclass(frozen=True)
class DatabaseCredentials:
    username: str = field(repr=False)
    password: str = field(repr=False)


class RotatingDatabaseConnection:
    """Fetch RDS-managed credentials, verify TLS, retry connection only once.

    Never retry a SQL statement/commit: its outcome may be uncertain. Refreshing
    credentials on a connection failure also covers libpq authentication failures
    which do not reliably expose SQLSTATE through the connect API.
    """

    def __init__(self, settings: PostgresStateSettings, *, secrets_client=None, connect=None):
        import psycopg

        self.settings = settings
        if secrets_client is None:
            import boto3

            secrets_client = boto3.client("secretsmanager", region_name=settings.region)
        self.secrets_client = secrets_client
        self.connect = connect or psycopg.connect
        self._credentials = None
        self._expires = 0.0
        self._lock = threading.Lock()

    def _read_credentials(self, *, refresh=False):
        with self._lock:
            if not refresh and self._credentials is not None and time.monotonic() < self._expires:
                return self._credentials
            try:
                response = self.secrets_client.get_secret_value(SecretId=self.settings.secret_arn)
                value = json.loads(response["SecretString"])
                username, password = value["username"], value["password"]
                if not all(
                    isinstance(item, str) and item and "\x00" not in item for item in (username, password)
                ):
                    raise ValueError("Invalid credentials")
            except Exception:
                raise OSError("Unable to read state database credentials") from None
            self._credentials = DatabaseCredentials(username, password)
            self._expires = time.monotonic() + 300
            return self._credentials

    def __call__(self):
        import psycopg

        settings = self.settings
        for attempt in range(2):
            credentials = self._read_credentials(refresh=bool(attempt))
            try:
                return self.connect(
                    host=settings.host,
                    port=settings.port,
                    dbname=settings.database,
                    user=credentials.username,
                    password=credentials.password,
                    sslmode="verify-full",
                    sslrootcert=settings.sslrootcert,
                    connect_timeout=10,
                    application_name="sky-state",
                    options="-c statement_timeout=30000 -c lock_timeout=10000",
                )
            except psycopg.OperationalError:
                if attempt:
                    raise OSError("Unable to connect to state database") from None
        raise AssertionError("Unreachable")


class PostgresDeploymentRecordStore:
    """JSONB compatibility projection scoped to a workspace.

    Each operation opens a short transaction; SQL errors are mapped to the port's
    OSError contract. initialize() is explicit, transactional, and serialized across
    replicas. Writes require explicit snapshot revisions; missing revisions are
    create-only. It supplies no leases and cannot activate the legacy App.
    """

    SCHEMA_VERSION = 2
    MIGRATION_LOCK = 1936419188

    def __init__(self, connection_factory, *, workspace="team"):
        if not isinstance(workspace, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", workspace):
            raise ValueError("Invalid state workspace")
        self.connection_factory = connection_factory
        self.workspace = workspace

    @contextmanager
    def _connection(self):
        import psycopg

        try:
            with self.connection_factory() as connection:
                yield connection
        except psycopg.Error:
            # Query diagnostics can contain the stored document or credentials.
            raise OSError("State database operation failed; outcome may be uncertain") from None

    def initialize(self):
        with self._connection() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (self.MIGRATION_LOCK,))
            connection.execute("CREATE SCHEMA IF NOT EXISTS sky_state")
            connection.execute("""CREATE TABLE IF NOT EXISTS sky_state.schema_versions (
                version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())""")
            versions = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT version FROM sky_state.schema_versions ORDER BY version"
                ).fetchall()
            )
            if versions not in ((), (1,), (1, 2)):
                raise ValueError("Unsupported state database schema version")
            if not versions:
                connection.execute("""CREATE TABLE sky_state.metadata_records (
                    workspace text NOT NULL,
                    kind text NOT NULL CHECK (kind IN ('job', 'health', 'github_sources')),
                    record_id text NOT NULL,
                    document jsonb NOT NULL CHECK (document <> 'null'::jsonb),
                    modified_at timestamptz NOT NULL DEFAULT clock_timestamp(),
                    PRIMARY KEY (workspace, kind, record_id))""")
                connection.execute("INSERT INTO sky_state.schema_versions (version) VALUES (1)")
            if 2 not in versions:
                connection.execute("""ALTER TABLE sky_state.metadata_records
                    ADD COLUMN revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0)""")
                connection.execute("INSERT INTO sky_state.schema_versions (version) VALUES (2)")

    @staticmethod
    def _identity(job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
            raise ValueError("Invalid job identity")
        return job_id

    def _load(self, kind, identity):
        with self._connection() as connection:
            return connection.execute(
                """SELECT document, modified_at, revision FROM sky_state.metadata_records
                WHERE workspace = %s AND kind = %s AND record_id = %s""",
                (self.workspace, kind, identity),
            ).fetchone()

    def _save(self, kind, identity, document, *, expected_revision=None):
        from psycopg.types.json import Jsonb

        if expected_revision is not None and (
            type(expected_revision) is not int
            or expected_revision <= 0
            or expected_revision >= 9223372036854775807
        ):
            raise ValueError("Invalid expected record revision")
        detached = json.loads(json.dumps(document, ensure_ascii=False, allow_nan=False))
        if detached is None:
            raise ValueError("Invalid null record")
        with self._connection() as connection:
            if expected_revision is None:
                row = connection.execute(
                    """INSERT INTO sky_state.metadata_records
                    (workspace, kind, record_id, document) VALUES (%s, %s, %s, %s)
                    ON CONFLICT (workspace, kind, record_id) DO NOTHING RETURNING revision""",
                    (self.workspace, kind, identity, Jsonb(detached)),
                ).fetchone()
            else:
                row = connection.execute(
                    """UPDATE sky_state.metadata_records
                    SET document = %s, modified_at = clock_timestamp(), revision = revision + 1
                    WHERE workspace = %s AND kind = %s AND record_id = %s AND revision = %s
                    RETURNING revision""",
                    (Jsonb(detached), self.workspace, kind, identity, expected_revision),
                ).fetchone()
            if row is None:
                raise RecordConflict("Record changed or no longer matches the expected revision")
            return row[0]

    def list_job_ids(self):
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT record_id FROM sky_state.metadata_records
                WHERE workspace = %s AND kind = 'job' ORDER BY record_id""",
                (self.workspace,),
            ).fetchall()
            return tuple(row[0] for row in rows)

    def load_job(self, job_id):
        row = self._load("job", self._identity(job_id))
        if row is None:
            raise FileNotFoundError("Job record not found")
        return StoredJob(row[0], row[1].isoformat(), row[2])

    def save_job(self, job_id, record, *, expected_revision=None):
        return self._save("job", self._identity(job_id), record, expected_revision=expected_revision)

    def load_health_record(self, job_id):
        row = self._load("health", self._identity(job_id))
        return StoredRecord(row[0], row[1].isoformat(), row[2]) if row else None

    def load_health(self, job_id):
        snapshot = self.load_health_record(job_id)
        return snapshot.record if snapshot else None

    def save_health(self, job_id, history, *, expected_revision=None):
        return self._save("health", self._identity(job_id), history, expected_revision=expected_revision)

    def load_github_sources_record(self):
        row = self._load("github_sources", "settings")
        return StoredRecord(row[0], row[1].isoformat(), row[2]) if row else None

    def load_github_sources(self):
        snapshot = self.load_github_sources_record()
        return snapshot.record if snapshot else None

    def save_github_sources(self, records, *, expected_revision=None):
        return self._save("github_sources", "settings", records, expected_revision=expected_revision)
