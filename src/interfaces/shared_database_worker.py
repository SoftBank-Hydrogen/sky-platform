"""Explicit or queued workload DB allocation; never deploys the application."""

import hashlib
import json
import os
import signal
import threading
from dataclasses import asdict
from pathlib import Path
from uuid import UUID, uuid4

from adapters.aws.shared_database import (
    AwsSharedDatabaseAllocator,
    AwsSharedPoolSettings,
)
from adapters.state.operations import PostgresOperationStore
from adapters.state.postgres import (
    PostgresDeploymentRecordStore,
    PostgresStateSettings,
    RotatingDatabaseConnection,
)
from adapters.state.readiness import check_database_ready
from application.shared_database_workflow import SharedDatabaseWorker
from domain.access import Principal
from domain.shared_database import SharedDatabasePool
from interfaces.http.alb_identity import AlbRequestAuthenticator


def load_pool_configuration(path):
    env = os.environ.get("SKY_SHARED_DATABASE_POOL_JSON")
    if env is not None:
        if path:
            raise ValueError("Use pool file or environment registration, not both")
        data = env.encode()
    elif path:
        with Path(path).open("rb") as source:
            data = source.read(65537)
    else:
        raise ValueError("Explicit pool registration required")
    if len(data) > 65536:
        raise ValueError("Pool configuration exceeds 64 KiB")
    value = json.loads(data)
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "settings"}
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise ValueError("Invalid registered pool configuration")
    values = value["settings"]
    if not isinstance(values, dict):
        raise TypeError("Invalid registered pool settings")
    values = dict(values)
    if not isinstance(values.get("allowed_client_groups"), list):
        raise TypeError("Client security groups must be a list")
    values["allowed_client_groups"] = tuple(values["allowed_client_groups"])
    values["pool"] = SharedDatabasePool(**values["pool"])
    settings = AwsSharedPoolSettings(**values)
    ca = Path(settings.sslrootcert).read_bytes()
    digest = hashlib.sha256(
        json.dumps(
            {"settings": asdict(settings), "ca_sha256": hashlib.sha256(ca).hexdigest()},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return settings, digest


def current_membership(authenticator, user_id, organization_id):
    principals = {
        Principal(*record, trust.login_source)
        for (issuer, _), record in authenticator.memberships.records.items()
        for trust in authenticator.trusts
        if trust.issuer == issuer and record[:2] == (user_id, organization_id)
    }
    if len(principals) != 1:
        raise PermissionError("Current worker membership is missing or ambiguous")
    return principals.pop()


def load_hosted_identity(args):
    """Files or ECS-injected documents use the same explicit trust/membership rules."""
    values = [os.environ.get(name) for name in ("SKY_ALB_TRUSTS_JSON", "SKY_MEMBERSHIPS_JSON")]
    if any(value is not None for value in values):
        if None in values or args.alb_trusts_file or args.memberships_file:
            raise ValueError("Use complete identity files or environment documents, not both")
        return AlbRequestAuthenticator.from_json(*values)
    if not args.alb_trusts_file or not args.memberships_file:
        raise ValueError("Complete hosted identity files required")
    return AlbRequestAuthenticator.from_files(Path(args.alb_trusts_file), Path(args.memberships_file))


def run_shared_database(args, parser):
    try:
        if not args.pool_config and "SKY_SHARED_DATABASE_POOL_JSON" not in os.environ:
            raise ValueError("Explicit registered pool and current membership configuration are required")
        state = PostgresStateSettings.from_environment()
        settings, digest = load_pool_configuration(args.pool_config)
        if (
            settings.pool.account_id != os.environ.get("SKY_AWS_ACCOUNT_ID")
            or settings.pool.region != state.region
            or settings.pool.control_database == state.database
        ):
            raise ValueError("Workload and state settings must be separate in the registered account/region")
        authenticator = load_hosted_identity(args)
        workspace = os.environ.get("SKY_STATE_WORKSPACE", "team")
        PostgresOperationStore(None, workspace=workspace)
        queued = args.worker_mode == "shared-database-queue"
        if args.initialize_pool and (queued or args.operation_id or args.attempt_id):
            raise ValueError("Pool initialization must be explicit and separate from queued allocations")
        protection = None
        if queued:
            from adapters.aws.job_queue import SqsOperationQueue

            protection_mode = os.environ.get("SKY_ALLOCATION_TASK_PROTECTION", "disabled")
            if protection_mode not in {"required", "disabled"}:
                raise ValueError("Invalid allocation protection mode")
            if protection_mode == "required":
                from adapters.aws.task_protection import EcsTaskProtection

                protection = EcsTaskProtection()

            queue_url = os.environ.get("SKY_SHARED_DATABASE_QUEUE_URL", "")
            SqsOperationQueue(
                queue_url, region=state.region, account_id=settings.pool.account_id, client=object()
            )
            if queue_url == os.environ.get("SKY_JOB_QUEUE_URL"):
                raise ValueError("Allocation and build queues must be separate")
            if args.operation_id or args.attempt_id or args.interval < 1:
                raise ValueError(
                    "Queue mode requires a dedicated queue and positive interval, not operation IDs"
                )
        if not args.check_config and not queued and not args.initialize_pool:
            if not args.operation_id or not args.attempt_id:
                raise ValueError("One operation and attempt ID are required")
            UUID(args.operation_id)
            UUID(args.attempt_id)
    except (ValueError, TypeError, KeyError, OSError):
        parser.error("Invalid shared database worker configuration or identity files")
    if args.check_config:
        print("Configuration valid: shared database allocation only; no AWS/DB calls", flush=True)
        return
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        connection = RotatingDatabaseConnection(state)
        check_database_ready(connection, operations=True)
        records = PostgresDeploymentRecordStore(connection, workspace=workspace)
        operations = PostgresOperationStore(connection, workspace=workspace)
        allocator = AwsSharedDatabaseAllocator(settings)
        if args.initialize_pool:
            allocator.initialize()
            print("Dedicated workload pool registered; no app allocation or deployment", flush=True)
            return
        worker = SharedDatabaseWorker(
            records,
            operations,
            settings.pool,
            digest,
            allocator,
            lambda user, org: current_membership(authenticator, user, org),
            owner="shared-db-" + uuid4().hex,
        )
        if queued:
            from application.shared_database_consumer import SharedDatabaseConsumer

            consumer = SharedDatabaseConsumer(
                operations,
                SqsOperationQueue(queue_url, region=state.region, account_id=settings.pool.account_id),
                worker,
                protection=protection,
            )
            stop = threading.Event()
            previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
            try:
                for sig in previous:
                    signal.signal(sig, lambda *_: stop.set())
                while not stop.is_set():
                    try:
                        report = consumer.consume_once()
                    except OSError:
                        report = "unavailable"
                    print("Shared database consumer: " + report, flush=True)
                    if args.once or stop.wait(args.interval):
                        break
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
            return
        result = worker.execute(args.operation_id, args.attempt_id)
        print(
            f"Shared database operation={result['operation_id']} status={result['status']}; app deployment not started",
            flush=True,
        )
        if result["status"] in {"failed", "needs_attention", "lease_lost", "interrupted"}:
            parser.exit(1, "Shared database allocation requires review; no automatic redeployment.\n")
    except (ValueError, PermissionError, OSError, TypeError, KeyError, BotoCoreError, ClientError):
        parser.exit(1, "Shared database worker unavailable; inspect durable operation state.\n")
