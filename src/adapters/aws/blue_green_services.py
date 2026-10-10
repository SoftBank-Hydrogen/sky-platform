"""Sky-owned Standard ECS pair behind an exclusive ALB HTTPS listener.

This is an operator provisioning boundary, not an AI command surface. It uses
existing roles/network/certificate; creates no IAM roles or databases. Exactly
one task per colour is deliberate: multi-writer drain and scaling are separate
contracts. Traffic remains 100/0 until the cutover controller changes it.
"""

from dataclasses import dataclass
import re


def _match(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


@dataclass(frozen=True)
class BlueGreenServiceSpec:
    account_id: str
    region: str
    application_id: str
    organization_id: str
    vpc_id: str
    public_subnets: tuple[str, ...]
    private_subnets: tuple[str, ...]
    cluster_arn: str
    execution_role_arn: str
    task_role_arns: tuple[str, str]
    certificate_arn: str
    images: tuple[str, str]
    port: int = 8080
    health_path: str = "/health"

    def validate(self):
        if not _match(self.account_id, r"\d{12}") or not _match(self.region, r"[a-z]{2}(?:-[a-z]+)+-\d+"):
            raise ValueError("Pinned AWS account and region required")
        for identity in (self.application_id, self.organization_id):
            if not _match(identity, r"[a-z][a-z0-9-]{2,47}"):
                raise ValueError("Scoped application and organization identifiers required")
        if not _match(self.vpc_id, r"vpc-[a-f0-9]{8,17}"):
            raise ValueError("Registered VPC required")
        for subnets in (self.public_subnets, self.private_subnets):
            if (
                not isinstance(subnets, tuple)
                or not 2 <= len(subnets) <= 8
                or len(set(subnets)) != len(subnets)
            ):
                raise ValueError("Distinct multi-AZ subnet registrations required")
            if any(not _match(value, r"subnet-[a-f0-9]{8,17}") for value in subnets):
                raise ValueError("Registered subnets required")
        if set(self.public_subnets) & set(self.private_subnets):
            raise ValueError("Public ALB and private tasks require separate subnets")
        prefix = f"arn:aws:ecs:{self.region}:{self.account_id}:cluster/"
        if not isinstance(self.cluster_arn, str) or not self.cluster_arn.startswith(prefix):
            raise ValueError("Same-account ECS cluster required")
        roles = (self.execution_role_arn, *self.task_role_arns)
        if len(self.task_role_arns) != 2 or any(
            not _match(role, f"arn:aws:iam::{self.account_id}:role/[A-Za-z0-9+=,.@_/-]+") for role in roles
        ):
            raise ValueError("Existing same-account execution and application roles required")
        if not _match(
            self.certificate_arn, f"arn:aws:acm:{self.region}:{self.account_id}:certificate/[a-f0-9-]+"
        ):
            raise ValueError("Registered regional HTTPS certificate required")
        if len(self.images) != 2 or any(
            not _match(
                image,
                f"{self.account_id}\\.dkr\\.ecr\\.{self.region}\\.amazonaws\\.com/[a-z0-9._/-]+@sha256:[a-f0-9]{{64}}",
            )
            for image in self.images
        ):
            raise ValueError("Both revisions require immutable same-account ECR image digests")
        if type(self.port) is not int or not 1024 <= self.port <= 65535:
            raise ValueError("Unprivileged application port required")
        if not _match(self.health_path, r"/[A-Za-z0-9/_-]{0,127}"):
            raise ValueError("Bounded health check path required")


def service_template(spec: BlueGreenServiceSpec):
    """Pure CloudFormation generation; callers separately audit roles and network.

    Registered app images must boot standby writers closed before this is used
    for database cutover. ALB health only establishes process readiness, never
    data readiness; the existing controller checks actual database reads.
    """
    spec.validate()
    tags = [
        {"Key": "sky-managed", "Value": "true"},
        {"Key": "sky-application", "Value": spec.application_id},
        {"Key": "sky-organization", "Value": spec.organization_id},
        {"Key": "sky-traffic-controller", "Value": "blue-green-v1"},
    ]
    ref = lambda name: {"Ref": name}  # noqa: E731
    attr = lambda name, key: {"Fn::GetAtt": [name, key]}  # noqa: E731
    resources = {
        "Logs": {"Type": "AWS::Logs::LogGroup", "Properties": {"RetentionInDays": 7, "Tags": tags}},
        "AlbSecurityGroup": {
            "Type": "AWS::EC2::SecurityGroup",
            "Properties": {
                "GroupDescription": "Owned application HTTPS entrypoint",
                "VpcId": spec.vpc_id,
                "SecurityGroupIngress": [
                    {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "CidrIp": "0.0.0.0/0"}
                ],
                "SecurityGroupEgress": [
                    {"IpProtocol": "tcp", "FromPort": 1, "ToPort": 1, "CidrIp": "127.0.0.1/32"}
                ],
                "Tags": tags,
            },
        },
        "AppSecurityGroup": {
            "Type": "AWS::EC2::SecurityGroup",
            "Properties": {
                "GroupDescription": "Private application tasks, ALB ingress only",
                "VpcId": spec.vpc_id,
                "SecurityGroupIngress": [
                    {
                        "IpProtocol": "tcp",
                        "FromPort": spec.port,
                        "ToPort": spec.port,
                        "SourceSecurityGroupId": ref("AlbSecurityGroup"),
                    }
                ],
                "SecurityGroupEgress": [
                    {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "CidrIp": "0.0.0.0/0"}
                ],
                "Tags": tags,
            },
        },
        "LoadBalancer": {
            "Type": "AWS::ElasticLoadBalancingV2::LoadBalancer",
            "Properties": {
                "Type": "application",
                "Scheme": "internet-facing",
                "Subnets": list(spec.public_subnets),
                "SecurityGroups": [ref("AlbSecurityGroup")],
                "Tags": tags,
            },
        },
    }
    # SG references in standalone rules avoid a circular dependency.
    # A harmless explicit rule suppresses AWS default allow-all egress.
    resources["AlbToApp"] = {
        "Type": "AWS::EC2::SecurityGroupEgress",
        "Properties": {
            "GroupId": ref("AlbSecurityGroup"),
            "IpProtocol": "tcp",
            "FromPort": spec.port,
            "ToPort": spec.port,
            "DestinationSecurityGroupId": ref("AppSecurityGroup"),
        },
    }
    for index, colour in enumerate(("Blue", "Green")):
        resources[colour + "Group"] = {
            "Type": "AWS::ElasticLoadBalancingV2::TargetGroup",
            "Properties": {
                "VpcId": spec.vpc_id,
                "TargetType": "ip",
                "Protocol": "HTTP",
                "Port": spec.port,
                "HealthCheckPath": spec.health_path,
                "HealthCheckIntervalSeconds": 10,
                "HealthyThresholdCount": 2,
                "UnhealthyThresholdCount": 2,
                "Matcher": {"HttpCode": "200"},
                "TargetGroupAttributes": [{"Key": "deregistration_delay.timeout_seconds", "Value": "30"}],
                "Tags": tags,
            },
        }
        resources[colour + "Task"] = {
            "Type": "AWS::ECS::TaskDefinition",
            "Properties": {
                "Cpu": "256",
                "Memory": "512",
                "NetworkMode": "awsvpc",
                "RequiresCompatibilities": ["FARGATE"],
                "RuntimePlatform": {"CpuArchitecture": "X86_64", "OperatingSystemFamily": "LINUX"},
                "ExecutionRoleArn": spec.execution_role_arn,
                "TaskRoleArn": spec.task_role_arns[index],
                "ContainerDefinitions": [
                    {
                        "Name": "app",
                        "Essential": True,
                        "Image": spec.images[index],
                        "PortMappings": [{"ContainerPort": spec.port, "Protocol": "tcp"}],
                        "Environment": [
                            {"Name": "PORT", "Value": str(spec.port)},
                            {"Name": "SKY_DEPLOYMENT_COLOUR", "Value": colour.lower()},
                            {"Name": "SKY_STANDBY_WRITES_BLOCKED", "Value": "true" if index else "false"},
                        ],
                        "LogConfiguration": {
                            "LogDriver": "awslogs",
                            "Options": {
                                "awslogs-group": ref("Logs"),
                                "awslogs-region": spec.region,
                                "awslogs-stream-prefix": colour.lower(),
                            },
                        },
                    }
                ],
                "Tags": tags,
            },
        }
        resources[colour + "Service"] = {
            "Type": "AWS::ECS::Service",
            "DependsOn": "Listener",
            "Properties": {
                "Cluster": spec.cluster_arn,
                "TaskDefinition": ref(colour + "Task"),
                "LaunchType": "FARGATE",
                "DesiredCount": 1,
                "HealthCheckGracePeriodSeconds": 60,
                "DeploymentConfiguration": {
                    "MaximumPercent": 100,
                    "MinimumHealthyPercent": 0,
                    "DeploymentCircuitBreaker": {"Enable": True, "Rollback": True},
                },
                "NetworkConfiguration": {
                    "AwsvpcConfiguration": {
                        "AssignPublicIp": "DISABLED",
                        "Subnets": list(spec.private_subnets),
                        "SecurityGroups": [ref("AppSecurityGroup")],
                    }
                },
                "LoadBalancers": [
                    {
                        "TargetGroupArn": ref(colour + "Group"),
                        "ContainerName": "app",
                        "ContainerPort": spec.port,
                    }
                ],
                "Tags": tags,
            },
        }
    resources["Listener"] = {
        "Type": "AWS::ElasticLoadBalancingV2::Listener",
        "Properties": {
            "LoadBalancerArn": ref("LoadBalancer"),
            "Protocol": "HTTPS",
            "Port": 443,
            "Certificates": [{"CertificateArn": spec.certificate_arn}],
            "SslPolicy": "ELBSecurityPolicy-TLS13-1-2-2021-06",
            "DefaultActions": [
                {
                    "Type": "forward",
                    "ForwardConfig": {
                        "TargetGroups": [
                            {"TargetGroupArn": ref("BlueGroup"), "Weight": 100},
                            {"TargetGroupArn": ref("GreenGroup"), "Weight": 0},
                        ],
                        "TargetGroupStickinessConfig": {"Enabled": False},
                    },
                }
            ],
            "Tags": tags,
        },
    }
    outputs = {
        name: {"Value": ref(name)}
        for name in (
            "Listener",
            "LoadBalancer",
            "BlueGroup",
            "GreenGroup",
            "BlueService",
            "GreenService",
            "AppSecurityGroup",
        )
    }
    outputs["DnsName"] = {"Value": attr("LoadBalancer", "DNSName")}
    outputs["HostedZoneId"] = {"Value": attr("LoadBalancer", "CanonicalHostedZoneID")}
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Sky owned single-writer Blue Green ECS service pair",
        "Resources": resources,
        "Outputs": outputs,
    }


def audit_service_registration(spec, *, session):
    """Read-only preflight before any provisioning intent is persisted.

    This checks placement and role trust, not IAM effective permission or app
    writer safety. Those remain mandatory operator registrations/verification.
    """
    spec.validate()
    if (
        session.region_name != spec.region
        or session.client("sts").get_caller_identity()["Account"] != spec.account_id
    ):
        raise ValueError("Provisioning session differs from pinned account/region")
    ec2 = session.client("ec2")
    subnets = ec2.describe_subnets(SubnetIds=[*spec.public_subnets, *spec.private_subnets])["Subnets"]
    indexed = {value["SubnetId"]: value for value in subnets}
    if set(indexed) != set(spec.public_subnets + spec.private_subnets) or any(
        value["VpcId"] != spec.vpc_id or value["State"] != "available" for value in subnets
    ):
        raise ValueError("All registered available subnets must belong to the pinned VPC")
    for ids in (spec.public_subnets, spec.private_subnets):
        if len({indexed[value]["AvailabilityZone"] for value in ids}) < 2:
            raise ValueError("Both ALB and task subnets must span multiple availability zones")
    routes = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [spec.vpc_id]}])
    if routes.get("NextToken"):
        raise ValueError("Complete route table inventory required")
    explicit, main = {}, None
    for table in routes["RouteTables"]:
        for association in table["Associations"]:
            if association.get("Main"):
                main = table
            if association.get("SubnetId"):
                explicit[association["SubnetId"]] = table
    for subnet in spec.public_subnets + spec.private_subnets:
        table = explicit.get(subnet, main)
        if table is None:
            raise ValueError("Subnet route table could not be resolved")
        public = any(
            route.get("DestinationCidrBlock") == "0.0.0.0/0"
            and route.get("GatewayId", "").startswith("igw-")
            and route.get("State") == "active"
            for route in table["Routes"]
        )
        if public != (subnet in spec.public_subnets):
            raise ValueError("Registered public/private placement differs from effective routes")
    certificate = session.client("acm").describe_certificate(CertificateArn=spec.certificate_arn)[
        "Certificate"
    ]
    if certificate["Status"] != "ISSUED":
        raise ValueError("Issued HTTPS certificate required before provisioning")
    iam = session.client("iam")
    for arn in {spec.execution_role_arn, *spec.task_role_arns}:
        role = iam.get_role(RoleName=arn.rsplit("/", 1)[1])["Role"]
        if role["Arn"] != arn:
            raise ValueError("Role identity differs from registration")
        statements = role["AssumeRolePolicyDocument"]["Statement"]
        if isinstance(statements, dict):
            statements = [statements]

        def allows_tasks(statement):
            principal = statement.get("Principal", {})
            services = principal.get("Service", []) if isinstance(principal, dict) else []
            services = [services] if isinstance(services, str) else services
            actions = statement.get("Action", [])
            actions = [actions] if isinstance(actions, str) else actions
            return (
                statement.get("Effect") == "Allow"
                and "ecs-tasks.amazonaws.com" in services
                and "sts:AssumeRole" in actions
            )

        if not any(allows_tasks(statement) for statement in statements):
            raise ValueError("Existing application roles must trust ECS tasks")
    cluster = session.client("ecs").describe_clusters(clusters=[spec.cluster_arn])
    if (
        cluster.get("failures")
        or len(cluster.get("clusters", [])) != 1
        or cluster["clusters"][0]["status"] != "ACTIVE"
    ):
        raise ValueError("Active registered ECS cluster required")
    return {
        "account_id": spec.account_id,
        "region": spec.region,
        "application_id": spec.application_id,
        "organization_id": spec.organization_id,
        "certificate_domains": certificate["SubjectAlternativeNames"],
        "single_task_per_colour": True,
        "database_readiness_verified": False,
    }
