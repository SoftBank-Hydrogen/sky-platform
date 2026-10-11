from copy import deepcopy
from unittest.mock import Mock

import pytest

from adapters.aws.ecs_application_control import AwsEcsApplicationControl


def fixture():
    prefix = "arn:aws:ecs:ap-northeast-2:111111111111:"
    task, service = prefix + "task/cluster/task", prefix + "service/cluster/service"
    role = "arn:aws:iam::111111111111:role/owned-app"
    client = Mock()
    snapshot = {
        "serviceArn": service,
        "serviceName": "service",
        "status": "ACTIVE",
        "desiredCount": 1,
        "runningCount": 1,
        "pendingCount": 0,
        "taskDefinition": prefix + "task-definition/app:1",
        "deployments": [{"rolloutState": "COMPLETED"}],
        "tags": [
            {"key": key, "value": value}
            for key, value in {
                "sky-managed": "true",
                "sky-application": "app",
                "sky-organization": "org",
                "sky-traffic-controller": "blue-green-v1",
            }.items()
        ],
    }
    current = {
        "taskArn": task,
        "lastStatus": "RUNNING",
        "taskDefinitionArn": snapshot["taskDefinition"],
        "containers": [{"imageDigest": "sha256:" + "a" * 64}],
        "attachments": [
            {
                "type": "ElasticNetworkInterface",
                "details": [{"name": "privateIPv4Address", "value": "10.0.1.2"}],
            }
        ],
    }
    stopped = []
    client.describe_services.return_value = {"services": [snapshot]}
    client.list_tasks.side_effect = lambda **kwargs: {
        "taskArns": [task]
        if kwargs["desiredStatus"] == "RUNNING"
        else [value["taskArn"] for value in stopped]
    }
    client.describe_tasks.side_effect = lambda **kwargs: {
        "tasks": [current] if kwargs["tasks"] == [task] else stopped
    }
    client.describe_task_definition.return_value = {"taskDefinition": {"taskRoleArn": role}}
    registry = AwsEcsApplicationControl(
        account_id="111111111111",
        region="ap-northeast-2",
        cluster_arn=prefix + "cluster/cluster",
        service_arn=service,
        task_arn=task,
        task_role_arn=role,
        image_digest="sha256:" + "a" * 64,
        application_id="app",
        organization_id="org",
        client=client,
        sts=Mock(get_caller_identity=Mock(return_value={"Account": "111111111111"})),
    )
    return registry, snapshot, current, stopped


def test_exact_single_writer_task_address_is_audited():
    registry, _, _, stopped = fixture()
    assert registry.audit()["address"] == "10.0.1.2"
    stopped.append({"taskArn": "old-task", "lastStatus": "STOPPED"})
    assert registry.audit()["single_running_writer"] is True


@pytest.mark.parametrize("failure", ["old_writer", "image", "replicas", "owner", "address_changed"])
def test_replaced_foreign_or_undrained_writers_are_rejected(failure):
    registry, service, task, stopped = fixture()
    registry.audit()
    if failure == "old_writer":
        stopped.append({**deepcopy(task), "taskArn": "old-task", "lastStatus": "STOPPING"})
    elif failure == "image":
        task["containers"][0]["imageDigest"] = "sha256:" + "b" * 64
    elif failure == "replicas":
        service["desiredCount"] = 2
    elif failure == "owner":
        service["tags"] = []
    else:
        task["attachments"][0]["details"][0]["value"] = "10.0.1.3"
    with pytest.raises(ValueError):
        registry.audit()
