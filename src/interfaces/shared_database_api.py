"""Explicit hosted shared DB intake composition; workload credentials stay in worker."""

import os
from http.server import ThreadingHTTPServer

from adapters.state.deployment_reads import PostgresDeploymentReads
from adapters.state.operations import PostgresOperationStore
from adapters.state.postgres import (
    PostgresDeploymentRecordStore,
    PostgresStateSettings,
    RotatingDatabaseConnection,
)
from adapters.state.shared_database_reviews import PostgresSharedDatabaseReviews
from application.deployment_reads import DeploymentReadService
from interfaces.http.mutation_requests import validate_origin
from interfaces.http.shared_database import SharedDatabaseApp, handler_for_shared_database
from interfaces.operating_review import OperatingReviewController, load_operating_policies
from interfaces.shared_database_worker import load_hosted_identity, load_pool_configuration


def run_shared_database_api(args, parser):
    try:
        if args.auth_mode != "alb":
            raise ValueError("Hosted authentication required for shared database admission")
        origin = validate_origin(args.origin)
        state = PostgresStateSettings.from_environment()
        settings, digest = load_pool_configuration(args.shared_database_pool_config)
        if (
            settings.pool.account_id != os.environ.get("SKY_AWS_ACCOUNT_ID")
            or settings.pool.region != state.region
            or settings.pool.control_database == state.database
        ):
            raise ValueError("Workload and state settings must be separate in the registered account/region")
        authenticator = load_hosted_identity(args)
        workspace = os.environ.get("SKY_STATE_WORKSPACE", "team")
        PostgresOperationStore(None, workspace=workspace)
        policy_path = getattr(args, "operating_review_policy_config", None)
        policies = (load_operating_policies(policy_path, account_id=settings.pool.account_id,
                                           region=state.region) if policy_path else None)
    except (ValueError, TypeError, KeyError, OSError):
        parser.error("Invalid shared database API configuration or identity settings")
    if args.check_config:
        print("Configuration valid: shared database review API; no AWS/DB calls", flush=True)
        return
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        # Only Sky state credentials are read here; no workload allocator/admin client.
        connection = RotatingDatabaseConnection(state)
        operations = PostgresOperationStore(connection, workspace=workspace)
        reviews = PostgresSharedDatabaseReviews(operations, settings.pool, digest)
        reviews.check_ready()
        reads = DeploymentReadService(PostgresDeploymentReads(connection, workspace=workspace))
        app = SharedDatabaseApp(
            reads, reviews, authenticator=authenticator, origin=origin, workspace=workspace
        )
        handler = handler_for_shared_database(app)
        if policies is not None:
            from interfaces.http.operating_review import handler_for_operating_review

            handler = handler_for_operating_review(app, OperatingReviewController(
                PostgresDeploymentRecordStore(connection, workspace=workspace), policies))
        server = ThreadingHTTPServer((args.host, args.port), handler)
        print(f"Sky shared database review: {origin}/shared-database", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    except (OSError, ValueError, BotoCoreError, ClientError):
        parser.exit(1, "Shared database API unavailable; check state schema, authentication and access.\n")
