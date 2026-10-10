"""Switch an explicitly owned ALB HTTPS listener between two registered groups.

This does not provision ECS/ALB or alter an Express-managed listener. Exclusive
execution ownership is mandatory: ModifyListener has no remote CAS token, so
the preflight check cannot fence an independent administrator. Unknown changes
must be reconciled; never retry a listener mutation automatically.
"""

import re
from copy import deepcopy


class AwsAlbBlueGreenRouter:
    def __init__(
        self,
        *,
        account_id,
        region,
        application_id,
        organization_id,
        listener_arn,
        load_balancer_arn,
        source,
        target,
        blue_group,
        green_group,
        blue_revision,
        green_revision,
        guard,
        entrypoint_probe,
        client=None,
        sts=None,
    ):
        self.account, self.region = account_id, region
        prefix = f"arn:aws:elasticloadbalancing:{region}:{account_id}:"
        if (
            not re.fullmatch(r"[0-9]{12}", account_id)
            or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", region)
            or not listener_arn.startswith(prefix + "listener/app/")
            or not load_balancer_arn.startswith(prefix + "loadbalancer/app/")
            or not all(group.startswith(prefix + "targetgroup/") for group in (blue_group, green_group))
            or blue_group == green_group
            or source == target
            or any(
                not isinstance(revision, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", revision)
                for revision in (blue_revision, green_revision)
            )
            or any(
                binding.get("account_id") != account_id
                or binding.get("region") != region
                or binding.get("organization_id") != organization_id
                or binding.get("application_id") != application_id
                for binding in (source, target)
            )
        ):
            raise ValueError("Exact owned ALB and application registration required")
        if client is None or sts is None:
            import boto3
            from botocore.config import Config

            client = client or boto3.client(
                "elbv2", region_name=region, config=Config(retries={"total_max_attempts": 1})
            )
            sts = sts or boto3.client("sts", region_name=region)
        self.client, self.sts, self.guard, self.entrypoint_probe = client, sts, guard, entrypoint_probe
        self.listener, self.load_balancer = listener_arn, load_balancer_arn
        self.bindings, self.groups = [deepcopy(source), deepcopy(target)], [blue_group, green_group]
        self.revisions = [blue_revision, green_revision]
        self.tags = {
            "sky-managed": "true",
            "sky-application": application_id,
            "sky-organization": organization_id,
            "sky-traffic-controller": "blue-green-v1",
        }
        self.uncertain = False

    def _audit(self):
        self.guard()
        if self.sts.get_caller_identity()["Account"] != self.account:
            raise ValueError("ALB caller account differs from registration")
        resources = [self.listener, self.load_balancer, *self.groups]
        descriptions = self.client.describe_tags(ResourceArns=resources)["TagDescriptions"]
        tags = {
            item["ResourceArn"]: {tag["Key"]: tag["Value"] for tag in item["Tags"]} for item in descriptions
        }
        if any(
            any(tags.get(arn, {}).get(key) != value for key, value in self.tags.items()) for arn in resources
        ):
            raise ValueError("ALB resource ownership differs from registration")
        listeners = self.client.describe_listeners(ListenerArns=[self.listener])["Listeners"]
        balancers = self.client.describe_load_balancers(LoadBalancerArns=[self.load_balancer])[
            "LoadBalancers"
        ]
        if (
            len(balancers) != 1
            or balancers[0].get("Type") != "application"
            or balancers[0].get("State", {}).get("Code") != "active"
        ):
            raise ValueError("Registered active application load balancer required")
        if (
            len(listeners) != 1
            or listeners[0]["LoadBalancerArn"] != self.load_balancer
            or listeners[0]["Protocol"] != "HTTPS"
        ):
            raise ValueError("Registered HTTPS listener required")
        rules = self.client.describe_rules(ListenerArn=self.listener)
        if (
            rules.get("NextMarker")
            or len(rules["Rules"]) != 1
            or rules["Rules"][0].get("IsDefault") is not True
        ):
            raise ValueError("Additional listener rules are unsupported")
        groups = self.client.describe_target_groups(TargetGroupArns=self.groups)["TargetGroups"]
        if (
            len(groups) != 2
            or {group["TargetGroupArn"] for group in groups} != set(self.groups)
            or any(
                group.get("TargetType") != "ip" or group.get("Protocol") not in {"HTTP", "HTTPS"}
                for group in groups
            )
            or len({group["VpcId"] for group in groups}) != 1
            or any(
                group["VpcId"] != balancers[0]["VpcId"]
                or self.load_balancer not in group.get("LoadBalancerArns", [])
                for group in groups
            )
        ):
            raise ValueError("Registered same-VPC IP target groups required")
        actions = listeners[0]["DefaultActions"]
        if len(actions) != 1 or actions[0].get("Type") != "forward":
            raise ValueError("Exclusive single-target forward action required")
        action = actions[0]
        if set(action) - {"Type", "TargetGroupArn", "Order", "ForwardConfig"}:
            raise ValueError("Unsupported listener action configuration")
        group = action.get("TargetGroupArn")
        if "ForwardConfig" in action:
            config = action["ForwardConfig"]
            forwards = config.get("TargetGroups", [])
            if config.get("TargetGroupStickinessConfig", {}).get("Enabled", False):
                raise ValueError("Sticky routing is unsupported")
            if len(forwards) == 1 and forwards[0].get("Weight", 1) == 1:
                selected = forwards[0].get("TargetGroupArn")
            elif (
                len(forwards) == 2
                and {entry.get("TargetGroupArn") for entry in forwards} == set(self.groups)
                and all(type(entry.get("Weight")) is int for entry in forwards)
                and sorted(entry["Weight"] for entry in forwards) == [0, 100]
            ):
                selected = next(entry["TargetGroupArn"] for entry in forwards if entry["Weight"] == 100)
            else:
                raise ValueError("Only exclusive 100/0 Blue/Green routing is supported")
            if group is not None and group != selected:
                raise ValueError("Conflicting listener target configuration")
            group = selected
        if group not in self.groups:
            raise ValueError("Listener points to an unregistered target")
        self.guard()
        return deepcopy(self.bindings[self.groups.index(group)])

    def current(self):
        return self._audit()

    def switch(self, expected_binding, target_binding):
        if self.uncertain:
            raise RuntimeError("Listener outcome requires reconciliation")
        if target_binding not in self.bindings or expected_binding == target_binding:
            raise ValueError("Registered distinct target binding required")
        if self._audit() != expected_binding:
            raise ValueError("Listener changed before traffic switch")
        group = self.groups[self.bindings.index(target_binding)]
        health = self.client.describe_target_health(TargetGroupArn=group)["TargetHealthDescriptions"]
        if not health or any(item["TargetHealth"]["State"] != "healthy" for item in health):
            raise ValueError("Green target group must be healthy before switching")
        self.guard()
        if self._audit() != expected_binding:
            raise ValueError("Listener changed during preflight")
        self.uncertain = True
        try:
            self.client.modify_listener(
                ListenerArn=self.listener,
                DefaultActions=[
                    {
                        "Type": "forward",
                        "ForwardConfig": {
                            "TargetGroups": [
                                {"TargetGroupArn": arn, "Weight": 100 if arn == group else 0}
                                for arn in self.groups
                            ],
                            "TargetGroupStickinessConfig": {"Enabled": False},
                        },
                    }
                ],
            )
            if self._audit() != target_binding:
                raise ValueError("Listener switch was not observed")
        except Exception:  # noqa: BLE001 -- do not replay an uncertain AWS mutation
            raise RuntimeError("Listener outcome requires reconciliation") from None
        self.uncertain = False
        return {
            "strategy": "blue_green",
            "listener_arn": self.listener,
            "target_group_arn": group,
            "binding": deepcopy(target_binding),
        }

    def probe(self, expected_binding):
        if self.uncertain or self._audit() != expected_binding:
            raise ValueError("Confirmed listener route required")
        receipt = self.entrypoint_probe(expected_binding)
        if (
            not isinstance(receipt, dict)
            or set(receipt) != {"http_status", "binding", "revision"}
            or type(receipt["http_status"]) is not int
            or receipt["http_status"] != 200
            or receipt["binding"] != expected_binding
            or not isinstance(receipt["revision"], str)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", receipt["revision"])
            or receipt["revision"] != self.revisions[self.bindings.index(expected_binding)]
        ):
            raise ValueError("Actual stable-entrypoint revision and binding read required")
        self.guard()
        return receipt
