"""CLI configuration may not construct clients, consume queues or bypass policies."""

import json
from dataclasses import asdict
from unittest.mock import Mock, patch

import pytest

from interfaces.b_runtime import main
from interfaces.dedicated_database_worker import load_dedicated_configuration

pytest_plugins = ["tests.unit.test_aws_dedicated_database", "tests.unit.test_b_runtime"]


@pytest.fixture
def registered(prepared, worker_environment, monkeypatch):
    adapter, plan, *_ = prepared
    settings = adapter.settings
    monkeypatch.setenv("SKY_AWS_ACCOUNT_ID", settings.source.pool.account_id)
    monkeypatch.setenv(
        "SKY_DATABASE_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:111111111111:secret:state-test01"
    )
    monkeypatch.setenv(
        "SKY_SHARED_DATABASE_POOL_JSON", json.dumps({"version": 1, "settings": asdict(settings.source)})
    )
    configuration = asdict(settings)
    configuration.pop("source")
    monkeypatch.setenv(
        "SKY_DEDICATED_DATABASE_CONFIG_JSON", json.dumps({"version": 1, "settings": configuration})
    )
    policy = {
        "job_id": "a" * 16,
        "policy_id": "policy",
        "review_after": "2026-10-11T00:00:00Z",
        "target": plan["target"],
        "max_health_age_seconds": 300,
        "websocket_sessions": False,
        "target_configuration_digest": settings.digest,
    }
    monkeypatch.setenv(
        "SKY_OPERATING_REVIEW_POLICIES_JSON", json.dumps({"schema_version": 1, "policies": [policy]})
    )
    monkeypatch.setenv(
        "SKY_DEDICATED_DATABASE_QUEUE_URL",
        "https://sqs.ap-northeast-2.amazonaws.com/111111111111/dedicated.fifo",
    )
    return settings, policy


@pytest.mark.parametrize("mode", ["dedicated-database", "dedicated-database-queue"])
def test_check_config_never_constructs_aws_or_database_clients(registered, mode, capsys):
    with (
        patch("interfaces.dedicated_database_worker.load_hosted_identity", return_value=Mock()),
        patch("boto3.client", side_effect=AssertionError("AWS call")),
        patch(
            "interfaces.dedicated_database_worker.RotatingDatabaseConnection",
            side_effect=AssertionError("DB call"),
        ),
        patch(
            "interfaces.dedicated_database_worker.AwsDedicatedDatabasePreparer",
            side_effect=AssertionError("RDS client"),
        ),
    ):
        main(["worker", "--mode", mode, "--check-config"])
    assert "no AWS/DB calls" in capsys.readouterr().out


@pytest.mark.parametrize(
    "change", ["digest", "missing", "same_queue", "initialize", "operation", "hosted_protection"]
)
def test_unsafe_configuration_is_rejected_before_clients(registered, monkeypatch, change):
    _, policy = registered
    extra = []
    if change == "digest":
        policy["target_configuration_digest"] = "b" * 64
        monkeypatch.setenv(
            "SKY_OPERATING_REVIEW_POLICIES_JSON", json.dumps({"schema_version": 1, "policies": [policy]})
        )
    elif change == "missing":
        monkeypatch.delenv("SKY_DEDICATED_DATABASE_CONFIG_JSON")
    elif change == "same_queue":
        monkeypatch.setenv(
            "SKY_SHARED_DATABASE_QUEUE_URL",
            "https://sqs.ap-northeast-2.amazonaws.com/111111111111/dedicated.fifo",
        )
    elif change == "initialize":
        extra = ["--initialize-pool"]
    elif change == "hosted_protection":
        monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/task")
    else:
        extra = ["--operation-id", "not-allowed"]
    with (
        patch("interfaces.dedicated_database_worker.load_hosted_identity", return_value=Mock()),
        patch(
            "interfaces.dedicated_database_worker.RotatingDatabaseConnection",
            side_effect=AssertionError("DB call"),
        ),
        pytest.raises(SystemExit) as error,
    ):
        main(["worker", "--mode", "dedicated-database-queue", "--check-config", *extra])
    assert error.value.code == 2


def test_duplicate_target_configuration_fields_are_rejected(registered, monkeypatch):
    settings, _ = registered
    monkeypatch.setenv("SKY_DEDICATED_DATABASE_CONFIG_JSON", '{"version":1,"version":1,"settings":{}}')
    with pytest.raises(ValueError, match="Duplicate"):
        load_dedicated_configuration(None, settings.source)
