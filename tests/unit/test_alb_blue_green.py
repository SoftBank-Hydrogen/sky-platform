from copy import deepcopy
from unittest.mock import Mock

import pytest

from adapters.aws.alb_blue_green import AwsAlbBlueGreenRouter
from tests.unit.test_blue_green_traffic import setup


def fixture():
    _, plan, _, _, _, _, _, _, _ = setup()
    prefix = "arn:aws:elasticloadbalancing:ap-northeast-2:111111111111:"
    listener, lb = prefix + "listener/app/owned/id/listener", prefix + "loadbalancer/app/owned/id"
    groups = [prefix + "targetgroup/blue/id", prefix + "targetgroup/green/id"]
    tags = {
        "sky-managed": "true",
        "sky-application": "app",
        "sky-organization": "org",
        "sky-traffic-controller": "blue-green-v1",
    }
    client = Mock()
    client.describe_tags.return_value = {
        "TagDescriptions": [
            {"ResourceArn": arn, "Tags": [{"Key": key, "Value": value} for key, value in tags.items()]}
            for arn in [listener, lb, *groups]
        ]
    }
    current = {
        "ListenerArn": listener,
        "LoadBalancerArn": lb,
        "Protocol": "HTTPS",
        "DefaultActions": [{"Type": "forward", "TargetGroupArn": groups[0]}],
    }
    client.describe_listeners.side_effect = lambda **kwargs: {"Listeners": [deepcopy(current)]}
    client.describe_rules.return_value = {"Rules": [{"IsDefault": True}]}
    client.describe_load_balancers.return_value = {
        "LoadBalancers": [{"Type": "application", "State": {"Code": "active"}, "VpcId": "vpc-owned"}]
    }
    client.describe_target_groups.return_value = {
        "TargetGroups": [
            {
                "TargetGroupArn": group,
                "TargetType": "ip",
                "Protocol": "HTTP",
                "VpcId": "vpc-owned",
                "LoadBalancerArns": [lb],
            }
            for group in groups
        ]
    }
    client.describe_target_health.return_value = {
        "TargetHealthDescriptions": [{"TargetHealth": {"State": "healthy"}}]
    }

    def modify(**kwargs):
        current["DefaultActions"] = kwargs["DefaultActions"]

    client.modify_listener.side_effect = modify
    probe = Mock(
        side_effect=lambda binding: {"http_status": 200, "binding": binding, "revision": "sha256:" + "a" * 64}
    )
    router = AwsAlbBlueGreenRouter(
        account_id="111111111111",
        region="ap-northeast-2",
        application_id="app",
        organization_id="org",
        listener_arn=listener,
        load_balancer_arn=lb,
        source=plan["source"],
        target=plan["target"],
        blue_group=groups[0],
        green_group=groups[1],
        blue_revision="sha256:" + "a" * 64,
        green_revision="sha256:" + "a" * 64,
        guard=lambda: None,
        entrypoint_probe=probe,
        client=client,
        sts=Mock(get_caller_identity=Mock(return_value={"Account": "111111111111"})),
    )
    return router, client, current, plan, probe


def test_owned_listener_switches_exactly_one_group_and_verifies_stable_read():
    router, client, current, plan, probe = fixture()
    assert router.current() == plan["source"]
    receipt = router.switch(plan["source"], plan["target"])
    assert current["DefaultActions"] == [
        {
            "Type": "forward",
            "ForwardConfig": {
                "TargetGroups": [
                    {"TargetGroupArn": router.groups[0], "Weight": 0},
                    {"TargetGroupArn": router.groups[1], "Weight": 100},
                ],
                "TargetGroupStickinessConfig": {"Enabled": False},
            },
        }
    ]
    assert receipt["binding"] == plan["target"]
    assert router.probe(plan["target"])["http_status"] == 200
    client.modify_listener.assert_called_once()
    probe.assert_called_once_with(plan["target"])


@pytest.mark.parametrize("failure", ["tags", "rules", "health", "route", "account", "weighted"])
def test_unowned_unsupported_or_unhealthy_routes_never_mutate_listener(failure):
    router, client, current, plan, _ = fixture()
    if failure == "tags":
        client.describe_tags.return_value["TagDescriptions"][0]["Tags"] = []
    elif failure == "rules":
        client.describe_rules.return_value["Rules"].append({"IsDefault": False})
    elif failure == "health":
        client.describe_target_health.return_value = {"TargetHealthDescriptions": []}
    elif failure == "route":
        current["DefaultActions"][0]["TargetGroupArn"] = router.groups[1]
    elif failure == "account":
        router.sts.get_caller_identity.return_value = {"Account": "222222222222"}
    else:
        current["DefaultActions"][0]["ForwardConfig"] = {
            "TargetGroups": [
                {"TargetGroupArn": router.groups[0], "Weight": 1},
                {"TargetGroupArn": router.groups[1], "Weight": 1},
            ]
        }
    with pytest.raises(ValueError):
        router.switch(plan["source"], plan["target"])
    client.modify_listener.assert_not_called()


def test_unknown_modify_response_not_retried():
    router, client, _, plan, _ = fixture()
    client.modify_listener.side_effect = OSError("unknown AWS result")
    with pytest.raises(RuntimeError, match="reconciliation"):
        router.switch(plan["source"], plan["target"])
    with pytest.raises(RuntimeError, match="reconciliation"):
        router.switch(plan["source"], plan["target"])
    client.modify_listener.assert_called_once()


def test_wrong_revision_at_stable_address_is_not_a_successful_probe():
    router, _, _, plan, probe = fixture()
    router.switch(plan["source"], plan["target"])
    probe.side_effect = None
    probe.return_value = {"http_status": 200, "binding": plan["target"], "revision": "sha256:" + "b" * 64}
    with pytest.raises(ValueError, match="revision"):
        router.probe(plan["target"])
