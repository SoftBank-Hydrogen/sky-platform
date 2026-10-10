"""Explicit B API, opt-in preparation/outbox publisher and one-shot migration."""

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
    ("admission", "sky_state.admission_schema_versions"),
    ("approval", "sky_state.approval_schema_versions"),
    ("preview", "sky_state.preview_schema_versions"),
)


def schema_versions(connection_factory):
    """Read every migration ledger without DDL; a missing ledger reports no versions."""
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


def migration_steps(connection_factory, workspace, *, account_id, region):
    """Initializers in ledger order. Construction validates the account/region without connecting."""
    from adapters.state.deployment_admission import PostgresDeploymentAdmission
    from adapters.state.deployment_approvals import PostgresDeploymentApprovals
    from adapters.state.deployment_previews import PostgresDeploymentPreviews
    from adapters.state.operations import PostgresOperationStore

    operations = PostgresOperationStore(connection_factory, workspace=workspace)
    admission = PostgresDeploymentAdmission(operations, account_id=account_id, region=region)
    approvals = PostgresDeploymentApprovals(admission)
    previews = PostgresDeploymentPreviews(approvals)
    return (operations.records, operations, admission, approvals, previews)


def run_migrations(connection_factory, workspace, *, account_id, region):
    """Apply metadata, operation, admission, approval then preview migrations.

    Each initializer re-runs its predecessors; those find their ledger current under the
    shared advisory lock and change nothing. Each step commits separately, so a rerun
    after a failure continues from the first incomplete ledger.
    """
    steps = migration_steps(connection_factory, workspace, account_id=account_id, region=region)
    before = schema_versions(connection_factory)
    for step in steps:
        step.initialize()
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
        "api", help="PostgreSQL API; preparation/admission requires explicit opt-in", allow_abbrev=False
    )
    api.add_argument("--host", default="0.0.0.0")
    api.add_argument("--port", type=int, default=8080)
    api.add_argument("--auth-mode", choices=("alb", "local"), default="alb")
    api.add_argument("--alb-trusts-file")
    api.add_argument("--memberships-file")
    api.add_argument(
        "--enable-preparation",
        action="store_true",
        help="Opt in to upload/preview/approval admission; no deployment consumer",
    )
    api.add_argument("--origin", help="Canonical HTTPS browser origin for preparation requests")
    worker = modes.add_parser(
        "worker", help="Outbox publisher; NOT a deployment consumer", allow_abbrev=False
    )
    worker.add_argument(
        "--mode",
        dest="worker_mode",
        choices=("outbox", "build"),
        required=True,
        help="Select outbox publisher or remote build consumer; neither executes ECS deployment",
    )
    worker.add_argument("--check-config", action="store_true", help="Validate settings without AWS/DB calls")
    worker.add_argument("--once", action="store_true", help="Publish one bounded batch and exit")
    worker.add_argument("--interval", type=int, default=5)
    modes.add_parser(
        "migrate",
        help="Apply pending PostgreSQL state migrations once and exit; safe to repeat",
        allow_abbrev=False,
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
            account_id = os.environ.get("SKY_AWS_ACCOUNT_ID", "").strip()
            PostgresDeploymentRecordStore(None, workspace=workspace)
            migration_steps(None, workspace, account_id=account_id, region=settings.region)
        except ValueError:
            parser.error("Invalid B migration database, account or workspace configuration")
        try:
            run_migrations(
                RotatingDatabaseConnection(settings), workspace, account_id=account_id, region=settings.region
            )
        except (OSError, ValueError, BotoCoreError, ClientError):
            # Do not log query diagnostics, credentials or DDL.
            parser.exit(
                1, "B state migration failed; check database access, schema version and configuration.\n"
            )
        return
    if args.mode == "api":
        if args.origin and not args.enable_preparation:
            parser.error("--origin requires --enable-preparation")
        if args.enable_preparation:
            if (
                args.auth_mode != "alb"
                or not (
                    (args.alb_trusts_file and args.memberships_file)
                    or all(name in os.environ for name in ("SKY_ALB_TRUSTS_JSON", "SKY_MEMBERSHIPS_JSON"))
                )
                or not args.origin
            ):
                parser.error(
                    "Preparation requires ALB authentication, trust/membership configuration and --origin"
                )
            from botocore.exceptions import BotoCoreError, ClientError

            from interfaces.b_preparation_runtime import run_preparation_api

            try:
                run_preparation_api(args)
            except (OSError, ValueError, TypeError, UnicodeError, BotoCoreError, ClientError):
                parser.exit(
                    1,
                    "Preparation API unavailable; check hosted authentication, schemas, S3 and database configuration.\n",
                )
            return
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
    if args.worker_mode == "build":
        from botocore.exceptions import BotoCoreError, ClientError

        from interfaces.b_build_runtime import run_build_consumer

        try:
            run_build_consumer(args)
        except (OSError, ValueError, TypeError, KeyError, BotoCoreError, ClientError):
            parser.exit(
                1,
                "Build consumer unavailable; check schemas, pinned builder configuration and credentials.\n",
            )
        return
    from botocore.exceptions import BotoCoreError, ClientError

    from adapters.aws.job_queue import SqsOperationQueue
    from adapters.state.operations import PostgresOperationStore
    from adapters.state.postgres import PostgresStateSettings, RotatingDatabaseConnection
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
