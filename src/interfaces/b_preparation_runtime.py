"""Opt-in hosted preparation runtime, no legacy App or startup schema writes."""

import os
from http.server import ThreadingHTTPServer
from pathlib import Path

from adapters.aws.source_artifacts import S3ArtifactSettings, S3SourceArtifactStore
from adapters.state.deployment_admission import PostgresDeploymentAdmission
from adapters.state.deployment_approvals import PostgresDeploymentApprovals
from adapters.state.deployment_previews import PostgresDeploymentPreviews
from adapters.state.deployment_reads import PostgresDeploymentReads
from adapters.state.operations import PostgresOperationStore
from adapters.state.postgres import PostgresStateSettings, RotatingDatabaseConnection
from application.deployment_preparation import DeploymentPreparationService
from application.deployment_reads import DeploymentReadService
from application.source_artifacts import SourceArtifactService
from interfaces.http.alb_identity import AlbRequestAuthenticator
from interfaces.http.deployment_approvals import DatabaseApprovalApp
from interfaces.http.deployment_preparation import DatabasePreparationApp, handler_for_preparation


def preparation_identity(args):
    """Accept exactly one complete identity source, matching the read-only API."""
    trusts = os.environ.get("SKY_ALB_TRUSTS_JSON")
    members = os.environ.get("SKY_MEMBERSHIPS_JSON")
    has_environment = trusts is not None or members is not None
    has_files = bool(args.alb_trusts_file or args.memberships_file)
    if has_environment:
        if has_files or trusts is None or members is None:
            raise ValueError("Use one complete ALB identity configuration source")
        return AlbRequestAuthenticator.from_json(trusts, members)
    if not args.alb_trusts_file or not args.memberships_file:
        raise ValueError("Both ALB identity files are required")
    return AlbRequestAuthenticator.from_files(Path(args.alb_trusts_file), Path(args.memberships_file))


def run_preparation_api(args):
    authenticator = preparation_identity(args)
    # Validate browser origin before constructing credential or AWS providers.
    DatabaseApprovalApp(None, None, authenticator=authenticator, origin=args.origin, readiness=lambda: None)
    database = PostgresStateSettings.from_environment()
    artifacts = S3ArtifactSettings.from_environment()
    workspace = os.environ.get("SKY_STATE_WORKSPACE", "team")
    operations = PostgresOperationStore(None, workspace=workspace)
    connection = RotatingDatabaseConnection(database)
    operations = PostgresOperationStore(connection, workspace=operations.workspace)
    approvals = PostgresDeploymentApprovals(
        PostgresDeploymentAdmission(operations, account_id=artifacts.account_id, region=artifacts.region)
    )
    previews = PostgresDeploymentPreviews(approvals)
    previews.check_ready()
    sources = SourceArtifactService(S3SourceArtifactStore(artifacts))
    preparation = DeploymentPreparationService(sources, previews)
    reads = DeploymentReadService(PostgresDeploymentReads(connection, workspace=workspace))
    app = DatabasePreparationApp(
        reads, approvals, preparation, authenticator=authenticator, origin=args.origin, workspace=workspace
    )
    server = ThreadingHTTPServer((args.host, args.port), handler_for_preparation(app))
    print(
        f"Sky preparation API: http://{args.host}:{args.port}/prepare; deployment consumer is not included",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
