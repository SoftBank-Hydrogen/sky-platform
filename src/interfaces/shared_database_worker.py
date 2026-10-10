"""One-operation workload DB worker; no automatic SQS consumption or deployment."""

import hashlib
import json
import os
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
    data = Path(path).read_bytes()
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


def run_shared_database(args, parser):
    try:
        if not args.pool_config or not args.alb_trusts_file or not args.memberships_file:
            raise ValueError("Explicit registered pool and current membership files are required")
        state = PostgresStateSettings.from_environment()
        settings, digest = load_pool_configuration(args.pool_config)
        if (
            settings.pool.account_id != os.environ.get("SKY_AWS_ACCOUNT_ID")
            or settings.pool.region != state.region
            or settings.pool.control_database == state.database
        ):
            raise ValueError("Workload and state settings must be separate in the registered account/region")
        authenticator = AlbRequestAuthenticator.from_files(
            Path(args.alb_trusts_file), Path(args.memberships_file)
        )
        workspace = os.environ.get("SKY_STATE_WORKSPACE", "team")
        PostgresOperationStore(None, workspace=workspace)
        if not args.check_config:
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
        worker = SharedDatabaseWorker(
            records,
            operations,
            settings.pool,
            digest,
            allocator,
            lambda user, org: current_membership(authenticator, user, org),
            owner="shared-db-" + uuid4().hex,
        )
        result = worker.execute(args.operation_id, args.attempt_id)
        print(
            f"Shared database operation={result['operation_id']} status={result['status']}; app deployment not started",
            flush=True,
        )
        if result["status"] in {"failed", "needs_attention", "lease_lost", "interrupted"}:
            parser.exit(1, "Shared database allocation requires review; no automatic redeployment.\n")
    except (ValueError, PermissionError, OSError, TypeError, KeyError, BotoCoreError, ClientError):
        parser.exit(1, "Shared database worker unavailable; inspect durable operation state.\n")
