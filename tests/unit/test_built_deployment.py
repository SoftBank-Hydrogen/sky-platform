"""SDK boundary: immutable image, account, ownership and readiness are mandatory."""

from copy import deepcopy
from unittest.mock import Mock
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError

from adapters.aws.built_deployment import BuiltImageDeployment
from ports.artifacts import SourceArtifact
from ports.remote_builds import BuildSettings, canonical, digest


@pytest.fixture
def prepared():
    settings = BuildSettings(
        "SoftBank-Hydrogen/sky-builder",
        "main",
        "a" * 40,
        "b" * 40,
        "sky-test-artifacts",
        "123456789012",
        "ap-northeast-2",
    )
    source = SourceArtifact("team", "game", "a" * 32, "prepared", "b" * 64, 100, "c" * 64)
    plan = {
        "target": "aws",
        "replicas": 1,
        "port": 8080,
        "health_path": "/health",
        "required_env": [],
        "source_digest": source.source_digest,
        "dockerfile_source": "existing",
        "dockerfile": "FROM node:22",
    }
    request = {
        "version": 1,
        "build_id": str(uuid4()),
        "workspace": "team",
        "job_id": "a" * 16,
        "created_by": "alice",
        "source_ref": source.record(),
        "approved_plan_json": canonical(plan).decode(),
        "plan_digest": digest(plan),
        "source_digest": source.source_digest,
        "bucket": settings.bucket,
        "account_id": settings.account_id,
        "region": settings.region,
        "repository": settings.repository,
        "workflow_sha": settings.workflow_sha,
        "platform_code_sha": settings.platform_code_sha,
    }
    result = {
        k: request[k]
        for k in (
            "version",
            "build_id",
            "source_digest",
            "plan_digest",
            "repository",
            "workflow_sha",
            "platform_code_sha",
        )
    }
    result.update(
        request_digest=digest(request),
        run_id=42,
        platform="linux/amd64",
        image_digest="sha256:" + "d" * 64,
        image=f"{settings.account_id}.dkr.ecr.{settings.region}.amazonaws.com/sky-managed@sha256:" + "d" * 64,
    )
    ecs, stacks, identity, probe = Mock(), Mock(), Mock(), Mock()
    identity.get_caller_identity.return_value = {"Account": settings.account_id}
    stacks.describe_stacks.return_value = {
        "Stacks": [
            {
                "StackStatus": "UPDATE_COMPLETE",
                "Tags": [{"Key": "sky-managed", "Value": "true"}],
                "Outputs": [
                    {"OutputKey": "RepositoryUri", "OutputValue": result["image"].split("@")[0]},
                    {
                        "OutputKey": "ExecutionRoleArn",
                        "OutputValue": f"arn:aws:iam::{settings.account_id}:role/sky-core-ExecutionRole-abc",
                    },
                    {
                        "OutputKey": "InfrastructureRoleArn",
                        "OutputValue": f"arn:aws:iam::{settings.account_id}:role/sky-core-InfrastructureRole-abc",
                    },
                ],
            }
        ]
    }
    ecs.describe_express_gateway_service.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException"}}, "DescribeExpressGatewayService"
    )
    adapter = BuiltImageDeployment(settings, ecs=ecs, stacks=stacks, identity=identity, probe=probe)
    return adapter, request, result


def ready(adapter, intent):
    payload = intent["payload"]
    task = "arn:aws:ecs:ap-northeast-2:123456789012:task-definition/game:1"
    config = {
        k: deepcopy(payload[k])
        for k in ("primaryContainer", "executionRoleArn", "healthCheckPath", "scalingTarget")
    }
    config.update(
        taskDefinitionArn=task,
        ingressPaths=[{"accessType": "PUBLIC", "endpoint": "game.ecs.ap-northeast-2.on.aws"}],
    )
    service = {
        "serviceArn": intent["service_arn"],
        "tags": payload["tags"],
        "status": {"statusCode": "ACTIVE"},
        "activeConfigurations": [config],
    }
    adapter.ecs.describe_express_gateway_service.side_effect = None
    adapter.ecs.describe_express_gateway_service.return_value = {"service": service}
    adapter.ecs.describe_task_definition.return_value = {
        "taskDefinition": {"containerDefinitions": [{"name": "Main", "image": intent["image"]}]}
    }
    return service


def test_digest_is_deployed_without_docker_or_core_apply(prepared):
    adapter, request, result = prepared
    intent = adapter.prepare(request, result)
    assert intent["payload"]["primaryContainer"]["image"] == result["image"]
    assert intent["payload"]["cpuArchitecture"] == "X86_64"
    adapter.ecs.create_express_gateway_service.return_value = {
        "service": {"serviceArn": intent["service_arn"]}
    }
    adapter.create(intent)
    ready(adapter, intent)
    observed = adapter.observe(intent)
    assert observed["image"] == result["image"] and observed["public"]
    adapter.probe.assert_called_once_with("https://game.ecs.ap-northeast-2.on.aws/health")
    assert not adapter.stacks.create_stack.called and not adapter.stacks.update_stack.called


@pytest.mark.parametrize(
    "field,value",
    [
        ("required_env", ["SECRET"]),
        ("port", True),
        ("port", 80),
        ("health_path", "/../secret"),
        ("health_path", "//evil"),
    ],
)
def test_unsupported_approved_plan_never_creates(prepared, field, value):
    adapter, request, result = prepared
    import json

    plan = json.loads(request["approved_plan_json"])
    plan[field] = value
    request["approved_plan_json"], request["plan_digest"] = canonical(plan).decode(), digest(plan)
    with pytest.raises(ValueError):
        adapter.prepare(request, result)
    assert not adapter.ecs.create_express_gateway_service.called


def test_existing_service_never_causes_second_create(prepared):
    adapter, request, result = prepared
    adapter.ecs.describe_express_gateway_service.side_effect = None
    with pytest.raises(ValueError):
        adapter.prepare(request, result)
    assert not adapter.ecs.create_express_gateway_service.called


def test_account_mismatch_never_creates(prepared):
    adapter, request, result = prepared
    adapter.identity.get_caller_identity.return_value = {"Account": "999999999999"}
    with pytest.raises(ValueError):
        adapter.prepare(request, result)
    assert not adapter.ecs.create_express_gateway_service.called


@pytest.mark.parametrize("change", ["tag", "image", "url", "task_image"])
def test_untrusted_observation_never_becomes_success(prepared, change):
    adapter, request, result = prepared
    intent = adapter.prepare(request, result)
    service = ready(adapter, intent)
    if change == "tag":
        service["tags"] = []
    if change == "image":
        service["activeConfigurations"][0]["primaryContainer"]["image"] = "evil"
    if change == "url":
        service["activeConfigurations"][0]["ingressPaths"][0]["endpoint"] = "https://127.0.0.1"
    if change == "task_image":
        adapter.ecs.describe_task_definition.return_value["taskDefinition"]["containerDefinitions"][0][
            "image"
        ] = "evil"
    with pytest.raises((ValueError, RuntimeError)):
        adapter.observe(intent)
    assert not adapter.probe.called


def test_unhealthy_http_is_not_success(prepared):
    adapter, request, result = prepared
    intent = adapter.prepare(request, result)
    ready(adapter, intent)
    adapter.probe.side_effect = OSError("unhealthy")
    with pytest.raises(OSError):
        adapter.observe(intent)
