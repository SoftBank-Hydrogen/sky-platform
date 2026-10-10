"""Explicit B entry points: read-only API, opt-in preparation and outbox publisher."""

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
        choices=("outbox",),
        required=True,
        help="Explicitly select outbox; deployment consumption is not implemented",
    )
    worker.add_argument("--check-config", action="store_true", help="Validate settings without AWS/DB calls")
    worker.add_argument("--once", action="store_true", help="Publish one bounded batch and exit")
    worker.add_argument("--interval", type=int, default=5)
    args = parser.parse_args(argv)
    if args.mode == "api":
        if args.origin and not args.enable_preparation:
            parser.error("--origin requires --enable-preparation")
        if args.enable_preparation:
            if (
                args.auth_mode != "alb"
                or not args.alb_trusts_file
                or not args.memberships_file
                or not args.origin
            ):
                parser.error("Preparation requires ALB authentication, trust/membership files and --origin")
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
