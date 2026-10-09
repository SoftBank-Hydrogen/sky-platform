"""Database execution must match the compiled target's actual resource choice."""

import copy

import pytest

from adapters.aws.postgres import PostgresRequest
from application.consistency import check_database_consistency
from engine.compatibility import InfrastructureProfile

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
