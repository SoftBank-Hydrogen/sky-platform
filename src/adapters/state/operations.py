"""Transactional admission, leases, app reservations and outbox for B workers.

No queue or AWS mutation happens in these transactions. Runtime activation and
reconciliation of uncertain external effects are separate subsequent stages.
"""

from __future__ import annotations

import hashlib
import json
import re
from uuid import UUID, uuid4

from adapters.state.postgres import PostgresDeploymentRecordStore
from ports.operations import (
    ApplicationBusy,
    ExecutionLease,
    FailedOutbox,
    IdempotencyConflict,
    Operation,
    OutboxDelivery,
)

KINDS = {
    "deploy",
    "rollback",
    "retire",
    "image_cleanup",
    "db_create",
    "db_retire",
    "snapshot_create",
    "restore_drill",
    "network_create",
    "github_poll",
    "external_poll",
    "reconcile",
    "observe",
}

DDL = (
    """CREATE TABLE sky_state.operations (
        workspace text NOT NULL, id uuid NOT NULL, application_id text NOT NULL,
        kind text NOT NULL, request_key text NOT NULL, request_hash text NOT NULL,
        command jsonb NOT NULL CHECK (jsonb_typeof(command) = 'object'),
        status text NOT NULL CHECK (status IN ('queued','running','succeeded','failed','needs_attention')),
        attempt_id uuid NOT NULL, generation bigint NOT NULL DEFAULT 1,
        row_version bigint NOT NULL DEFAULT 1,
        lease_owner text, lease_epoch bigint NOT NULL DEFAULT 0, lease_until timestamptz,
        checkpoint jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(checkpoint) = 'object'),
        result jsonb, external_pending boolean NOT NULL DEFAULT false,
        external_intent jsonb, external_receipt jsonb,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (workspace,id), UNIQUE (workspace,request_key),
        UNIQUE (workspace,application_id,id),
        CHECK ((status = 'running') = (lease_owner IS NOT NULL AND lease_until IS NOT NULL)))""",
    """CREATE INDEX operations_expired ON sky_state.operations (workspace,lease_until)
        WHERE status = 'running'""",
    """CREATE TABLE sky_state.mutation_scopes (
        workspace text NOT NULL, application_id text NOT NULL, operation_id uuid NOT NULL,
        PRIMARY KEY (workspace,application_id),
        FOREIGN KEY (workspace,application_id,operation_id)
            REFERENCES sky_state.operations (workspace,application_id,id))""",
    """CREATE TABLE sky_state.operation_events (
        workspace text NOT NULL, operation_id uuid NOT NULL, sequence bigint NOT NULL,
        stage text NOT NULL, occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (workspace,operation_id,sequence),
        FOREIGN KEY (workspace,operation_id) REFERENCES sky_state.operations (workspace,id))""",
    """CREATE TABLE sky_state.outbox_events (
        workspace text NOT NULL, id uuid NOT NULL, operation_id uuid NOT NULL,
        attempt_id uuid NOT NULL, application_id text NOT NULL, generation bigint NOT NULL,
        available_at timestamptz NOT NULL DEFAULT clock_timestamp(), published_at timestamptz,
        publisher_owner text, publisher_epoch bigint NOT NULL DEFAULT 0, publisher_until timestamptz,
        publish_attempts bigint NOT NULL DEFAULT 0,
        PRIMARY KEY (workspace,id), UNIQUE (workspace,operation_id,generation),
        FOREIGN KEY (workspace,application_id,operation_id)
            REFERENCES sky_state.operations (workspace,application_id,id))""",
    """CREATE INDEX outbox_pending ON sky_state.outbox_events (workspace,available_at)
        WHERE published_at IS NULL""",
)


class PostgresOperationStore:
    def __init__(
        self,
        connection_factory,
        *,
        workspace="team",
        outbox_max_attempts=8,
        outbox_retry_base=5,
        outbox_retry_cap=300,
    ):
        self.records = PostgresDeploymentRecordStore(connection_factory, workspace=workspace)
        self.workspace = workspace
        self.outbox_max_attempts = self._bounded(outbox_max_attempts, 1, 100)
        self.outbox_retry_base = self._bounded(outbox_retry_base, 0, 3600)
        self.outbox_retry_cap = self._bounded(outbox_retry_cap, self.outbox_retry_base, 3600)

    def initialize(self):
        self.records.initialize()
        with self.records._connection() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (self.records.MIGRATION_LOCK,))
            # Separate additive migration ledger keeps metadata readers compatible.
            connection.execute("""CREATE TABLE IF NOT EXISTS sky_state.operation_schema_versions (
                version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())""")
            versions = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT version FROM sky_state.operation_schema_versions ORDER BY version"
                ).fetchall()
            )
            if versions not in ((), (1,), (1, 2)):
                raise ValueError("Unsupported operation schema version")
            if not versions:
                for statement in DDL:
                    connection.execute(statement)
                connection.execute("INSERT INTO sky_state.operation_schema_versions (version) VALUES (1)")
            if 2 not in versions:
                connection.execute("""ALTER TABLE sky_state.outbox_events
                    ADD COLUMN max_attempts integer NOT NULL DEFAULT 8 CHECK (max_attempts BETWEEN 1 AND 100),
                    ADD COLUMN retry_base_seconds integer NOT NULL DEFAULT 5 CHECK (retry_base_seconds BETWEEN 0 AND 3600),
                    ADD COLUMN retry_cap_seconds integer NOT NULL DEFAULT 300 CHECK (retry_cap_seconds BETWEEN retry_base_seconds AND 3600),
                    ADD COLUMN failed_at timestamptz,
                    ADD COLUMN failure_code text CHECK (failure_code = 'retry_exhausted'),
                    ADD CONSTRAINT outbox_failure_consistent CHECK ((failed_at IS NULL) = (failure_code IS NULL))""")
                connection.execute("INSERT INTO sky_state.operation_schema_versions (version) VALUES (2)")

    @staticmethod
    def _text(value, label, *, maximum=128):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1," + str(maximum) + "}", value):
            raise ValueError(f"Invalid {label}")
        return value

    @staticmethod
    def _uuid(value):
        try:
            return str(UUID(value))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Invalid operation identity") from None

    @staticmethod
    def _bounded(value, low, high):
        if type(value) is not int or not low <= value <= high:
            raise ValueError("Invalid operation timing or batch size")
        return value

    @staticmethod
    def _document(value):
        if not isinstance(value, dict):
            raise ValueError("Operation document must be an object")
        try:
            encoded = json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
        except (TypeError, ValueError):
            raise ValueError("Invalid operation JSON document") from None
        if len(encoded.encode()) > 65536:
            raise ValueError("Operation document is too large; use an artifact reference")
        return json.loads(encoded)

    @staticmethod
    def _json(value):
        from psycopg.types.json import Jsonb

        return Jsonb(value)

    @staticmethod
    def _operation(row):
        return Operation(
            str(row[0]),
            row[1],
            row[2],
            row[3],
            str(row[4]),
            row[5],
            row[6],
            row[7],
            row[8],
            row[9],
            row[10],
            row[11],
        )

    def _select(self, connection, operation_id, *, lock=False):
        query = """SELECT id,application_id,kind,status,attempt_id,command,checkpoint,result,
            external_pending,row_version,external_intent,external_receipt FROM sky_state.operations WHERE workspace=%s AND id=%s"""
        if lock:
            query += " FOR UPDATE"
        row = connection.execute(query, (self.workspace, operation_id)).fetchone()
        if row is None:
            raise FileNotFoundError("Operation not found")
        return self._operation(row)

    def get(self, operation_id):
        operation_id = self._uuid(operation_id)
        with self.records._connection() as connection:
            return self._select(connection, operation_id)

    def _event(self, connection, operation_id, stage):
        # Caller owns the operation row lock (or its uncommitted INSERT).
        connection.execute(
            """INSERT INTO sky_state.operation_events (workspace,operation_id,sequence,stage)
            SELECT %s,%s,coalesce(max(sequence),0)+1,%s FROM sky_state.operation_events
            WHERE workspace=%s AND operation_id=%s""",
            (self.workspace, operation_id, stage, self.workspace, operation_id),
        )

    def _outbox(self, connection, operation_id):
        connection.execute(
            """INSERT INTO sky_state.outbox_events
            (workspace,id,operation_id,attempt_id,application_id,generation,max_attempts,retry_base_seconds,retry_cap_seconds)
            SELECT workspace,%s,id,attempt_id,application_id,generation,%s,%s,%s FROM sky_state.operations
            WHERE workspace=%s AND id=%s""",
            (
                str(uuid4()),
                self.outbox_max_attempts,
                self.outbox_retry_base,
                self.outbox_retry_cap,
                self.workspace,
                operation_id,
            ),
        )

    def admit(self, application_id, kind, request_key, command):
        application_id = self._text(application_id, "application identity")
        request_key = self._text(request_key, "request key")
        if not isinstance(kind, str) or kind not in KINDS:
            raise ValueError("Unsupported operation kind")
        command = self._document(command)
        with self.records._connection() as connection:
            return self._admit_in_transaction(connection, application_id, kind, request_key, command)

    def _admit_in_transaction(self, connection, application_id, kind, request_key, command):
        """Internal composition boundary; caller owns commit/rollback, never commits here."""
        application_id = self._text(application_id, "application identity")
        request_key = self._text(request_key, "request key")
        if not isinstance(kind, str) or kind not in KINDS:
            raise ValueError("Unsupported operation kind")
        command = self._document(command)
        request_hash = hashlib.sha256(
            json.dumps(
                [application_id, kind, command], ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        identity, attempt = str(uuid4()), str(uuid4())
        created = connection.execute(
            """INSERT INTO sky_state.operations
            (workspace,id,application_id,kind,request_key,request_hash,command,status,attempt_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,'queued',%s)
            ON CONFLICT (workspace,request_key) DO NOTHING RETURNING id""",
            (
                self.workspace,
                identity,
                application_id,
                kind,
                request_key,
                request_hash,
                self._json(command),
                attempt,
            ),
        ).fetchone()
        if created is None:
            existing = connection.execute(
                """SELECT id,request_hash FROM sky_state.operations
                WHERE workspace=%s AND request_key=%s""",
                (self.workspace, request_key),
            ).fetchone()
            if existing[1] != request_hash:
                raise IdempotencyConflict("Request key belongs to a different operation")
            return self._select(connection, existing[0])
        reserved = connection.execute(
            """INSERT INTO sky_state.mutation_scopes
            (workspace,application_id,operation_id) VALUES (%s,%s,%s)
            ON CONFLICT DO NOTHING RETURNING operation_id""",
            (self.workspace, application_id, identity),
        ).fetchone()
        if reserved is None:
            raise ApplicationBusy("Application has an unfinished operation")
        self._event(connection, identity, "queued")
        self._outbox(connection, identity)
        return self._select(connection, identity)

    def claim(self, operation_id, attempt_id, owner, *, seconds=90):
        operation_id, attempt_id = self._uuid(operation_id), self._uuid(attempt_id)
        owner = self._text(owner, "worker identity")
        seconds = self._bounded(seconds, 1, 3600)
        with self.records._connection() as connection:
            row = connection.execute(
                """UPDATE sky_state.operations SET status='running',lease_owner=%s,
                lease_epoch=lease_epoch+1,lease_until=clock_timestamp()+(%s * interval '1 second'),
                row_version=row_version+1 WHERE workspace=%s AND id=%s AND attempt_id=%s AND status='queued'
                RETURNING lease_epoch,lease_until""",
                (owner, seconds, self.workspace, operation_id, attempt_id),
            ).fetchone()
            if row is None:
                return None
            self._event(connection, operation_id, "claimed")
            return ExecutionLease(operation_id, attempt_id, owner, row[0], row[1], self.workspace)

    def _owned(self, connection, lease):
        if lease.workspace != self.workspace:
            return False
        row = connection.execute(
            """SELECT id FROM sky_state.operations WHERE workspace=%s AND id=%s
            AND attempt_id=%s AND status='running' AND lease_owner=%s AND lease_epoch=%s
            AND lease_until > clock_timestamp() FOR UPDATE""",
            (self.workspace, lease.operation_id, lease.attempt_id, lease.owner, lease.epoch),
        ).fetchone()
        if row is None:
            return False
        # Recheck after acquiring a contended lock; the lease may have expired while waiting.
        return connection.execute(
            """SELECT lease_until > clock_timestamp() FROM sky_state.operations
            WHERE workspace=%s AND id=%s""",
            (self.workspace, lease.operation_id),
        ).fetchone()[0]

    def heartbeat(self, lease, *, seconds=90):
        seconds = self._bounded(seconds, 1, 3600)
        with self.records._connection() as connection:
            if not self._owned(connection, lease):
                return False
            connection.execute(
                """UPDATE sky_state.operations SET
                lease_until=clock_timestamp()+(%s * interval '1 second'),row_version=row_version+1
                WHERE workspace=%s AND id=%s""",
                (seconds, self.workspace, lease.operation_id),
            )
            return True

    def checkpoint(self, lease, checkpoint):
        checkpoint = self._document(checkpoint)
        with self.records._connection() as connection:
            if not self._owned(connection, lease):
                return False
            connection.execute(
                """UPDATE sky_state.operations SET checkpoint=%s,row_version=row_version+1
                WHERE workspace=%s AND id=%s""",
                (self._json(checkpoint), self.workspace, lease.operation_id),
            )
            self._event(connection, lease.operation_id, "checkpoint")
            return True

    def begin_external(self, lease, intent):
        intent = self._document(intent)
        if not intent:
            raise ValueError("External request intent is required")
        with self.records._connection() as connection:
            if not self._owned(connection, lease):
                return False
            pending = connection.execute(
                """SELECT external_pending FROM sky_state.operations
                WHERE workspace=%s AND id=%s""",
                (self.workspace, lease.operation_id),
            ).fetchone()[0]
            if pending:
                raise ValueError("An external request already requires observation")
            connection.execute(
                """UPDATE sky_state.operations SET external_pending=true,
                external_intent=%s,external_receipt=NULL,row_version=row_version+1
                WHERE workspace=%s AND id=%s""",
                (self._json(intent), self.workspace, lease.operation_id),
            )
            self._event(connection, lease.operation_id, "external_intent")
            return True

    def observe_external(self, lease, receipt, checkpoint):
        receipt = self._document(receipt)
        checkpoint = self._document(checkpoint)
        if not receipt:
            raise ValueError("Verified external observation is required")
        with self.records._connection() as connection:
            if not self._owned(connection, lease):
                return False
            row = connection.execute(
                """UPDATE sky_state.operations SET external_pending=false,
                external_receipt=%s,checkpoint=%s,row_version=row_version+1 WHERE workspace=%s AND id=%s
                AND external_pending RETURNING id""",
                (self._json(receipt), self._json(checkpoint), self.workspace, lease.operation_id),
            ).fetchone()
            if row is None:
                raise ValueError("No external request awaiting observation")
            self._event(connection, lease.operation_id, "external_observed")
            return True

    def complete(self, lease, result, *, succeeded=True):
        result = self._document(result)
        if type(succeeded) is not bool:
            raise ValueError("Invalid completion status")
        with self.records._connection() as connection:
            if not self._owned(connection, lease):
                return False
            if self._select(connection, lease.operation_id).external_pending:
                raise ValueError("Observe the external request before completing")
            state = "succeeded" if succeeded else "failed"
            connection.execute(
                """UPDATE sky_state.operations SET status=%s,result=%s,
                lease_owner=NULL,lease_until=NULL,row_version=row_version+1 WHERE workspace=%s AND id=%s""",
                (state, self._json(result), self.workspace, lease.operation_id),
            )
            connection.execute(
                "DELETE FROM sky_state.mutation_scopes WHERE workspace=%s AND operation_id=%s",
                (self.workspace, lease.operation_id),
            )
            self._event(connection, lease.operation_id, state)
            return True

    def _resume_or_block(self, connection, operation):
        if operation.external_pending:
            connection.execute(
                """UPDATE sky_state.operations SET status='needs_attention',
                lease_owner=NULL,lease_until=NULL,row_version=row_version+1 WHERE workspace=%s AND id=%s""",
                (self.workspace, operation.id),
            )
            self._event(connection, operation.id, "needs_attention")
        else:
            # New execution attempt avoids SQS's five-minute dedup window when recovering.
            connection.execute(
                """UPDATE sky_state.operations SET status='queued',attempt_id=%s,
                generation=generation+1,lease_owner=NULL,lease_until=NULL,row_version=row_version+1
                WHERE workspace=%s AND id=%s""",
                (str(uuid4()), self.workspace, operation.id),
            )
            self._outbox(connection, operation.id)
            self._event(connection, operation.id, "requeued")

    def interrupt(self, lease, checkpoint):
        checkpoint = self._document(checkpoint)
        with self.records._connection() as connection:
            if not self._owned(connection, lease):
                return False
            connection.execute(
                "UPDATE sky_state.operations SET checkpoint=%s WHERE workspace=%s AND id=%s",
                (self._json(checkpoint), self.workspace, lease.operation_id),
            )
            self._resume_or_block(connection, self._select(connection, lease.operation_id))
            return True

    def recover_expired(self, *, limit=100):
        limit = self._bounded(limit, 1, 1000)
        with self.records._connection() as connection:
            rows = connection.execute(
                """SELECT id FROM sky_state.operations
                WHERE workspace=%s AND status='running' AND lease_until <= clock_timestamp()
                ORDER BY lease_until,id LIMIT %s FOR UPDATE SKIP LOCKED""",
                (self.workspace, limit),
            ).fetchall()
            for (identity,) in rows:
                self._resume_or_block(connection, self._select(connection, identity))
            return tuple(str(row[0]) for row in rows)

    def resolve_attention(
        self,
        operation_id,
        attempt_id,
        expected_version,
        expected_intent,
        receipt,
        checkpoint,
        *,
        resolver,
        outcome,
        result=None,
    ):
        """Commit an externally verified resolution against the original snapshot.

        The trusted caller must observe the original resource/request outside this
        transaction and establish that the old executor cannot issue late effects.
        This method performs no AWS calls and cannot verify arbitrary JSON evidence.
        Unknown or merely absent resources must remain blocked. Resume means start
        a new attempt at the verified checkpoint, never blindly repeat the intent.
        """
        operation_id, attempt_id = self._uuid(operation_id), self._uuid(attempt_id)
        resolver = self._text(resolver, "resolver identity")
        if type(expected_version) is not int or expected_version <= 0:
            raise ValueError("Invalid expected operation version")
        if not isinstance(outcome, str) or outcome not in {"succeeded", "failed", "resume"}:
            raise ValueError("Invalid reconciliation outcome")
        expected_intent = self._document(expected_intent)
        receipt, checkpoint = self._document(receipt), self._document(checkpoint)
        if not expected_intent or not receipt or not checkpoint:
            raise ValueError("Original intent, verified receipt and checkpoint are required")
        if outcome == "resume":
            if result is not None:
                raise ValueError("Resume cannot include a terminal result")
        else:
            result = self._document(result)
        evidence = self._document(
            {
                "observation": receipt,
                "resolution": {
                    "resolver": resolver,
                    "outcome": outcome,
                    "attempt_id": attempt_id,
                    "row_version": expected_version,
                },
            }
        )
        with self.records._connection() as connection:
            operation = self._select(connection, operation_id, lock=True)
            if (
                operation.status != "needs_attention"
                or not operation.external_pending
                or operation.attempt_id != attempt_id
                or operation.row_version != expected_version
                or operation.external_intent != expected_intent
            ):
                return False
            reserved = connection.execute(
                """SELECT operation_id FROM sky_state.mutation_scopes
                WHERE workspace=%s AND application_id=%s AND operation_id=%s FOR UPDATE""",
                (self.workspace, operation.application_id, operation_id),
            ).fetchone()
            if reserved is None:
                raise ValueError("Reconciliation requires the original application reservation")
            connection.execute(
                """UPDATE sky_state.operations SET external_pending=false,
                external_receipt=%s,checkpoint=%s,row_version=row_version+1
                WHERE workspace=%s AND id=%s""",
                (self._json(evidence), self._json(checkpoint), self.workspace, operation_id),
            )
            self._event(connection, operation_id, "reconciled_" + outcome)
            if outcome == "resume":
                self._resume_or_block(connection, self._select(connection, operation_id))
            else:
                connection.execute(
                    """UPDATE sky_state.operations SET status=%s,result=%s,
                    lease_owner=NULL,lease_until=NULL WHERE workspace=%s AND id=%s""",
                    (outcome, self._json(result), self.workspace, operation_id),
                )
                connection.execute(
                    "DELETE FROM sky_state.mutation_scopes WHERE workspace=%s AND operation_id=%s",
                    (self.workspace, operation_id),
                )
                self._event(connection, operation_id, outcome)
            return True

    def claim_outbox(self, owner, *, seconds=60, limit=10):
        owner = self._text(owner, "publisher identity")
        seconds, limit = self._bounded(seconds, 1, 3600), self._bounded(limit, 1, 100)
        with self.records._connection() as connection:
            # Final claims lost to process/DB failure still consume the durable budget.
            connection.execute(
                """WITH exhausted AS (
                SELECT id FROM sky_state.outbox_events WHERE workspace=%s
                AND published_at IS NULL AND failed_at IS NULL AND publish_attempts >= max_attempts
                AND (publisher_until IS NULL OR publisher_until <= clock_timestamp())
                ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED)
                UPDATE sky_state.outbox_events e SET failed_at=clock_timestamp(),
                failure_code='retry_exhausted',publisher_owner=NULL,publisher_until=NULL
                FROM exhausted WHERE e.workspace=%s AND e.id=exhausted.id""",
                (self.workspace, limit, self.workspace),
            )
            rows = connection.execute(
                """WITH pending AS (
                SELECT id FROM sky_state.outbox_events WHERE workspace=%s AND published_at IS NULL
                AND failed_at IS NULL AND publish_attempts < max_attempts
                AND available_at <= clock_timestamp()
                AND (publisher_until IS NULL OR publisher_until <= clock_timestamp())
                ORDER BY available_at,id LIMIT %s FOR UPDATE SKIP LOCKED)
                UPDATE sky_state.outbox_events e SET publisher_owner=%s,publisher_epoch=publisher_epoch+1,
                publisher_until=clock_timestamp()+(%s * interval '1 second'),
                available_at=clock_timestamp()+((%s + LEAST(retry_cap_seconds,
                    retry_base_seconds * power(2::numeric,LEAST(publish_attempts,30)))) * interval '1 second'),
                publish_attempts=publish_attempts+1
                FROM pending WHERE e.workspace=%s AND e.id=pending.id
                RETURNING e.id,e.publisher_epoch,e.operation_id,e.attempt_id,e.application_id""",
                (self.workspace, limit, owner, seconds, seconds, self.workspace),
            ).fetchall()
            return tuple(
                OutboxDelivery(str(r[0]), owner, r[1], str(r[2]), str(r[3]), r[4], self.workspace)
                for r in rows
            )

    def confirm_outbox(self, delivery):
        if delivery.workspace != self.workspace:
            return False
        with self.records._connection() as connection:
            return (
                connection.execute(
                    """UPDATE sky_state.outbox_events SET published_at=clock_timestamp(),
                publisher_owner=NULL,publisher_until=NULL WHERE workspace=%s AND id=%s AND published_at IS NULL
                AND failed_at IS NULL AND publisher_owner=%s AND publisher_epoch=%s
                AND publisher_until > clock_timestamp() RETURNING id""",
                    (self.workspace, delivery.id, delivery.owner, delivery.epoch),
                ).fetchone()
                is not None
            )

    def release_outbox(self, delivery, *, delay=None):
        # Optional explicit delay is for controlled recovery/tests; default is persisted policy.
        if delay is not None:
            delay = self._bounded(delay, 0, 3600)
        if delivery.workspace != self.workspace:
            return False
        with self.records._connection() as connection:
            return (
                connection.execute(
                    """UPDATE sky_state.outbox_events SET publisher_owner=NULL,publisher_until=NULL,
                available_at=clock_timestamp()+(COALESCE(%s,LEAST(retry_cap_seconds,
                    retry_base_seconds * power(2::numeric,LEAST(GREATEST(publish_attempts-1,0),30)))) * interval '1 second'),
                failed_at=CASE WHEN publish_attempts >= max_attempts THEN clock_timestamp() ELSE NULL END,
                failure_code=CASE WHEN publish_attempts >= max_attempts THEN 'retry_exhausted' ELSE NULL END
                WHERE workspace=%s AND id=%s AND published_at IS NULL AND failed_at IS NULL
                AND publisher_owner=%s AND publisher_epoch=%s AND publisher_until > clock_timestamp()
                RETURNING id""",
                    (delay, self.workspace, delivery.id, delivery.owner, delivery.epoch),
                ).fetchone()
                is not None
            )

    def list_failed_outbox(self, *, limit=100):
        limit = self._bounded(limit, 1, 1000)
        with self.records._connection() as connection:
            rows = connection.execute(
                """SELECT id,operation_id,attempt_id,application_id,publish_attempts,
                max_attempts,failure_code,failed_at FROM sky_state.outbox_events
                WHERE workspace=%s AND failed_at IS NOT NULL ORDER BY failed_at,id LIMIT %s""",
                (self.workspace, limit),
            ).fetchall()
            return tuple(
                FailedOutbox(str(r[0]), str(r[1]), str(r[2]), r[3], self.workspace, r[4], r[5], r[6], r[7])
                for r in rows
            )
