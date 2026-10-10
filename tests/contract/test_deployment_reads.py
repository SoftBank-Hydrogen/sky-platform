"""Read isolation and projections using an explicitly disposable local PostgreSQL."""

import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.types.json import Jsonb

from adapters.state.deployment_reads import PostgresDeploymentReads
from adapters.state.postgres import PostgresDeploymentRecordStore
from application.deployment_reads import DeploymentReadService
from domain.access import LoginSource, Principal, Role


@pytest.fixture
def setup():
    dsn = os.environ.get("SKY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Disposable PostgreSQL DSN required")
    if psycopg.conninfo.conninfo_to_dict(dsn).get("host") not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("Only loopback PostgreSQL endpoints are allowed")
    connect = lambda: psycopg.connect(dsn, options="-c statement_timeout=30000 -c lock_timeout=10000")
    store = PostgresDeploymentRecordStore(connect, workspace=uuid4().hex)
    store.initialize()
    return store, connect, DeploymentReadService(PostgresDeploymentReads(connect, workspace=store.workspace))


def principal(org="org1", role=Role.VIEWER, source=LoginSource.LOCAL):
    return Principal("user1", org, role, source)


def job(identifier="job1", **extra):
    return {
        "id": identifier,
        "organization_id": "org1",
        "created_by": "author",
        "status": "succeeded",
        "created_at": "2026-10-10T00:00:00Z",
        **extra,
    }


@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize("source", list(LoginSource))
def test_members_read_but_even_other_organization_admin_cannot(setup, role, source):
    store, _, service = setup
    store.save_job("job1", job())
    assert service.detail(principal(role=role, source=source), "job1")["id"] == "job1"
    outsider = principal("org2", role, source)
    assert service.summaries(outsider).items == ()
    with pytest.raises(FileNotFoundError, match="Deployment not found"):
        service.detail(outsider, "job1")
    with pytest.raises(FileNotFoundError):
        service.history(outsider, "job1")


@pytest.mark.parametrize(
    "owner",
    [
        {},
        {"organization_id": "org1"},
        {"organization_id": "org1", "created_by": ""},
        {"organization_id": "org1", "created_by": 4},
        {"organization_id": "org1", "created_by": "bad\n"},
    ],
)
def test_legacy_or_invalid_ownership_is_not_claimed(setup, owner):
    store, _, service = setup
    store.save_job("job1", {"id": "job1", "status": "succeeded", **owner})
    assert service.summaries(principal(role=Role.ADMIN)).items == ()
    with pytest.raises(FileNotFoundError):
        service.detail(principal(role=Role.ADMIN), "job1")


def test_projections_and_detached_history(setup):
    store, _, service = setup
    store.save_job("job1", job(status="failed", events=[{"stage": "building", "message": "private"}]))
    store.save_health("job1", [{"healthy": False}])
    detail = service.detail(principal(), "job1")
    assert detail["diagnosis"]["last_observed_phase"] == "image_build"
    assert "private" not in str(detail["diagnosis"])
    assert detail["last_health"] == {"healthy": False}
    assert detail["monitor_error"] is None
    detail["health_history"].clear()
    assert service.history(principal(), "job1") == [{"healthy": False}]
    summary = service.summaries(principal()).items[0]
    assert set(summary) == {
        "id",
        "status",
        "created_at",
        "application_id",
        "deployment_state",
        "release_rollback_state",
        "analyzer",
        "result",
        "last_health",
        "monitor_error",
    }
    assert service.releases(principal(), "job1").items[0]["target"] == "local-docker"
    assert service.releases(principal(), "absent").items == ()


def test_independent_readers_observe_committed_updates_without_recovery(setup):
    store, connect, service = setup
    store.save_job("job1", job(status="running"))
    second = DeploymentReadService(PostgresDeploymentReads(connect, workspace=store.workspace))
    assert service.detail(principal(), "job1")["status"] == "running"
    store.save_job("job1", job(status="succeeded"), expected_revision=1)
    assert second.detail(principal(), "job1")["status"] == "succeeded"
    assert service.detail(principal(), "job1")["status"] == "succeeded"
    assert store.load_job("job1").revision == 2


def test_same_id_in_another_workspace_does_not_leak_health(setup):
    store, connect, service = setup
    other = PostgresDeploymentRecordStore(connect, workspace=uuid4().hex)
    store.save_job("job1", job())
    other.save_job("job1", job(result={"private": True}))
    other.save_health("job1", [{"private": True}])
    assert service.detail(principal(), "job1")["health_history"] == []
    assert "result" not in service.detail(principal(), "job1")


def test_keyset_pagination_filters_ownership_and_application_before_limit(setup):
    store, _, service = setup
    for identifier in ("a", "b", "c", "d"):
        store.save_job(identifier, job(identifier, application_id="app1"))
    store.save_job("z", job("z", organization_id="org2"))
    store.save_job("y", job("y", application_id="app2"))
    first = service.releases(principal(), "app1", limit=2)
    second = service.releases(principal(), "app1", limit=2, cursor=first.next_cursor)
    assert [x["id"] for x in first.items + second.items] == ["d", "c", "b", "a"]
    assert second.next_cursor is None
    with pytest.raises(ValueError, match="cursor"):
        service.summaries(principal(), cursor=first.next_cursor)
    with pytest.raises(ValueError, match="cursor"):
        service.releases(principal("org2"), "app1", cursor=first.next_cursor)


@pytest.mark.parametrize("limit", [0, 101, True, "5"])
def test_invalid_page_limits_are_rejected(setup, limit):
    with pytest.raises(ValueError):
        setup[2].summaries(principal(), limit=limit)


def test_invalid_principal_never_reaches_database():
    class Unreachable:
        def page(self, *args, **kwargs):
            pytest.fail("Unauthenticated call reached DB")

    with pytest.raises(PermissionError):
        DeploymentReadService(Unreachable()).summaries(None)


@pytest.mark.parametrize("bad", [{"id": "different"}, {"plan": []}, {"created_at": 4}])
def test_corrupt_owned_record_fails_with_redacted_error(setup, bad):
    store, _, service = setup
    store.save_job("job1", job(**bad))
    with pytest.raises(ValueError, match="Invalid persisted deployment read record"):
        service.detail(principal(), "job1")


def test_corrupt_health_fails_instead_of_fabricating_success(setup):
    store, _, service = setup
    store.save_job("job1", job())
    store.save_health("job1", {"private": "invalid"})
    with pytest.raises(ValueError, match="Invalid persisted"):
        service.history(principal(), "job1")


def test_joined_read_never_combines_two_committed_generations(setup):
    store, connect, service = setup
    store.save_job("job1", job(generation=0))
    store.save_health("job1", [{"generation": 0}])

    def writer():
        for generation in range(1, 31):
            with connect() as connection:
                connection.execute(
                    "UPDATE sky_state.metadata_records SET document=%s WHERE workspace=%s AND kind='job' AND record_id='job1'",
                    (Jsonb(job(generation=generation)), store.workspace),
                )
                connection.execute(
                    "UPDATE sky_state.metadata_records SET document=%s WHERE workspace=%s AND kind='health' AND record_id='job1'",
                    (Jsonb([{"generation": generation}]), store.workspace),
                )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(writer)
        for _ in range(60):
            detail = service.detail(principal(), "job1")
            assert detail["generation"] == detail["last_health"]["generation"]
        future.result()
    assert service.detail(principal(), "job1")["generation"] == 30


def test_query_connection_is_read_only(setup):
    store, connect, _ = setup
    reads = PostgresDeploymentReads(connect, workspace=store.workspace)
    with reads._connection() as connection:
        assert connection.execute("SHOW transaction_read_only").fetchone() == ("on",)
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            connection.execute(
                "DELETE FROM sky_state.metadata_records WHERE workspace=%s", (store.workspace,)
            )
        connection.rollback()
