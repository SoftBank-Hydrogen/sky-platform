"""Owned, bounded PostgreSQL reads; each joined SELECT is one MVCC snapshot.

Schema initialization and authentication belong to the composition boundary.
No startup recovery, migration, cache, health probe or write runs here.
"""

import re
from contextlib import contextmanager

from domain.access import owner_from_record
from ports.deployment_reads import DeploymentSnapshot, ReadCursor, SnapshotPage

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_JOIN = """SELECT j.record_id, j.document, h.document, j.revision,
    COALESCE(j.document->>'created_at', '') FROM sky_state.metadata_records j
    LEFT JOIN sky_state.metadata_records h ON h.workspace=j.workspace
      AND h.kind='health' AND h.record_id=j.record_id
    WHERE j.workspace=%s AND j.kind='job'
      AND jsonb_typeof(j.document)='object'
      AND j.document->>'organization_id'=%s
      AND jsonb_typeof(j.document->'organization_id')='string'
      AND jsonb_typeof(j.document->'created_by')='string'
      AND j.document->>'created_by' ~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$'
"""


def _identifier(value):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("Invalid deployment read identifier")
    return value


def _snapshot(row):
    record_id, job, health, revision, _ = row
    if (
        owner_from_record(job) is None
        or job.get("id") != record_id
        or not isinstance(job.get("status"), str)
        or not isinstance(job.get("created_at", ""), str)
        or len(job.get("created_at", "")) > 128
        or (job.get("plan") is not None and not isinstance(job["plan"], dict))
        or (
            health is not None
            and (not isinstance(health, list) or any(not isinstance(item, dict) for item in health))
        )
    ):
        raise ValueError("Invalid persisted deployment read record")
    return DeploymentSnapshot(job, health if health is not None else [], revision)


class PostgresDeploymentReads:
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
                connection.execute("SET TRANSACTION READ ONLY")
                yield connection
        except psycopg.Error:
            raise OSError("Deployment database read failed") from None

    def detail(self, organization_id, job_id):
        _identifier(organization_id)
        _identifier(job_id)
        with self._connection() as connection:
            row = connection.execute(
                _JOIN + " AND j.record_id=%s", (self.workspace, organization_id, job_id)
            ).fetchone()
        return _snapshot(row) if row else None

    def page(self, organization_id, *, application_id=None, limit=50, cursor=None):
        _identifier(organization_id)
        if application_id is not None:
            _identifier(application_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Deployment page limit must be between 1 and 100")
        query, parameters = _JOIN, [self.workspace, organization_id]
        if application_id is not None:
            query += " AND COALESCE(j.document->>'application_id', j.record_id)=%s"
            parameters.append(application_id)
        if cursor is not None:
            if (
                not isinstance(cursor, ReadCursor)
                or cursor.organization_id != organization_id
                or cursor.application_id != application_id
                or not isinstance(cursor.created_at, str)
                or len(cursor.created_at) > 128
            ):
                raise ValueError("Invalid deployment page cursor")
            _identifier(cursor.record_id)
            query += " AND (COALESCE(j.document->>'created_at', ''), j.record_id) < (%s, %s)"
            parameters.extend((cursor.created_at, cursor.record_id))
        query += " ORDER BY COALESCE(j.document->>'created_at', '') DESC, j.record_id DESC LIMIT %s"
        parameters.append(limit + 1)
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        items = tuple(_snapshot(row) for row in rows[:limit])
        next_cursor = (
            ReadCursor(organization_id, application_id, rows[limit - 1][4], rows[limit - 1][0])
            if len(rows) > limit
            else None
        )
        return SnapshotPage(items, next_cursor)
