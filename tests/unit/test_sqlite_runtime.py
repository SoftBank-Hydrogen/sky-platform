"""Isolated preparation config validation performs no network/database calls."""

from unittest.mock import patch

import pytest

from adapters.aws.source_artifacts import S3ArtifactSettings
from interfaces.b_runtime import main
from interfaces.b_sqlite_runtime import preparation_scope


def environment():
    return {
        "SKY_STATE_WORKSPACE": "team",
        "SKY_PREPARATION_WORKSPACE": "team_prepare",
        "SKY_JOB_QUEUE_URL": "https://sqs.ap-northeast-2.amazonaws.com/123456789012/build.fifo",
        "SKY_PREPARATION_QUEUE_URL": "https://sqs.ap-northeast-2.amazonaws.com/123456789012/prepare.fifo",
    }


@pytest.mark.parametrize(
    "field", ["SKY_PREPARATION_WORKSPACE", "SKY_PREPARATION_QUEUE_URL", "SKY_JOB_QUEUE_URL"]
)
def test_partial_preparation_scope_rejected(field):
    env = environment()
    del env[field]
    with pytest.raises(ValueError):
        preparation_scope(env, S3ArtifactSettings("artifacts-bucket", "ap-northeast-2", "123456789012"))


@pytest.mark.parametrize(
    "field,base",
    [
        ("SKY_PREPARATION_WORKSPACE", "SKY_STATE_WORKSPACE"),
        ("SKY_PREPARATION_QUEUE_URL", "SKY_JOB_QUEUE_URL"),
    ],
)
def test_shared_build_scope_rejected(field, base):
    env = environment()
    env[field] = env[base]
    with pytest.raises(ValueError, match="separate"):
        preparation_scope(env, S3ArtifactSettings("artifacts-bucket", "ap-northeast-2", "123456789012"))


def test_config_checks_without_aws_database_or_github(tmp_path, monkeypatch):
    ca = tmp_path / "ca.pem"
    ca.write_text("configuration only")
    env = {
        **environment(),
        "SKY_DATABASE_HOST": "db.example",
        "SKY_DATABASE_NAME": "sky",
        "SKY_DATABASE_SECRET_ARN": "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:test",
        "SKY_AWS_REGION": "ap-northeast-2",
        "SKY_AWS_ACCOUNT_ID": "123456789012",
        "SKY_ARTIFACTS_BUCKET": "artifacts-bucket",
        "SKY_DATABASE_SSLROOTCERT": str(ca),
        "ECS_AGENT_URI": "http://169.254.170.2",
    }
    with (
        patch.dict("os.environ", env, clear=True),
        patch("boto3.client", side_effect=AssertionError("AWS")),
        patch("interfaces.b_sqlite_runtime.RotatingDatabaseConnection", side_effect=AssertionError("DB")),
    ):
        main(["worker", "--mode", "prepare-sqlite", "--check-config"])
        main(["worker", "--mode", "preparation-outbox", "--check-config"])


def test_sqlite_api_requires_preparation_opt_in():
    with pytest.raises(SystemExit) as error:
        main(["api", "--enable-sqlite-preparation"])
    assert error.value.code == 2


def test_prepare_mode_cannot_enable_deployment():
    with pytest.raises(SystemExit) as error:
        main(["worker", "--mode", "prepare-sqlite", "--deploy-built-image"])
    assert error.value.code == 2
