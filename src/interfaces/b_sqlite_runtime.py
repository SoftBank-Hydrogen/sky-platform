"""Explicit SQLite preparation publisher/consumer composition; default stays off."""

import os
import signal
import threading
from uuid import uuid4

from adapters.aws.job_queue import SqsOperationQueue
from adapters.aws.source_artifacts import S3ArtifactSettings, S3SourceArtifactStore
from adapters.state.operations import PostgresOperationStore
from adapters.state.postgres import PostgresStateSettings, RotatingDatabaseConnection
from adapters.state.readiness import check_database_ready
from application.outbox import OutboxPublisher
from application.source_artifacts import SourceArtifactService
from application.sqlite_preparation import SqliteSourcePreparation
from application.sqlite_preparation_consumer import SqlitePreparationConsumer


def preparation_scope(env, artifacts):
    workspace, url = env.get("SKY_PREPARATION_WORKSPACE", ""), env.get("SKY_PREPARATION_QUEUE_URL", "")
    base_workspace, base_url = env.get("SKY_STATE_WORKSPACE", "team"), env.get("SKY_JOB_QUEUE_URL", "")
    PostgresOperationStore(None, workspace=workspace)
    SqsOperationQueue(url, region=artifacts.region, account_id=artifacts.account_id, client=object())
    SqsOperationQueue(base_url, region=artifacts.region, account_id=artifacts.account_id, client=object())
    if workspace == base_workspace or url == base_url:
        raise ValueError("Preparation workspace and queue must be separate from builds")
    return workspace, url


def run_sqlite_worker(args):
    from adapters.aws.task_protection import EcsTaskProtection
    from interfaces.b_runtime import run_outbox

    database = PostgresStateSettings.from_environment()
    artifacts = S3ArtifactSettings.from_environment()
    workspace, url = preparation_scope(os.environ, artifacts)
    protection = EcsTaskProtection() if args.worker_mode == "prepare-sqlite" else None
    if args.check_config:
        print("Configuration valid: isolated SQLite preparation; no AWS/DB calls", flush=True)
        return
    connection = RotatingDatabaseConnection(database)
    check_database_ready(connection, operations=True)
    store = PostgresOperationStore(connection, workspace=workspace)
    queue = SqsOperationQueue(url, region=artifacts.region, account_id=artifacts.account_id)
    stop = threading.Event()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        for sig in previous:
            signal.signal(sig, lambda *_: stop.set())
        if args.worker_mode == "preparation-outbox":
            run_outbox(
                OutboxPublisher(store, queue, "preparation-outbox-" + uuid4().hex),
                stop,
                interval=args.interval,
                once=args.once,
            )
        else:
            consumer = SqlitePreparationConsumer(
                store,
                queue,
                SqliteSourcePreparation(SourceArtifactService(S3SourceArtifactStore(artifacts))),
                protection,
                "sqlite-preparation-" + uuid4().hex,
            )
            while not stop.is_set():
                print("SQLite preparation: " + consumer.consume_once(stop), flush=True)
                if args.once or stop.wait(args.interval):
                    return
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
