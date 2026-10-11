"""Read-only ownership audit for a registered single-task application controller.

A stopped/replaced/draining task invalidates the registration. Old non-stopped
writers are rejected as well. This does not infer writer instrumentation from
an ECS image; the app gate and database fencing remain separate contracts.
"""

import ipaddress
import re


class AwsEcsApplicationControl:
    def __init__(
        self,
        *,
        account_id,
        region,
        cluster_arn,
        service_arn,
        task_arn,
        task_role_arn,
        image_digest,
        application_id,
        organization_id,
        client,
        sts,
    ):
        prefix = f"arn:aws:ecs:{region}:{account_id}:"
        if (
            not re.fullmatch(r"\d{12}", account_id)
            or not cluster_arn.startswith(prefix + "cluster/")
            or not service_arn.startswith(prefix + "service/")
            or not task_arn.startswith(prefix + "task/")
            or not task_role_arn.startswith(f"arn:aws:iam::{account_id}:role/")
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_digest)
        ):
            raise ValueError("Exact single-task application registration required")
        self.account, self.region = account_id, region
        self.cluster, self.service, self.task = cluster_arn, service_arn, task_arn
        self.role, self.digest = task_role_arn, image_digest
        self.application, self.organization = application_id, organization_id
        self.client, self.sts = client, sts
        self.address = None

    def audit(self):
        if self.sts.get_caller_identity()["Account"] != self.account:
            raise ValueError("Controller audit caller account differs from registration")
        result = self.client.describe_services(
            cluster=self.cluster, services=[self.service], include=["TAGS"]
        )
        if result.get("failures") or len(result.get("services", [])) != 1:
            raise ValueError("Registered service could not be inspected")
        service = result["services"][0]
        tags = {tag["key"]: tag["value"] for tag in service.get("tags", [])}
        if (
            service.get("serviceArn") != self.service
            or service.get("status") != "ACTIVE"
            or service.get("desiredCount") != 1
            or service.get("runningCount") != 1
            or service.get("pendingCount") != 0
            or len(service.get("deployments", [])) != 1
            or service["deployments"][0].get("rolloutState") != "COMPLETED"
            or any(
                tags.get(key) != value
                for key, value in {
                    "sky-managed": "true",
                    "sky-application": self.application,
                    "sky-organization": self.organization,
                    "sky-traffic-controller": "blue-green-v1",
                }.items()
            )
        ):
            raise ValueError("Owned stable single-task service required")
        tasks = []
        for desired in ("RUNNING", "STOPPED"):
            listed = self.client.list_tasks(
                cluster=self.cluster, serviceName=service["serviceName"], desiredStatus=desired
            )
            if listed.get("nextToken") or len(listed.get("taskArns", [])) > 100:
                raise ValueError("Complete bounded writer inventory required")
            if listed.get("taskArns"):
                described = self.client.describe_tasks(cluster=self.cluster, tasks=listed["taskArns"])
                if described.get("failures"):
                    raise ValueError("Writer inventory could not be verified")
                tasks.extend(described["tasks"])
        active = [task for task in tasks if task.get("lastStatus") != "STOPPED"]
        if (
            len(active) != 1
            or active[0].get("taskArn") != self.task
            or active[0].get("lastStatus") != "RUNNING"
        ):
            raise ValueError("Replaced or undrained writers invalidate application control")
        task = active[0]
        definition = self.client.describe_task_definition(taskDefinition=task["taskDefinitionArn"])[
            "taskDefinition"
        ]
        if (
            definition.get("taskRoleArn") != self.role
            or task["taskDefinitionArn"] != service["taskDefinition"]
            or len(task.get("containers", [])) != 1
            or task["containers"][0].get("imageDigest") != self.digest
        ):
            raise ValueError("Controller task role/image differs from registration")
        addresses = [
            detail["value"]
            for attachment in task.get("attachments", [])
            if attachment.get("type") == "ElasticNetworkInterface"
            for detail in attachment.get("details", [])
            if detail.get("name") == "privateIPv4Address"
        ]
        if len(addresses) != 1 or not ipaddress.ip_address(addresses[0]).is_private:
            raise ValueError("Exact private controller task address required")
        if self.address is not None and self.address != addresses[0]:
            raise ValueError("Controller address changed; explicit registration required")
        self.address = addresses[0]
        return {
            "task_arn": self.task,
            "service_arn": self.service,
            "address": self.address,
            "image_digest": self.digest,
            "single_running_writer": True,
        }
