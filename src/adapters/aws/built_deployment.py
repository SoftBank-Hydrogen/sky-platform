"""Deploy an approved, verified image without Docker or infrastructure mutations."""
import re
import urllib.request

from adapters.github.remote_build import NoRedirect
from ports.remote_builds import digest, validate_request


class BuiltImageDeployment:
    def __init__(self, settings, *, ecs=None, stacks=None, identity=None, probe=None):
        self.settings = settings
        if any(client is None for client in (ecs, stacks, identity)):
            import boto3
            from botocore.config import Config
            config = Config(connect_timeout=5, read_timeout=20, retries={"total_max_attempts": 1})
            ecs = ecs or boto3.client("ecs", region_name=settings.region, config=config)
            stacks = stacks or boto3.client("cloudformation", region_name=settings.region, config=config)
            identity = identity or boto3.client("sts", region_name=settings.region, config=config)
        self.ecs, self.stacks, self.identity = ecs, stacks, identity
        self.probe = probe or self.http_probe

    @staticmethod
    def http_probe(url):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(url, timeout=5) as response:
            if response.status != 200:
                raise OSError("Deployment HTTP verification unavailable")

    def prepare(self, request, result):
        from botocore.exceptions import BotoCoreError, ClientError
        artifact, plan = validate_request(request)
        if (request["account_id"], request["region"]) != (self.settings.account_id, self.settings.region):
            raise ValueError("Deployment account scope changed")
        if (plan.get("required_env") or plan.get("replicas") != 1
                or type(plan.get("port")) is not int or not 1024 <= plan["port"] <= 65535
                or not isinstance(plan.get("health_path"), str)
                or len(plan["health_path"]) > 200
                or not re.fullmatch(r"/[A-Za-z0-9/_.-]*", plan["health_path"])
                or ".." in plan["health_path"] or "//" in plan["health_path"]):
            raise ValueError("Deployment requires a bounded, environment-free HTTP plan")
        from ports.remote_builds import validate_result
        if type(result.get("run_id")) is not int or result["run_id"] < 1:
            raise ValueError("Verified workflow run identity required")
        validate_result(result, request, result["run_id"])
        try:
            if self.identity.get_caller_identity()["Account"] != self.settings.account_id:
                raise ValueError("Deployment caller account changed")
            stacks = self.stacks.describe_stacks(StackName="sky-core")["Stacks"]
        except (BotoCoreError, ClientError):
            raise OSError("Deployment infrastructure inspection unavailable") from None
        if (len(stacks) != 1 or stacks[0].get("StackStatus") not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
                or {x["Key"]: x["Value"] for x in stacks[0].get("Tags", [])}.get("sky-managed") != "true"):
            raise ValueError("Managed core stack is not ready")
        outputs = {x["OutputKey"]: x["OutputValue"] for x in stacks[0].get("Outputs", [])}
        if outputs.get("RepositoryUri") != result["image"].split("@")[0]:
            raise ValueError("Managed repository changed")
        roles = [outputs.get(k, "") for k in ("ExecutionRoleArn", "InfrastructureRoleArn")]
        for role, logical in zip(roles, ("ExecutionRole", "InfrastructureRole")):
            if not re.fullmatch(r"arn:aws:iam::" + self.settings.account_id + r":role/sky-core-" + logical + r"-[A-Za-z0-9]+", role):
                raise ValueError("Managed core role changed")
        service = "sky-b-" + request["build_id"].replace("-", "")
        arn = f"arn:aws:ecs:{self.settings.region}:{self.settings.account_id}:service/default/{service}"
        tags = {"sky-managed": "true", "sky-operation": request["build_id"],
                "sky-workspace": request["workspace"], "sky-application": artifact.application_id,
                "sky-organization": artifact.organization_id}
        payload = {"serviceName": service, "cluster": "default", "executionRoleArn": roles[0],
                   "infrastructureRoleArn": roles[1], "cpuArchitecture": "X86_64",
                   "healthCheckPath": plan["health_path"],
                   "primaryContainer": {"image": result["image"], "containerPort": plan["port"],
                                        "environment": [{"name": "PORT", "value": str(plan["port"])}]},
                   "scalingTarget": {"minTaskCount": 1, "maxTaskCount": 1},
                   "tags": [{"key": k, "value": v} for k, v in tags.items()]}
        # No create retries: Express has no clientToken. An uncertain request is parked by the caller.
        try:
            self.ecs.describe_express_gateway_service(serviceArn=arn, include=["TAGS"])
        except ClientError as error:
            if error.response["Error"]["Code"] != "ResourceNotFoundException":
                raise OSError("Deployment identity inspection unavailable") from None
        except BotoCoreError:
            raise OSError("Deployment identity inspection unavailable") from None
        else:
            raise ValueError("Deployment identity already exists; reconciliation required")
        return {"kind": "ecs_deployment", "service_arn": arn, "payload": payload,
                "request_digest": digest(request), "image": result["image"]}

    def create(self, intent):
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            service = self.ecs.create_express_gateway_service(**intent["payload"])["service"]
        except (BotoCoreError, ClientError):
            raise OSError("Deployment submission requires reconciliation") from None
        if service.get("serviceArn") != intent["service_arn"]:
            raise ValueError("Deployment submission identity changed")

    def observe(self, intent):
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            service = self.ecs.describe_express_gateway_service(serviceArn=intent["service_arn"], include=["TAGS"])["service"]
            tags = {x["key"]: x["value"] for x in service.get("tags", [])}
            expected = {x["key"]: x["value"] for x in intent["payload"]["tags"]}
            if service.get("serviceArn") != intent["service_arn"] or any(tags.get(k) != v for k, v in expected.items()):
                raise ValueError("Deployment ownership changed")
            status = service.get("status", {}).get("statusCode")
            if status in {"FAILED", "INACTIVE"}:
                raise ValueError("Deployment is not healthy; reconciliation required")
            if status != "ACTIVE" or service.get("currentDeployment"):
                return None
            configs = service.get("activeConfigurations", [])
            if len(configs) != 1:
                return None
            c, payload = configs[0], intent["payload"]
            container = c.get("primaryContainer", {})
            if (any(container.get(k) != v for k, v in payload["primaryContainer"].items())
                    or container.get("secrets") or container.get("command") or container.get("repositoryCredentials")
                    or any(c.get(k) != payload[k] for k in ("executionRoleArn", "healthCheckPath"))
                    or any(c.get("scalingTarget", {}).get(k) != v for k, v in payload["scalingTarget"].items())
                    or c.get("taskRoleArn")):
                raise ValueError("Deployment configuration differs from approval")
            task = c.get("taskDefinitionArn", "")
            if not re.fullmatch(r"arn:aws:ecs:" + self.settings.region + ":" + self.settings.account_id + r":task-definition/[A-Za-z0-9_-]+:[0-9]+", task):
                raise ValueError("Deployment task definition changed")
            containers = self.ecs.describe_task_definition(taskDefinition=task)["taskDefinition"].get("containerDefinitions", [])
            if len(containers) != 1 or containers[0].get("name") != "Main" or containers[0].get("image") != intent["image"]:
                raise ValueError("Deployment image differs from verified build")
            paths = [p["endpoint"] for p in c.get("ingressPaths", []) if p.get("accessType") == "PUBLIC" and p.get("endpoint")]
            if len(paths) != 1:
                return None
            url = paths[0].rstrip("/")
            if not url.startswith("https://"):
                url = "https://" + url
            from urllib.parse import urlsplit
            parsed = urlsplit(url)
            hostname = parsed.hostname or ""
            label = hostname.removesuffix(f".ecs.{self.settings.region}.on.aws")
            if (parsed.scheme != "https" or label == hostname
                    or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", label)
                    or parsed.username or parsed.password or parsed.port
                    or parsed.path or parsed.query or parsed.fragment):
                raise ValueError("Deployment endpoint is outside ECS Express")
            self.probe(url + payload["healthCheckPath"])
            return {"target": "aws-ecs-express", "service": payload["serviceName"], "service_arn": intent["service_arn"],
                    "url": url, "health_url": url + payload["healthCheckPath"], "public": True,
                    "image": intent["image"], "image_digest": intent["image"].split("@")[1],
                    "task_definition_arn": task, "account": self.settings.account_id, "region": self.settings.region,
                    "verification": {"http": "verified", "image": "verified"}}
        except (BotoCoreError, ClientError):
            raise OSError("Deployment observation unavailable") from None
