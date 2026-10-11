"""Explicit or queued dedicated preparation; no data copy or application cutover."""

import json
import os
import signal
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from adapters.aws.dedicated_database import AwsDedicatedDatabasePreparer, DedicatedDatabaseSettings
from adapters.aws.job_queue import SqsOperationQueue
from adapters.state.operations import PostgresOperationStore
from adapters.state.postgres import (
    PostgresDeploymentRecordStore,
    PostgresStateSettings,
    RotatingDatabaseConnection,
)
from adapters.state.readiness import check_database_ready
from application.database_promotion_workflow import KIND, PromotionPreparationWorker
from application.shared_database_consumer import SharedDatabaseConsumer
from interfaces.operating_review import load_operating_policies
from interfaces.shared_database_worker import (
    current_membership,
    load_hosted_identity,
    load_pool_configuration,
)


def load_dedicated_configuration(path, source):
    value = os.environ.get("SKY_DEDICATED_DATABASE_CONFIG_JSON")
    if value is not None:
        if path:
            raise ValueError("Use dedicated configuration file or environment, not both")
        data = value.encode()
    elif path:
        with Path(path).open("rb") as stream:
            data = stream.read(16385)
    else:
        raise ValueError("Explicit dedicated configuration required")
    if len(data) > 16384:
        raise ValueError("Dedicated configuration exceeds size limit")

    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("Duplicate configuration field")
            result[key] = item
        return result

    document = json.loads(data, object_pairs_hook=unique)
    if (
        not isinstance(document, dict)
        or set(document) != {"version", "settings"}
        or type(document["version"]) is not int
        or document["version"] != 1
        or not isinstance(document["settings"], dict)
        or "source" in document["settings"]
    ):
        raise ValueError("Invalid dedicated registration")
    return DedicatedDatabaseSettings(source=source, **document["settings"])


def run_dedicated_database(args, parser):
    try:
        if args.initialize_pool:
            raise ValueError("Dedicated preparation cannot initialize or mutate the pool")
        state = PostgresStateSettings.from_environment()
        source, _ = load_pool_configuration(args.pool_config)
        if (
            source.pool.account_id != os.environ.get("SKY_AWS_ACCOUNT_ID")
            or source.pool.region != state.region
            or source.pool.control_database == state.database
        ):
            raise ValueError("Dedicated and state registration mismatch")
        settings = load_dedicated_configuration(args.dedicated_config, source)
        policies = load_operating_policies(
            args.operating_review_policy_config, account_id=source.pool.account_id, region=state.region
        )
        if any(policy.target_configuration_digest != settings.digest for policy in policies.values()):
            raise ValueError("Policy capacity/network digest differs from runtime registration")
        identity = load_hosted_identity(args)
        workspace = os.environ.get("SKY_STATE_WORKSPACE", "team")
        PostgresOperationStore(None, workspace=workspace)
        queued = args.worker_mode == "dedicated-database-queue"
        protection = None
        if queued:
            url = os.environ.get("SKY_DEDICATED_DATABASE_QUEUE_URL", "")
            SqsOperationQueue(url, region=state.region, account_id=source.pool.account_id, client=object())
            if url in {os.environ.get("SKY_JOB_QUEUE_URL"), os.environ.get("SKY_SHARED_DATABASE_QUEUE_URL")}:
                raise ValueError("Dedicated queue must be separate")
            if args.operation_id or args.attempt_id:
                raise ValueError("Queue mode cannot use explicit operation IDs")
            protection_mode = os.environ.get("SKY_DEDICATED_TASK_PROTECTION", "disabled")
            if protection_mode not in {"required", "disabled"}:
                raise ValueError("Invalid dedicated protection mode")
            if os.environ.get("ECS_CONTAINER_METADATA_URI_V4") and protection_mode != "required":
                raise ValueError("Hosted dedicated consumer requires task protection")
            if protection_mode == "required":
                from adapters.aws.task_protection import EcsTaskProtection

                protection = EcsTaskProtection()
        elif not args.check_config:
            if any(not value or str(UUID(value)) != value for value in (args.operation_id, args.attempt_id)):
                raise ValueError("One canonical operation and attempt required")
    except (ValueError, TypeError, KeyError, OSError):
        parser.error("Invalid dedicated preparation configuration or identity")
    if args.check_config:
        print("Configuration valid: dedicated target preparation; no AWS/DB calls", flush=True)
        return
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        connection = RotatingDatabaseConnection(state)
        check_database_ready(connection, operations=True)
        records = PostgresDeploymentRecordStore(connection, workspace=workspace)
        operations = PostgresOperationStore(connection, workspace=workspace)
        worker = PromotionPreparationWorker(
            records,
            operations,
            policies,
            AwsDedicatedDatabasePreparer(settings),
            lambda user, org: current_membership(identity, user, org),
            owner="dedicated-db-" + uuid4().hex,
            clock=lambda: datetime.now(UTC).isoformat(),
        )
        if not queued:
            result = worker.execute(args.operation_id, args.attempt_id)
            print("Dedicated preparation: " + result["status"] + "; data/routing unchanged", flush=True)
            if result["status"] != "succeeded":
                parser.exit(1, "Dedicated preparation requires review.\n")
            return
        consumer = SharedDatabaseConsumer(
            operations,
            SqsOperationQueue(url, region=state.region, account_id=source.pool.account_id),
            worker,
            protection=protection,
            kind=KIND,
        )
        stop = threading.Event()
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            for sig in previous:
                signal.signal(sig, lambda *_: stop.set())
            while not stop.is_set():
                try:
                    outcome = consumer.consume_once()
                except OSError:
                    outcome = "unavailable"
                print("Dedicated preparation consumer: " + outcome, flush=True)
                if args.once or stop.wait(args.interval):
                    break
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    except (ValueError, TypeError, KeyError, PermissionError, OSError, BotoCoreError, ClientError):
        parser.exit(1, "Dedicated preparation unavailable; inspect durable operation state.\n")
