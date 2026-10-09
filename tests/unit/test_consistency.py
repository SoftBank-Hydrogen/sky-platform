"""Database execution must match the compiled target's actual resource choice."""

import copy
import hashlib
import sqlite3

import pytest

from adapters.aws.postgres import PostgresRequest
from adapters.database.sqlite_snapshot import compile_sqlite_snapshot
from application.consistency import (
    check_database_consistency,
    check_port_consistency,
    check_source_change_scope,
    check_target_resource_consistency,
    health_result_matches_plan,
)
from application.deployment_core import make_plan
from engine.compatibility import TARGET_RESOURCES, InfrastructureProfile

POSTGRES = InfrastructureProfile(
    "database", (), 1, requirements=("database",), database_engines=("postgresql",)
)
SQLITE = InfrastructureProfile(
    "sqlite", (), 1, requirements=("database", "sqlite"), database_engines=("sqlite",)
)
NO_SIGNAL = InfrastructureProfile("unconfirmed", (), 1)


def test_postgres_plan_requires_same_database_resource_and_final_source():
    request = PostgresRequest("demo-app", "123456789012", "ap-northeast-2", "vpc-12345678", (), "sg-12345678")
    plan = {
        "compatibility": {"postgres_binding": True, "local_sqlite_binding": False},
        "database": {"binding": "existing", "database_id": request.database_id},
        "resources": ["existing RDS PostgreSQL"],
    }
    assert check_database_consistency(plan, POSTGRES, postgres_request=request)["status"] == "pass"
    with pytest.raises(ValueError, match="source requirement"):
        check_database_consistency(plan, SQLITE, postgres_request=request)
    missing_resource = copy.deepcopy(plan)
    missing_resource["resources"] = []
    with pytest.raises(ValueError, match="resource"):
        check_database_consistency(missing_resource, POSTGRES, postgres_request=request)
    wrong_database = copy.deepcopy(plan)
    wrong_database["database"]["database_id"] = "sky-other-app"
    with pytest.raises(ValueError, match="identity"):
        check_database_consistency(wrong_database, POSTGRES, postgres_request=request)
    with pytest.raises(ValueError, match="execution binding"):
        check_database_consistency(plan, POSTGRES)


def test_sqlite_conversion_requires_final_postgres_source():
    request = PostgresRequest("demo-app", "123456789012", "ap-northeast-2", "vpc-12345678", (), "sg-12345678")
    plan = {
        "compatibility": {"postgres_binding": True, "local_sqlite_binding": False},
        "database": {"binding": "create", "database_id": request.database_id},
        "resources": ["new RDS PostgreSQL"],
        "conversion_pending": "sqlite-to-postgresql",
    }
    assert (
        check_database_consistency(
            plan, POSTGRES, postgres_request=request, sqlite_conversion={"path": "app.db"}
        )["status"]
        == "pass"
    )
    with pytest.raises(ValueError, match="migration decision"):
        check_database_consistency(plan, POSTGRES, postgres_request=request)
    with pytest.raises(ValueError, match="resource or final source"):
        check_database_consistency(
            plan, SQLITE, postgres_request=request, sqlite_conversion={"path": "app.db"}
        )


def test_local_sqlite_volume_requires_same_binding_and_final_sqlite_source():
    binding = {"application_id": "demo-app", "mount_path": "/data"}
    plan = {
        "compatibility": {"postgres_binding": False, "local_sqlite_binding": True},
        "resources": ["optional SQLite volume"],
        "sqlite_volume": binding,
    }
    assert check_database_consistency(plan, SQLITE, local_sqlite_binding=binding)["status"] == "pass"
    with pytest.raises(ValueError, match="volume or final source"):
        check_database_consistency(plan, NO_SIGNAL, local_sqlite_binding=binding)
    with pytest.raises(ValueError, match="volume or final source"):
        check_database_consistency(plan, SQLITE, local_sqlite_binding={"mount_path": "/other"})


def test_no_database_signal_remains_unknown():
    plan = {"compatibility": {"postgres_binding": False, "local_sqlite_binding": False}}
    assert check_database_consistency(plan, NO_SIGNAL) == {
        "id": "CV-03",
        "status": "unknown",
        "source": "final_working_copy",
    }


def test_final_dockerfile_stage_must_not_conflict_with_selected_http_port(tmp_path):
    (tmp_path / "Dockerfile").write_text(
        "FROM node:22 AS builder\nEXPOSE 3000\nFROM node:22\nEXPOSE 8080/tcp\n"
    )
    assert check_port_consistency(make_plan(tmp_path, "dockerfile", None, 8080)) == {
        "id": "CV-06",
        "status": "unknown",
        "source": "executable_dockerfile",
    }
    with pytest.raises(ValueError, match="CV-06.*8081"):
        check_port_consistency(make_plan(tmp_path, "dockerfile", None, 8081))


@pytest.mark.parametrize("expose", ["", "EXPOSE $PORT\n", "EXPOSE 8080-8082\n", "EXPOSE 8080 \\\n 8081\n"])
def test_unresolved_dockerfile_port_is_left_for_http_verification(tmp_path, expose):
    (tmp_path / "Dockerfile").write_text("FROM node:22\n" + expose)
    assert check_port_consistency(make_plan(tmp_path, "dockerfile", None, 8080))["status"] == "unknown"


def test_udp_only_declaration_conflicts_with_http_port(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM node:22\nEXPOSE 8080/udp\n")
    with pytest.raises(ValueError, match="CV-06"):
        check_port_consistency(make_plan(tmp_path, "dockerfile", None, 8080))


def test_http_result_must_reference_the_planned_health_endpoint():
    plan = {"target": "cloud-run", "port": 8080, "health_path": "/ready"}
    result = {"url": "https://example.test", "health_url": "https://example.test/ready"}
    assert health_result_matches_plan(plan, result)
    for changed in (
        {**result, "health_url": "https://example.test/"},
        {**result, "url": "https://example.test/other", "health_url": "https://example.test/other/ready"},
        {**result, "url": "file://example.test", "health_url": "file://example.test/ready"},
    ):
        assert not health_result_matches_plan(plan, changed)


@pytest.mark.parametrize(
    "target,access_mode",
    [
        ("local-docker", "loopback"),
        ("onprem-compose", "loopback"),
        ("cloud-run", "public"),
        ("aws-ecs-express", "public"),
    ],
)
def test_compiled_resources_require_implemented_target_capabilities(target, access_mode):
    plan = {
        "target": target,
        "resources": TARGET_RESOURCES[target],
        "compatibility": {"access_mode": access_mode},
    }
    compilation = {
        "target_plan": {"target": target, "resources": list(plan["resources"]), "access_mode": access_mode}
    }
    assert check_target_resource_consistency(compilation, plan, target) == {
        "id": "CV-09",
        "status": "pass",
        "source": "compiled_target_plan",
    }
    unknown = copy.deepcopy(plan)
    unknown["resources"].append("unimplemented queue")
    compilation["target_plan"]["resources"] = list(unknown["resources"])
    with pytest.raises(ValueError, match="CV-09.*unimplemented queue"):
        check_target_resource_consistency(compilation, unknown, target)
    if target != "aws-ecs-express":
        extra = copy.deepcopy(plan)
        extra["resources"].append("new RDS PostgreSQL")
        compilation["target_plan"]["resources"] = list(extra["resources"])
        with pytest.raises(ValueError, match="CV-09.*new RDS PostgreSQL"):
            check_target_resource_consistency(compilation, extra, target)


def test_target_resource_check_rejects_mixed_plan_and_unsupported_access():
    target = "aws-ecs-express"
    plan = {
        "target": target,
        "resources": list(TARGET_RESOURCES[target]),
        "compatibility": {"access_mode": "public"},
    }
    compilation = {
        "target_plan": {"target": target, "resources": list(plan["resources"]), "access_mode": "public"}
    }
    compilation["target_plan"]["resources"].append("new RDS PostgreSQL")
    with pytest.raises(ValueError, match="CV-09.*disagree"):
        check_target_resource_consistency(compilation, plan, target)
    compilation["target_plan"]["resources"] = list(plan["resources"])
    compilation["target_plan"]["access_mode"] = "loopback"
    plan["compatibility"]["access_mode"] = "loopback"
    with pytest.raises(ValueError, match="CV-09.*access mode"):
        check_target_resource_consistency(compilation, plan, target)


@pytest.mark.parametrize("database_resource", ["new RDS PostgreSQL", "existing RDS PostgreSQL"])
def test_aws_database_and_migration_resources_have_separate_adapter_capabilities(database_resource):
    target = "aws-ecs-express"
    resources = [*TARGET_RESOURCES[target], database_resource, "one-off SQL migration task"]
    plan = {"target": target, "resources": resources, "compatibility": {"access_mode": "public"}}
    compilation = {"target_plan": {"target": target, "resources": list(resources), "access_mode": "public"}}
    assert check_target_resource_consistency(compilation, plan, target)["status"] == "pass"


def test_applied_source_changes_must_follow_agent_file_scope(tmp_path):
    (tmp_path / "server.js").write_text("before")
    record = {"changes": [{"path": "server.js", "before_sha256": "a", "after_sha256": "b"}]}
    assert check_source_change_scope(record, tmp_path)["status"] == "pass"
    for name in (
        ".env",
        "package-lock.json",
        "node_modules/backdoor.js",
        "migrations/extra.sql",
        "Dockerfile",
    ):
        record["changes"][0]["path"] = name
        with pytest.raises(ValueError, match="CV-02"):
            check_source_change_scope(record, tmp_path)
    record["changes"][0].update(path="server.js", after_sha256=None)
    with pytest.raises(ValueError, match="CV-02"):
        check_source_change_scope(record, tmp_path)


def test_sqlite_conversion_requires_matching_approved_snapshot_and_generated_sql(tmp_path):
    database = tmp_path / "data" / "scores.db"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE scores (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO scores VALUES (1, 'hello')")
    snapshot = compile_sqlite_snapshot(database)
    approval = {
        "path": "data/scores.db",
        "source_sha256": snapshot.source_sha256,
        "row_counts": snapshot.row_counts,
        "schema": snapshot.schema,
    }
    record = {
        "changes": [
            {"path": "data/scores.db", "before_sha256": snapshot.source_sha256, "after_sha256": None},
            {
                "path": "migrations/0000_sky_sqlite_import.sql",
                "before_sha256": None,
                "after_sha256": hashlib.sha256(snapshot.sql.encode()).hexdigest(),
            },
        ]
    }
    assert check_source_change_scope(record, tmp_path, approval)["status"] == "pass"
    record["changes"][1]["after_sha256"] = hashlib.sha256(b"different SQL").hexdigest()
    with pytest.raises(ValueError, match="CV-02.*SQLite conversion"):
        check_source_change_scope(record, tmp_path, approval)
    record["changes"].pop()
    with pytest.raises(ValueError, match="CV-02.*incomplete"):
        check_source_change_scope(record, tmp_path, approval)
