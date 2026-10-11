"""Sky-owned Standard ECS pair behind an exclusive ALB HTTPS listener.

This is an operator provisioning boundary, not an AI command surface. It uses
existing roles/network/certificate; creates no IAM roles or databases. Exactly
one task per colour is deliberate: multi-writer drain and scaling are separate
contracts. Traffic remains 100/0 until the cutover controller changes it.
"""

from dataclasses import dataclass, asdict
import json
from domain.database import DatabaseBinding
import re


def _match(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


@dataclass(frozen=True)
class DatabaseServiceAccess:
    binding: DatabaseBinding
    security_group_id: str
    secret_arn: str
    host: str


@dataclass(frozen=True)
class PrivateControlAccess:
    worker_security_group_id: str
    secret_arns: tuple[str, str]
    port: int = 8443


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
    database_access: tuple[DatabaseServiceAccess, DatabaseServiceAccess] | None = None
    private_control: PrivateControlAccess | None = None

    def validate(self):
        if not _match(self.account_id, r"\d{12}") or not _match(self.region, r"[a-z]{2}(?:-[a-z]+)+-\d+"):
            raise ValueError("Pinned AWS account and region required")
        for identity in (self.application_id, self.organization_id):
            if not _match(identity, r"[a-z][a-z0-9_-]{2,127}"):
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

        def secret_matches(arn):
            return _match(
                arn, f"arn:aws:secretsmanager:{self.region}:{self.account_id}:secret:[A-Za-z0-9/_+=.@!-]+"
            )

        if self.database_access is not None:
            if not isinstance(self.database_access, tuple) or len(self.database_access) != 2:
                raise ValueError("Two fixed database access registrations required")
            for index, access in enumerate(self.database_access):
                if not isinstance(access, DatabaseServiceAccess) or not isinstance(
                    access.binding, DatabaseBinding
                ):
                    raise ValueError("Typed database binding registration required")
                binding = access.binding
                if (
                    (
                        binding.account_id,
                        binding.region,
                        binding.organization_id,
                        binding.application_id,
                        binding.role,
                    )
                    != (
                        self.account_id,
                        self.region,
                        self.organization_id,
                        self.application_id,
                        ("shared_workload", "dedicated_workload")[index],
                    )
                    or not _match(access.security_group_id, r"sg-[a-f0-9]{8,17}")
                    or not secret_matches(access.secret_arn)
                    or not _match(access.host, r"[a-z0-9.-]+\.rds\.amazonaws\.com")
                ):
                    raise ValueError("Database scope, role or secret registration mismatch")
            if self.database_access[0].binding.instance_id == self.database_access[1].binding.instance_id:
                raise ValueError("Distinct source and target instances required")
            if self.database_access[0].secret_arn == self.database_access[1].secret_arn:
                raise ValueError("Distinct application database credentials required")
        if self.private_control is not None:
            control = self.private_control
            if (
                self.database_access is None
                or not isinstance(control, PrivateControlAccess)
                or not _match(control.worker_security_group_id, r"sg-[a-f0-9]{8,17}")
                or type(control.port) is not int
                or not 1024 <= control.port <= 65535
                or control.port == self.port
                or len(control.secret_arns) != 2
                or control.secret_arns[0] == control.secret_arns[1]
                or any(not secret_matches(arn) for arn in control.secret_arns)
            ):
                raise ValueError("Separate private control scope and per-colour secrets required")


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
    if spec.private_control is not None:
        control = spec.private_control
        resources["ControlSecurityGroup"] = {
            "Type": "AWS::EC2::SecurityGroup",
            "Properties": {
                "GroupDescription": "Authenticated TLS application control, worker ingress only",
                "VpcId": spec.vpc_id,
                "Tags": tags,
                "SecurityGroupIngress": [
                    {
                        "IpProtocol": "tcp",
                        "FromPort": control.port,
                        "ToPort": control.port,
                        "SourceSecurityGroupId": control.worker_security_group_id,
                    }
                ],
                "SecurityGroupEgress": [
                    {"IpProtocol": "tcp", "FromPort": 1, "ToPort": 1, "CidrIp": "127.0.0.1/32"}
                ],
            },
        }
    if spec.database_access is not None:
        for index, colour in enumerate(("Blue", "Green")):
            access = spec.database_access[index]
            group = colour + "DatabaseSecurityGroup"
            resources[group] = {
                "Type": "AWS::EC2::SecurityGroup",
                "Properties": {
                    "GroupDescription": "Colour-specific fixed database access",
                    "VpcId": spec.vpc_id,
                    "Tags": tags,
                    "SecurityGroupEgress": [
                        {
                            "IpProtocol": "tcp",
                            "FromPort": 5432,
                            "ToPort": 5432,
                            "DestinationSecurityGroupId": access.security_group_id,
                        }
                    ],
                },
            }
            resources[colour + "DatabaseIngress"] = {
                "Type": "AWS::EC2::SecurityGroupIngress",
                "Properties": {
                    "GroupId": access.security_group_id,
                    "IpProtocol": "tcp",
                    "FromPort": 5432,
                    "ToPort": 5432,
                    "SourceSecurityGroupId": ref(group),
                    "Description": "Sky owned fixed-binding app access",
                },
            }
            resources[colour + "Service"]["DependsOn"] = ["Listener", colour + "DatabaseIngress"]
            network = resources[colour + "Service"]["Properties"]["NetworkConfiguration"][
                "AwsvpcConfiguration"
            ]
            network["SecurityGroups"].append(ref(group))
            container = resources[colour + "Task"]["Properties"]["ContainerDefinitions"][0]
            container["Environment"] += [
                {"Name": "SKY_DATABASE_SECRET_ARN", "Value": access.secret_arn},
                {
                    "Name": "SKY_APPLICATION_BINDING_JSON",
                    "Value": json.dumps(asdict(access.binding), sort_keys=True),
                },
                {"Name": "SKY_DATABASE_HOST", "Value": access.host},
            ]
            if spec.private_control is not None:
                network["SecurityGroups"].append(ref("ControlSecurityGroup"))
                container["PortMappings"].append(
                    {"ContainerPort": spec.private_control.port, "Protocol": "tcp"}
                )
                container["Environment"] += [
                    {"Name": "SKY_CONTROL_SECRET_ARN", "Value": spec.private_control.secret_arns[index]},
                    {"Name": "SKY_CONTROL_PORT", "Value": str(spec.private_control.port)},
                ]
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
    if spec.database_access is not None or spec.private_control is not None:
        groups = {access.security_group_id for access in (spec.database_access or ())}
        if spec.private_control is not None:
            groups.add(spec.private_control.worker_security_group_id)
        described = ec2.describe_security_groups(GroupIds=sorted(groups))["SecurityGroups"]
        if {group["GroupId"] for group in described} != groups or any(
            group["VpcId"] != spec.vpc_id for group in described
        ):
            raise ValueError("DB and worker groups must belong to the registered VPC")
        for access in spec.database_access or ():
            instance = session.client("rds").describe_db_instances(
                DBInstanceIdentifier=access.binding.instance_id
            )["DBInstances"]
            if (
                len(instance) != 1
                or instance[0]["DBInstanceStatus"] != "available"
                or instance[0]["Endpoint"]["Address"] != access.host
                or instance[0]["Endpoint"]["Port"] != 5432
                or access.security_group_id
                not in {group["VpcSecurityGroupId"] for group in instance[0]["VpcSecurityGroups"]}
            ):
                raise ValueError("Actual database endpoint/security group differs from fixed binding")
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
