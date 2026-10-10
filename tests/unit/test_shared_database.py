"""Pool scope and application ownership must precede privileged allocation."""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from adapters.database.shared_pool import PostgresSharedPool
from application.shared_database import SharedDatabaseService
from domain.access import LoginSource, Principal, Role
from domain.shared_database import PoolAllocationRequest, SharedDatabasePool
from ports.shared_database import AllocationCredentials, PoolAllocationError


def pool():
    return SharedDatabasePool(
        "demo", "111111111111", "ap-northeast-2", "workload-pool", "sky_pool_control", 30
    )


def test_different_organizations_have_different_db_and_role_names():
    first = PoolAllocationRequest(pool(), "team-a", "game")
    second = PoolAllocationRequest(pool(), "team-b", "game")
    assert first.database_name != second.database_name
    assert first.login_role != second.login_role
    assert first.binding().application_id == "game"
    assert first.binding().role == "shared_workload"
    with pytest.raises(ValueError):
        replace(pool(), role="sky_state")


def test_viewer_foreign_org_and_mismatched_application_never_call_adapter():
    adapter = Mock()
    service = SharedDatabaseService(adapter)
    request = PoolAllocationRequest(pool(), "team-a", "game")
    application = {"application_id": "game", "organization_id": "team-a", "created_by": "alice"}
    credentials = AllocationCredentials("secret-ref", "disposable-password-for-test")
    for principal in (
        Principal("alice", "team-a", Role.VIEWER, LoginSource.LOCAL),
        Principal("bob", "team-b", Role.ADMIN, LoginSource.LOCAL),
    ):
        with pytest.raises(PermissionError):
            service.allocate(principal, application, request, credentials)
    deployer = Principal("alice", "team-a", Role.DEPLOYER, LoginSource.LOCAL)
    with pytest.raises(PermissionError):
        service.allocate(deployer, {**application, "application_id": "other"}, request, credentials)
    adapter.allocate.assert_not_called()
    service.allocate(deployer, application, request, credentials)
    adapter.allocate.assert_called_once_with(request, credentials)
    assert credentials.password not in repr(credentials)


def test_other_app_secret_reference_is_rejected_before_any_database_connection():
    request = PoolAllocationRequest(pool(), "team-a", "game")
    other = PoolAllocationRequest(pool(), "team-b", "game")
    reference = f"arn:aws:secretsmanager:{pool().region}:{pool().account_id}:secret:sky-pool/{pool().id}/{other.id}-test01"
    connect = Mock()
    adapter = PostgresSharedPool(pool(), connect, Mock())
    with pytest.raises(PoolAllocationError, match="credential reference"):
        adapter.allocate(request, AllocationCredentials(reference, "disposable-password-for-test"))
    connect.assert_not_called()
