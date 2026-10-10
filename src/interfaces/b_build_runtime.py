"""Opt-in build/deployment consumer; task protection required; no Docker or runtime migrations."""

import os
import signal
import threading
from uuid import uuid4


def run_build_consumer(args):
    from adapters.aws.build_image import EcrBuildVerifier
    from adapters.aws.build_objects import S3BuildObjects
    from adapters.aws.job_queue import SqsOperationQueue
    from adapters.aws.source_artifacts import S3ArtifactSettings
    from adapters.aws.task_protection import EcsTaskProtection
    from adapters.github.remote_build import (
        GitHubApi,
        GitHubRemoteBuild,
        InstallationToken,
    )
    from adapters.state.build_execution import PostgresBuildExecutionStore
    from adapters.state.deployment_admission import PostgresDeploymentAdmission
    from adapters.state.deployment_approvals import PostgresDeploymentApprovals
    from adapters.state.postgres import (
        PostgresStateSettings,
        RotatingDatabaseConnection,
    )
    from application.build_consumer import BuildConsumer
    from ports.remote_builds import BuildSettings

    database = PostgresStateSettings.from_environment()
    settings = BuildSettings.from_environment(os.environ)
    workspace = os.environ.get("SKY_STATE_WORKSPACE", "team")
    queue_url = os.environ.get("SKY_JOB_QUEUE_URL", "")
    SqsOperationQueue(queue_url, region=settings.region, account_id=settings.account_id, client=object())
    PostgresBuildExecutionStore(None, workspace=workspace)
    protection = EcsTaskProtection()
    token = InstallationToken(
        os.environ.get("SKY_GITHUB_APP_ID", ""),
        os.environ.get("SKY_GITHUB_INSTALLATION_ID", ""),
        os.environ.get("SKY_GITHUB_APP_PRIVATE_KEY", ""),
        settings.repository,
    )
    if args.check_config:
        print("Build consumer configuration valid; no AWS/DB/GitHub calls", flush=True)
        return
    connection = RotatingDatabaseConnection(database)
    store = PostgresBuildExecutionStore(connection, workspace=workspace)
    PostgresDeploymentApprovals(
        PostgresDeploymentAdmission(store, account_id=settings.account_id, region=settings.region)
    ).check_ready()
    artifacts = S3ArtifactSettings(settings.bucket, settings.region, settings.account_id)
    from adapters.aws.built_deployment import BuiltImageDeployment
    deployer = BuiltImageDeployment(settings) if getattr(args, "deploy_built_image", False) else None
    consumer = BuildConsumer(
        store,
        SqsOperationQueue(queue_url, region=settings.region, account_id=settings.account_id),
        GitHubRemoteBuild(settings, GitHubApi(token)),
        S3BuildObjects(artifacts),
        EcrBuildVerifier(settings),
        settings,
        protection,
        "build-" + uuid4().hex,
        deployer=deployer,
    )
    stop = threading.Event()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        for sig in previous:
            signal.signal(sig, lambda *_: stop.set())
        while not stop.is_set():
            report = consumer.consume_once(stop)
            print("Build consumer: " + report, flush=True)
            if args.once or stop.wait(args.interval):
                break
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
