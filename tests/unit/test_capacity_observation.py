from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock

import pytest

from application.capacity_observation import CapacityObservationRecorder
from engine.promotion_policy import CapacityPromotionCriteria
from ports.state import RecordConflict
from tests.unit.test_deployment_writes import principal
from tests.unit.test_operating_review import ID, NOW

pytest_plugins = ["tests.unit.test_operating_review"]


def test_sql_collection_cannot_refresh_old_http_health(setup):
    records, policy, review = setup
    policy = replace(policy, capacity_criteria=CapacityPromotionCriteria("collector", connections_percent=80))
    original = deepcopy(records.health[-1])
    original["checked_at"] = "2026-10-10T23:00:00+00:00"
    records.health[-1] = original
    metric = {
        "schema_version": 1,
        "collector_id": "collector",
        "database_binding": records.job["workload_database_binding"],
        "observed_at": NOW,
        "values": {"connections": 1, "connection_limit": 1},
    }
    collector = Mock(collect=Mock(return_value=metric))
    CapacityObservationRecorder(review, collector).collect(principal(), ID, policy)
    assert records.health[-1]["checked_at"] == original["checked_at"]
    assert records.health[-1]["database_metrics"] == metric
    with pytest.raises(ValueError, match="fresh healthy"):
        review.review(principal(), ID, policy, now=NOW)


def test_foreign_collector_and_concurrent_revision_are_blocked(setup):
    records, policy, review = setup
    policy = replace(policy, capacity_criteria=CapacityPromotionCriteria("collector", connections_percent=80))
    original = deepcopy(records.health)
    with pytest.raises(ValueError, match="scope"):
        CapacityObservationRecorder(
            review, Mock(collect=Mock(return_value={"collector_id": "wrong"}))
        ).collect(principal(), ID, policy)

    def collect(*args, **kwargs):
        records.revision += 1
        return {"collector_id": "collector", "database_binding": records.job["workload_database_binding"]}

    with pytest.raises(RecordConflict):
        CapacityObservationRecorder(review, Mock(collect=collect)).collect(principal(), ID, policy)
    assert records.health == original


def test_database_error_is_sanitized_before_leaving_metric_collector():
    import psycopg

    from adapters.database.workload_metrics import PostgresWorkloadMetrics
    from domain.database import DatabaseBinding

    binding = DatabaseBinding(
        "org", "app", "shared_workload", "111111111111", "ap-northeast-2", "pool", "sky_app_a", "allocation-a"
    )
    collector = PostgresWorkloadMetrics(
        Mock(side_effect=psycopg.OperationalError("sensitive-connection-detail")),
        pool_database="sky_pool_test",
        collector_id="registered",
        clock=lambda: NOW,
    )
    with pytest.raises(OSError, match="metrics unavailable") as error:
        collector.collect(binding)
    assert "sensitive" not in str(error.value)
