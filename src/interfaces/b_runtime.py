"""Explicit B modes: API, outbox, state migration and one shared DB allocation."""

import argparse
import os
import signal
import sys
import threading
from uuid import uuid4


def run_outbox(publisher, stop, *, interval=5, once=False):
    while not stop.is_set():
        report = publisher.dispatch_once(limit=1)
        print(f"Outbox confirmed={report.confirmed} deferred={report.deferred}", flush=True)
        if once or stop.wait(interval):
            return


_LEDGERS = (
    ("metadata", "sky_state.schema_versions"),
    ("operation", "sky_state.operation_schema_versions"),
)


def schema_versions(connection_factory):
    """Read both migration ledgers without DDL; a missing ledger reports no versions."""
    import psycopg

    try:
        with connection_factory() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            versions = {}
            for name, table in _LEDGERS:
                if connection.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is None:
                    versions[name] = ()
                else:
                    # Identifiers come from the fixed ledger list above, never from input.
                    rows = connection.execute(f"SELECT version FROM {table} ORDER BY version").fetchall()
                    versions[name] = tuple(row[0] for row in rows)
            return versions
    except psycopg.Error:
        raise OSError("State database schema versions are unavailable") from None


def run_migrations(connection_factory, workspace):
    """Apply metadata then operation migrations under the existing advisory lock."""
    from adapters.state.operations import PostgresOperationStore
    from adapters.state.postgres import PostgresDeploymentRecordStore

    before = schema_versions(connection_factory)
    PostgresDeploymentRecordStore(connection_factory, workspace=workspace).initialize()
    PostgresOperationStore(connection_factory, workspace=workspace).initialize()
    after = schema_versions(connection_factory)
    for name, _ in _LEDGERS:
        applied = [version for version in after[name] if version not in before[name]]
        current = ",".join(map(str, after[name]))
        state = "applied " + ",".join(map(str, applied)) if applied else "up to date"
        print(f"{name} schema: {state} (now {current})", flush=True)


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    modes = parser.add_subparsers(dest="mode", required=True)
    api = modes.add_parser(
        "api", help="Read-only PostgreSQL API; no deployment admission", allow_abbrev=False
    )
    api.add_argument("--host", default="0.0.0.0")
    api.add_argument("--port", type=int, default=8080)
    api.add_argument("--auth-mode", choices=("alb", "local"), default="alb")
    api.add_argument("--alb-trusts-file")
    api.add_argument("--memberships-file")
    api.add_argument("--shared-database-pool-config", help="Opt in to reviewed workload DB allocation intake")
    api.add_argument("--origin", help="Exact browser origin for shared database review and consent")
    api.add_argument(
        "--check-config", action="store_true", help="Validate shared intake without AWS/DB calls"
    )
    worker = modes.add_parser(
        "worker", help="Explicit outbox or single shared database allocation", allow_abbrev=False
    )
    worker.add_argument(
        "--mode",
        dest="worker_mode",
        choices=("outbox", "shared-database"),
        required=True,
        help="Select outbox publishing or one shared DB allocation; no app deployment consumer",
    )
    worker.add_argument("--pool-config")
    worker.add_argument("--alb-trusts-file")
    worker.add_argument("--memberships-file")
    worker.add_argument("--operation-id")
    worker.add_argument("--attempt-id")
    worker.add_argument("--check-config", action="store_true", help="Validate settings without AWS/DB calls")
    worker.add_argument("--once", action="store_true", help="Publish one bounded batch and exit")
    worker.add_argument("--interval", type=int, default=5)
    migrate = modes.add_parser(
        "migrate",
        help="Apply pending PostgreSQL state migrations once and exit; safe to repeat",
        allow_abbrev=False,
    )
    migrate.add_argument(
        "--shared-database-reviews",
        action="store_true",
        help="Explicitly migrate the opt-in shared DB review schema too",
    )
    args = parser.parse_args(argv)
    if args.mode == "migrate":
        from botocore.exceptions import BotoCoreError, ClientError

        from adapters.state.postgres import (
            PostgresDeploymentRecordStore,
            PostgresStateSettings,
            RotatingDatabaseConnection,
        )

        try:
            settings = PostgresStateSettings.from_environment()
            workspace = os.environ.get("SKY_STATE_WORKSPACE", "team")
            PostgresDeploymentRecordStore(None, workspace=workspace)
        except ValueError:
            parser.error("Invalid B migration database or workspace configuration")
        try:
            run_migrations(RotatingDatabaseConnection(settings), workspace)
            if args.shared_database_reviews:
                from adapters.state.operations import PostgresOperationStore
                from adapters.state.shared_database_reviews import PostgresSharedDatabaseReviews

                PostgresSharedDatabaseReviews.initialize_schema(
                    PostgresOperationStore(RotatingDatabaseConnection(settings), workspace=workspace)
                )
                print("shared database review schema: up to date", flush=True)
        except (OSError, ValueError, BotoCoreError, ClientError):
            # Do not log query diagnostics, credentials or DDL.
            parser.exit(
                1, "B state migration failed; check database access, schema version and configuration.\n"
            )
        return
    if args.mode == "api":
        if args.shared_database_pool_config:
            from interfaces.shared_database_api import run_shared_database_api

            run_shared_database_api(args, parser)
            return
        if args.origin or args.check_config:
            parser.error("--origin and --check-config require --shared-database-pool-config")
        from interfaces.http.server import serve

        forwarded = [
            "--read-only-database",
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--auth-mode",
            args.auth_mode,
        ]
        for name in ("alb_trusts_file", "memberships_file"):
            value = getattr(args, name)
            if value:
                forwarded.extend(["--" + name.replace("_", "-"), value])
        previous = sys.argv
        try:
            sys.argv = [previous[0], *forwarded]
            serve(product_name="Sky")
        finally:
            sys.argv = previous
        return
    if not 1 <= args.interval <= 300:
        parser.error("--interval must be between 1 and 300 seconds")
    if args.worker_mode == "shared-database":
        from interfaces.shared_database_worker import run_shared_database

        run_shared_database(args, parser)
        return
    if any(
        (args.pool_config, args.alb_trusts_file, args.memberships_file, args.operation_id, args.attempt_id)
    ):
        parser.error("Shared database options require --mode shared-database")
    from botocore.exceptions import BotoCoreError, ClientError

    from adapters.aws.job_queue import SqsOperationQueue
    from adapters.state.operations import PostgresOperationStore
    from adapters.state.postgres import (
        PostgresStateSettings,
        RotatingDatabaseConnection,
    )
    from adapters.state.readiness import check_database_ready
    from application.outbox import OutboxPublisher

    try:
        settings = PostgresStateSettings.from_environment()
        account = os.environ.get("SKY_AWS_ACCOUNT_ID", "")
        url = os.environ.get("SKY_JOB_QUEUE_URL", "")
        # A sentinel avoids constructing a boto client during configuration validation.
        SqsOperationQueue(url, region=settings.region, account_id=account, client=object())
        workspace = os.environ.get("SKY_STATE_WORKSPACE", "team")
        PostgresOperationStore(None, workspace=workspace)
    except ValueError:
        parser.error("Invalid B worker database, workspace or queue configuration")
    if args.check_config:
        print("Configuration valid: outbox publisher only; no AWS/DB calls", flush=True)
        return
    try:
        connection = RotatingDatabaseConnection(settings)
        check_database_ready(connection, operations=True)
        store = PostgresOperationStore(connection, workspace=workspace)
        queue = SqsOperationQueue(url, region=settings.region, account_id=account)
        publisher = OutboxPublisher(store, queue, "outbox-" + uuid4().hex)
        stop = threading.Event()
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            for sig in previous:
                signal.signal(sig, lambda *_: stop.set())
            run_outbox(publisher, stop, interval=args.interval, once=args.once)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    except (OSError, ValueError, BotoCoreError, ClientError):
        # Do not log query diagnostics, commands, credentials or queue bodies.
        parser.exit(1, "B outbox publisher unavailable; check database schema, IAM and configuration.\n")
