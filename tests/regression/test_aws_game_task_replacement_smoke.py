"""The opt-in live drill must identify the disposable service before task replacement."""

import json
import unittest
from types import SimpleNamespace

from tests.live.smoke_aws_game_task_replacement import owned_task


JOB = "a" * 16
ATTEMPT = JOB + "-a1"
SERVICE = "sky-" + ATTEMPT
ARN = "arn:aws:ecs:ap-northeast-2:265233844540:service/default/" + SERVICE
TASK = "arn:aws:ecs:ap-northeast-2:265233844540:task/default/" + "b" * 32
DEFINITION = "arn:aws:ecs:ap-northeast-2:265233844540:task-definition/game:1"


class FakeAdapter:
    def __init__(self, *, task_group=None, tags=None):
        self.settings = SimpleNamespace(region="ap-northeast-2", expected_account="265233844540")
        self.calls = []
        self.task_group = task_group or "service:" + SERVICE
        self.tags = tags or [
            {"key": "sky-managed", "value": "true"},
            {"key": "sky-attempt", "value": ATTEMPT},
        ]

    def validate_url(self, *_args):
        pass

    def aws(self, args, **_kwargs):
        self.calls.append(args)
        if args[1] == "describe-express-gateway-service":
            return json.dumps(
                {"service": {"serviceArn": ARN, "status": {"statusCode": "ACTIVE"}, "tags": self.tags}}
            )
        if args[1] == "list-tasks":
            return json.dumps({"taskArns": [TASK]})
        if args[1] == "describe-tasks":
            return json.dumps(
                {
                    "tasks": [
                        {
                            "taskArn": TASK,
                            "taskDefinitionArn": DEFINITION,
                            "group": self.task_group,
                            "lastStatus": "RUNNING",
                        }
                    ]
                }
            )
        raise AssertionError("Unexpected AWS command: " + str(args))


class GameTaskReplacementSmokeTests(unittest.TestCase):
    def setUp(self):
        self.result = {
            "owner_attempt": ATTEMPT,
            "service": SERVICE,
            "service_arn": ARN,
            "account": "265233844540",
            "region": "ap-northeast-2",
            "url": "https://example.com",
            "task_definition_arn": DEFINITION,
        }

    def test_preflight_accepts_only_the_owned_single_service_task(self):
        adapter = FakeAdapter()
        self.assertEqual(owned_task(adapter, self.result, JOB), TASK)
        self.assertEqual(
            [call[1] for call in adapter.calls],
            ["describe-express-gateway-service", "list-tasks", "describe-tasks"],
        )

    def test_wrong_service_tag_or_task_group_blocks_replacement(self):
        for adapter in (
            FakeAdapter(tags=[{"key": "sky-managed", "value": "true"}]),
            FakeAdapter(task_group="service:other"),
        ):
            with self.subTest(adapter=adapter):
                with self.assertRaises(AssertionError):
                    owned_task(adapter, self.result, JOB)
                self.assertFalse(any(call[1] == "stop-task" for call in adapter.calls))

    def test_job_id_mismatch_stops_before_aws_calls(self):
        adapter = FakeAdapter()
        with self.assertRaises(AssertionError):
            owned_task(adapter, self.result, "c" * 16)
        self.assertEqual(adapter.calls, [])
