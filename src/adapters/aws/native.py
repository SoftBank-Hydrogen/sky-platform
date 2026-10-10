"""Explicit AWS native backends. No automatic lowering or user-code execution here.

Receipts are checkpointed before AWS mutations. Retry means reconcile/verify, never
silently create another release. Lambda supports dependency-free Python handlers;
EC2 accepts an already built, immutable private ECR image in an existing public subnet.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import io
import json
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

from adapters.aws.ecs import AwsConfigurationError, AwsSettings
from adapters.aws.role_boundary import managed_role_boundary


def lambda_bundle(project: Path, handler: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", handler):
        raise ValueError("Handler must be module.function")
    module, function = handler.rsplit(".", 1)
    entry = project / (module.replace(".", "/") + ".py")
    files = sorted(project.rglob("*"))
    if any(p.is_symlink() for p in files):
        raise ValueError("Symbolic links are not supported")
    files = [p for p in files if p.is_file()]
    if not files or len(files) > 500 or sum(p.stat().st_size for p in files) > 20 * 1024**2:
        raise ValueError("Bundle exceeds 500 files or 20 MiB")
    if not entry.is_file():
        raise ValueError("Handler module is absent")
    tree = ast.parse(entry.read_text())
    if not any(
        isinstance(n, ast.FunctionDef) and n.name == function and len(n.args.args) == 2 for n in tree.body
    ):
        raise ValueError("A synchronous handler(event, context) is required")
    local = {p.relative_to(project).parts[0].removesuffix(".py") for p in files}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            relative = path.relative_to(project).as_posix()
            if path.suffix != ".py":
                raise ValueError(
                    "This profile accepts Python source only; dependencies/data require another backend"
                )
            content = path.read_bytes()
            for node in ast.walk(ast.parse(content)):
                imports = (
                    [n.name for n in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                    if isinstance(node, ast.ImportFrom) and not node.level
                    else []
                )
                if any(name.split(".")[0] not in sys.stdlib_module_names | local for name in imports):
                    raise ValueError("External Python dependencies are not supported by this ZIP profile")
            info = zipfile.ZipInfo(relative, (1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            bundle.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED)
    return output.getvalue()


class NativeAwsAdapter:
    backend = ""

    def __init__(self, settings: AwsSettings, *, clients=None, checkpoint=None, probe=None):
        settings.validate()
        if not settings.expected_account:
            raise ValueError("AWS account pin is required")
        self.settings = settings
        self.boundary = managed_role_boundary(settings.expected_account)
        if not self.boundary:
            raise ValueError("SKY_AWS_ROLE_BOUNDARY_ARN is required for native backends")
        self.clients = clients
        self.checkpoint = checkpoint or (lambda receipt: None)
        self.probe = probe or self._probe

    def client(self, name):
        if self.clients is not None:
            return self.clients[name]
        import boto3

        if not hasattr(self, "_session"):
            self._session = boto3.Session(region_name=self.settings.region)
        return self._session.client(name)

    def identity(self):
        if self.client("sts").get_caller_identity()["Account"] != self.settings.expected_account:
            raise AwsConfigurationError("AWS account differs from pinned account")

    def start(self, application_id, attempt_id, template, artifact_digest):
        self.identity()
        if not re.fullmatch(r"[a-z][a-z0-9-]{2,30}", application_id):
            raise ValueError("Invalid application ID")
        if not re.fullmatch(r"[a-f0-9]{16}-a[1-3]", attempt_id):
            raise ValueError("Invalid attempt ID")
        name = f"sky-{self.backend}-{attempt_id}"
        request_digest = hashlib.sha256(json.dumps(template, sort_keys=True).encode()).hexdigest()
        receipt = {
            "target": "aws-" + self.backend,
            "application_id": application_id,
            "attempt_id": attempt_id,
            "stack_name": name,
            "account": self.settings.expected_account,
            "region": self.settings.region,
            "artifact_digest": artifact_digest,
            "request_digest": request_digest,
            "status": "creating",
        }
        self.checkpoint(dict(receipt))
        # No describe-or-create on generic errors: an uncertain create must be recovered by receipt.
        result = self.client("cloudformation").create_stack(
            StackName=name,
            TemplateBody=json.dumps(template),
            Capabilities=["CAPABILITY_IAM"],
            ClientRequestToken=attempt_id,
            Tags=[{"Key": k, "Value": v} for k, v in self.tags(receipt).items()],
        )
        receipt["stack_id"] = result["StackId"]
        self._arn(receipt)
        self.checkpoint(dict(receipt))
        self.client("cloudformation").get_waiter("stack_create_complete").wait(StackName=receipt["stack_id"])
        self.owned(receipt)
        return receipt

    @staticmethod
    def tags(receipt):
        return {
            "sky-managed": "true",
            "sky-app": receipt["application_id"],
            "sky-attempt": receipt["attempt_id"],
            "sky-request": receipt["request_digest"],
        }

    def _arn(self, receipt):
        prefix = f"arn:aws:cloudformation:{self.settings.region}:{self.settings.expected_account}:stack/"
        if (
            receipt.get("account") != self.settings.expected_account
            or receipt.get("region") != self.settings.region
        ):
            raise ValueError("Receipt belongs to another AWS account/region")
        if not receipt.get("stack_id", "").startswith(prefix + receipt["stack_name"] + "/"):
            raise ValueError("Stack ARN does not match receipt")

    def owned(self, receipt):
        self.identity()
        if not receipt.get("stack_id"):
            recovered = self.client("cloudformation").describe_stacks(StackName=receipt["stack_name"])[
                "Stacks"
            ][0]
            receipt["stack_id"] = recovered["StackId"]
            self._arn(receipt)
            if {t["Key"]: t["Value"] for t in recovered["Tags"]} != self.tags(receipt):
                raise ValueError("Recovery stack ownership differs")
            self.checkpoint(dict(receipt))
        self._arn(receipt)
        stack = self.client("cloudformation").describe_stacks(StackName=receipt["stack_id"])["Stacks"][0]
        if stack["StackId"] != receipt["stack_id"] or {
            t["Key"]: t["Value"] for t in stack["Tags"]
        } != self.tags(receipt):
            raise ValueError("Stack ownership or request identity differs")
        return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}

    def destroy(self, receipt):
        self.owned(receipt)
        receipt = dict(receipt, status="deleting")
        self.checkpoint(receipt)
        self.client("cloudformation").delete_stack(StackName=receipt["stack_id"])
        self.client("cloudformation").get_waiter("stack_delete_complete").wait(StackName=receipt["stack_id"])
        receipt["status"] = "deleted"
        self.checkpoint(receipt)
        return receipt

    @staticmethod
    def _probe(url):
        with urllib.request.urlopen(url, timeout=15) as response:
            if response.status != 200 or response.url != url:
                raise ValueError("HTTP health verification failed or redirected")

    def role(self, service, policies):
        return {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "PermissionsBoundary": self.boundary,
                "AssumeRolePolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {"Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}
                    ],
                },
                "Policies": [
                    {
                        "PolicyName": "SkyRuntime",
                        "PolicyDocument": {"Version": "2012-10-17", "Statement": policies},
                    }
                ],
            },
        }


class AwsLambdaAdapter(NativeAwsAdapter):
    backend = "lambda"

    def deploy(self, project, application_id, attempt_id, *, handler="handler.handler", public_access=False):
        if public_access is not True:
            raise ValueError("Public Function URL requires explicit public-access permission")
        artifact = lambda_bundle(Path(project), handler)
        digest = base64.b64encode(hashlib.sha256(artifact).digest()).decode()
        name = "sky-lambda-" + attempt_id
        log_arn = f"arn:aws:logs:{self.settings.region}:{self.settings.expected_account}:log-group:/aws/lambda/{name}:*"
        template = {
            "Resources": {
                "Role": self.role(
                    "lambda.amazonaws.com",
                    [
                        {
                            "Effect": "Allow",
                            "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                            "Resource": log_arn,
                        }
                    ],
                ),
                "Logs": {
                    "Type": "AWS::Logs::LogGroup",
                    "Properties": {"LogGroupName": "/aws/lambda/" + name, "RetentionInDays": 7},
                },
                "Function": {
                    "Type": "AWS::Lambda::Function",
                    "DependsOn": "Logs",
                    "Properties": {
                        "FunctionName": name,
                        "Runtime": "python3.13",
                        "Handler": "index.handler",
                        "MemorySize": 128,
                        "Timeout": 10,
                        "Role": {"Fn::GetAtt": ["Role", "Arn"]},
                        "Code": {
                            "ZipFile": 'def handler(event, context):\n return {"statusCode":503,"body":"Preparing"}\n'
                        },
                    },
                },
            },
            "Outputs": {"Function": {"Value": {"Ref": "Function"}}},
        }
        receipt = self.start(application_id, attempt_id, template, digest)
        receipt["function"] = name
        receipt["handler"] = handler
        receipt["status"] = "publishing"
        self.checkpoint(dict(receipt))
        api = self.client("lambda")
        api.update_function_configuration(FunctionName=name, Handler=handler)
        api.get_waiter("function_updated_v2").wait(FunctionName=name)
        uploaded = api.update_function_code(FunctionName=name, ZipFile=artifact, Publish=True)
        if uploaded["CodeSha256"] != digest:
            raise ValueError("Uploaded Lambda ZIP differs from inspected bundle")
        receipt["version"] = uploaded["Version"]
        self.checkpoint(dict(receipt))
        api.get_waiter("function_updated_v2").wait(FunctionName=name)
        version = uploaded["Version"]
        api.create_alias(FunctionName=name, Name="live", FunctionVersion=version)
        receipt["version"] = version
        self.checkpoint(dict(receipt))
        api.create_function_url_config(FunctionName=name, Qualifier="live", AuthType="NONE")
        for sid, action, condition in (
            ("SkyUrl", "lambda:InvokeFunctionUrl", {"FunctionUrlAuthType": "NONE"}),
            ("SkyUrlInvoke", "lambda:InvokeFunction", {"InvokedViaFunctionUrl": True}),
        ):
            api.add_permission(
                FunctionName=name,
                Qualifier="live",
                StatementId=sid,
                Action=action,
                Principal="*",
                **condition,
            )
        return self.verify(receipt)

    def verify(self, receipt):
        outputs = self.owned(receipt)
        if outputs.get("Function") != receipt["function"]:
            raise ValueError("Function differs from owned stack")
        api = self.client("lambda")
        configuration = api.get_function(FunctionName=receipt["function"], Qualifier="live")["Configuration"]
        if configuration["CodeSha256"] != receipt["artifact_digest"] or configuration[
            "Version"
        ] != receipt.get("version"):
            raise ValueError("Running Lambda version/digest differs")
        url = api.get_function_url_config(FunctionName=receipt["function"], Qualifier="live")["FunctionUrl"]
        if not re.fullmatch(
            r"https://[a-z0-9]+\.lambda-url\." + re.escape(self.settings.region) + r"\.on\.aws/", url
        ):
            raise ValueError("Unexpected Function URL")
        self.probe(url)
        receipt = dict(receipt, url=url, status="verified", artifact_verified=True, http_verified=True)
        self.checkpoint(receipt)
        return receipt


class AwsEc2Adapter(NativeAwsAdapter):
    backend = "ec2"

    def deploy(
        self, image, application_id, attempt_id, *, subnet_id, port=8080, public_access=False, stateless=False
    ):
        if public_access is not True or stateless is not True:
            raise ValueError("This profile requires explicit public access and a stateless container")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Invalid container port")
        account, region = self.settings.expected_account, self.settings.region
        pattern = rf"{account}\.dkr\.ecr\.{re.escape(region)}\.amazonaws\.com/([a-z0-9/_-]+)@sha256:([a-f0-9]{{64}})"
        match = re.fullmatch(pattern, image)
        if not match:
            raise ValueError("An immutable private ECR image in the pinned account/region is required")
        if not re.fullmatch(r"subnet-[a-f0-9]{8,17}", subnet_id):
            raise ValueError("Invalid subnet")
        self.identity()
        ec2 = self.client("ec2")
        subnet = ec2.describe_subnets(SubnetIds=[subnet_id])["Subnets"][0]
        if subnet["OwnerId"] != account or subnet.get("State") != "available":
            raise ValueError("Subnet is unavailable or belongs to another account")
        routes = ec2.describe_route_tables(
            Filters=[{"Name": "association.subnet-id", "Values": [subnet_id]}]
        )["RouteTables"]
        if not routes:
            routes = ec2.describe_route_tables(
                Filters=[
                    {"Name": "vpc-id", "Values": [subnet["VpcId"]]},
                    {"Name": "association.main", "Values": ["true"]},
                ]
            )["RouteTables"]
        if len(routes) != 1 or not any(
            r.get("DestinationCidrBlock") == "0.0.0.0/0"
            and r.get("State") == "active"
            and r.get("GatewayId", "").startswith("igw-")
            for r in routes[0]["Routes"]
        ):
            raise ValueError("EC2 profile requires an existing public subnet with an internet gateway")
        for attribute in ("enableDnsSupport", "enableDnsHostnames"):
            response = ec2.describe_vpc_attribute(VpcId=subnet["VpcId"], Attribute=attribute)
            if response[attribute[0].upper() + attribute[1:]]["Value"] is not True:
                raise ValueError("EC2 HTTPS origin requires VPC DNS support and hostnames")
        # Validate the exact immutable artifact before allocation; runtime inspection checks architecture.
        detail = self.client("ecr").describe_images(
            repositoryName=match[1], imageIds=[{"imageDigest": "sha256:" + match[2]}]
        )["imageDetails"][0]
        if detail["imageDigest"] != "sha256:" + match[2]:
            raise ValueError("ECR digest differs")
        manifest = self.client("ecr").batch_get_image(
            repositoryName=match[1], imageIds=[{"imageDigest": detail["imageDigest"]}]
        )
        images = manifest.get("images", [])
        if len(images) != 1 or images[0]["imageId"]["imageDigest"] != detail["imageDigest"]:
            raise ValueError("ECR artifact is not readable")
        metadata = json.loads(images[0]["imageManifest"])
        if "manifests" in metadata:
            raise ValueError("This profile requires a single linux/amd64 manifest, not an image index")
        ami = self.client("ssm").get_parameter(
            Name="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
        )["Parameter"]["Value"]
        description = ec2.describe_images(ImageIds=[ami], Owners=["amazon"])["Images"][0]
        if description["Architecture"] != "x86_64" or description["State"] != "available":
            raise ValueError("Amazon Linux amd64 AMI is unavailable")
        prefixes = ec2.describe_managed_prefix_lists(
            Filters=[
                {"Name": "prefix-list-name", "Values": ["com.amazonaws.global.cloudfront.origin-facing"]}
            ]
        )["PrefixLists"]
        if len(prefixes) != 1 or prefixes[0]["OwnerId"] != "AWS":
            raise ValueError("CloudFront origin prefix list is unavailable")
        repository_arn = f"arn:aws:ecr:{region}:{account}:repository/{match[1]}"
        role = self.role(
            "ec2.amazonaws.com",
            [
                {"Effect": "Allow", "Action": ["ecr:GetAuthorizationToken"], "Resource": "*"},
                {
                    "Effect": "Allow",
                    "Action": [
                        "ecr:BatchGetImage",
                        "ecr:GetDownloadUrlForLayer",
                        "ecr:BatchCheckLayerAvailability",
                    ],
                    "Resource": repository_arn,
                },
            ],
        )
        role["Properties"]["ManagedPolicyArns"] = ["arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"]
        # User source never runs as bootstrap, with host networking, or with a Docker socket.
        bootstrap = f"""#!/bin/bash
set -euo pipefail
dnf install -y docker awscli-2 iptables
systemctl enable --now docker
iptables -I DOCKER-USER -d 169.254.169.254/32 -j REJECT
install -d -m 700 /run/sky-ecr
export DOCKER_CONFIG=/run/sky-ecr
aws ecr get-login-password --region {region} | docker login --username AWS --password-stdin {account}.dkr.ecr.{region}.amazonaws.com
docker pull {image}
docker logout {account}.dkr.ecr.{region}.amazonaws.com
rm -rf /run/sky-ecr
docker run -d --name sky-app --restart unless-stopped --read-only --tmpfs /tmp:size=64m --cap-drop ALL --security-opt no-new-privileges --memory 512m --cpus 1 -p 80:{port} {image}
"""
        resources = {
            "Role": role,
            "Profile": {"Type": "AWS::IAM::InstanceProfile", "Properties": {"Roles": [{"Ref": "Role"}]}},
            "Group": {
                "Type": "AWS::EC2::SecurityGroup",
                "Properties": {
                    "GroupDescription": "Sky CloudFront origin only",
                    "VpcId": subnet["VpcId"],
                    "SecurityGroupIngress": [
                        {
                            "IpProtocol": "tcp",
                            "FromPort": 80,
                            "ToPort": 80,
                            "SourcePrefixListId": prefixes[0]["PrefixListId"],
                        }
                    ],
                },
            },
            "Instance": {
                "Type": "AWS::EC2::Instance",
                "Properties": {
                    "ImageId": ami,
                    "InstanceType": "t3.small",
                    "IamInstanceProfile": {"Ref": "Profile"},
                    "MetadataOptions": {"HttpTokens": "required", "HttpPutResponseHopLimit": 1},
                    "BlockDeviceMappings": [
                        {
                            "DeviceName": "/dev/xvda",
                            "Ebs": {
                                "Encrypted": True,
                                "VolumeSize": 8,
                                "VolumeType": "gp3",
                                "DeleteOnTermination": True,
                            },
                        }
                    ],
                    "NetworkInterfaces": [
                        {
                            "DeviceIndex": "0",
                            "AssociatePublicIpAddress": True,
                            "SubnetId": subnet_id,
                            "GroupSet": [{"Ref": "Group"}],
                        }
                    ],
                    "UserData": {"Fn::Base64": bootstrap},
                },
            },
            "Distribution": {
                "Type": "AWS::CloudFront::Distribution",
                "Properties": {
                    "DistributionConfig": {
                        "Enabled": True,
                        "HttpVersion": "http2",
                        "Origins": [
                            {
                                "Id": "vm",
                                "DomainName": {"Fn::GetAtt": ["Instance", "PublicDnsName"]},
                                "CustomOriginConfig": {"HTTPPort": 80, "OriginProtocolPolicy": "http-only"},
                            }
                        ],
                        "DefaultCacheBehavior": {
                            "TargetOriginId": "vm",
                            "ViewerProtocolPolicy": "redirect-to-https",
                            "AllowedMethods": ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"],
                            "CachedMethods": ["GET", "HEAD"],
                            "CachePolicyId": "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",
                            "OriginRequestPolicyId": "216adef6-5c7f-47e4-b989-5492eafa07d3",
                        },
                        "ViewerCertificate": {"CloudFrontDefaultCertificate": True},
                    }
                },
            },
        }
        launch_data = resources["Instance"]["Properties"]
        launch_data["IamInstanceProfile"] = {"Name": {"Ref": "Profile"}}
        resources["LaunchTemplate"] = {
            "Type": "AWS::EC2::LaunchTemplate",
            "Properties": {"LaunchTemplateData": launch_data},
        }
        resources["Instance"] = {
            "Type": "AWS::EC2::Instance",
            "Properties": {
                "LaunchTemplate": {
                    "LaunchTemplateId": {"Ref": "LaunchTemplate"},
                    "Version": {"Fn::GetAtt": ["LaunchTemplate", "LatestVersionNumber"]},
                }
            },
        }
        template = {
            "Resources": resources,
            "Outputs": {
                "Instance": {"Value": {"Ref": "Instance"}},
                "Domain": {"Value": {"Fn::GetAtt": ["Distribution", "DomainName"]}},
            },
        }
        receipt = self.start(application_id, attempt_id, template, image)
        receipt["image"] = image
        self.checkpoint(dict(receipt))
        return self.verify(receipt)

    def verify(self, receipt):
        outputs = self.owned(receipt)
        instance = outputs["Instance"]
        domain = outputs["Domain"]
        if not re.fullmatch(r"i-[a-f0-9]{8,17}", instance) or not re.fullmatch(
            r"[a-z0-9]+\.cloudfront\.net", domain
        ):
            raise ValueError("Invalid owned instance or HTTPS distribution")
        ssm = self.client("ssm")
        command = ssm.send_command(
            InstanceIds=[instance],
            DocumentName="AWS-RunShellScript",
            Parameters={
                "commands": [
                    "docker inspect --format '{{json .}}' sky-app",
                    "docker image inspect --format '{{json .}}' \"$(docker inspect --format '{{.Image}}' sky-app)\"",
                ]
            },
            TimeoutSeconds=60,
        )["Command"]["CommandId"]
        ssm.get_waiter("command_executed").wait(CommandId=command, InstanceId=instance)
        result = ssm.get_command_invocation(CommandId=command, InstanceId=instance)
        if result["Status"] != "Success":
            raise ValueError("EC2 runtime inspection failed")
        lines = result["StandardOutputContent"].splitlines()
        if len(lines) != 2:
            raise ValueError("Missing container/image runtime evidence")
        running, artifact = (json.loads(line) for line in lines)
        if (
            artifact.get("Architecture") != "amd64"
            or artifact.get("Os") != "linux"
            or receipt["artifact_digest"] not in artifact.get("RepoDigests", [])
        ):
            raise ValueError("Running image architecture or repository digest differs")
        if (
            running["Config"]["Image"] != receipt["artifact_digest"]
            or running["State"]["Running"] is not True
        ):
            raise ValueError("EC2 is not running the requested immutable image")
        url = "https://" + domain + "/"
        self.probe(url)
        receipt = dict(
            receipt,
            url=url,
            instance_id=instance,
            status="verified",
            artifact_verified=True,
            http_verified=True,
        )
        self.checkpoint(receipt)
        return receipt
