from dataclasses import replace

import pytest

from adapters.aws.blue_green_services import BlueGreenServiceSpec, service_template


def spec():
    return BlueGreenServiceSpec(
        account_id="977889523182",
        region="ap-northeast-2",
        application_id="validation-app",
        organization_id="validation-org",
        vpc_id="vpc-06433579d1d23c4fc",
        public_subnets=("subnet-0b1fa0ce0dee1e209", "subnet-0a4ff64ba15e3183f"),
        private_subnets=("subnet-07e688ca6da2998f3", "subnet-05064929dd10e295b"),
        cluster_arn="arn:aws:ecs:ap-northeast-2:977889523182:cluster/sky-dev",
        execution_role_arn="arn:aws:iam::977889523182:role/execution",
        task_role_arns=("arn:aws:iam::977889523182:role/blue", "arn:aws:iam::977889523182:role/green"),
        certificate_arn="arn:aws:acm:ap-northeast-2:977889523182:certificate/abc-123",
        images=tuple("977889523182.dkr.ecr.ap-northeast-2.amazonaws.com/app@sha256:" + x * 64 for x in "ab"),
    )


def test_two_private_single_writer_services_and_both_groups_associated():
    resources = service_template(spec())["Resources"]
    weights = resources["Listener"]["Properties"]["DefaultActions"][0]["ForwardConfig"]
    assert [x["Weight"] for x in weights["TargetGroups"]] == [100, 0]
    assert weights["TargetGroupStickinessConfig"] == {"Enabled": False}
    for colour in ("Blue", "Green"):
        service = resources[colour + "Service"]
        assert service["DependsOn"] == "Listener"
        assert service["Properties"]["DesiredCount"] == 1
        assert (
            service["Properties"]["NetworkConfiguration"]["AwsvpcConfiguration"]["AssignPublicIp"]
            == "DISABLED"
        )
        assert service["Properties"]["DeploymentConfiguration"]["MaximumPercent"] == 100
        task = resources[colour + "Task"]["Properties"]
        assert task["ContainerDefinitions"][0]["Image"].split("@sha256:")[1]
        assert task["TaskRoleArn"].endswith(colour.lower())
    assert not any(x["Type"].startswith(("AWS::IAM::", "AWS::RDS::")) for x in resources.values())


def test_public_ingress_is_https_and_application_only_accepts_owned_alb():
    resources = service_template(spec())["Resources"]
    ingress = resources["AlbSecurityGroup"]["Properties"]["SecurityGroupIngress"]
    assert len(ingress) == 1 and ingress[0]["FromPort"] == ingress[0]["ToPort"] == 443
    ingress = resources["AppSecurityGroup"]["Properties"]["SecurityGroupIngress"]
    assert ingress == [
        {
            "IpProtocol": "tcp",
            "FromPort": 8080,
            "ToPort": 8080,
            "SourceSecurityGroupId": {"Ref": "AlbSecurityGroup"},
        }
    ]
    for name in ("Listener", "LoadBalancer", "BlueGroup", "GreenGroup"):
        assert {x["Key"]: x["Value"] for x in resources[name]["Properties"]["Tags"]}[
            "sky-traffic-controller"
        ] == "blue-green-v1"


@pytest.mark.parametrize(
    "changes",
    [
        {"images": ("app:latest", "app:latest")},
        {"certificate_arn": "arn:aws:acm:us-east-1:977889523182:certificate/abc"},
        {"task_role_arns": ("arn:aws:iam::111111111111:role/blue", "arn:aws:iam::977889523182:role/green")},
        {"private_subnets": ("subnet-0b1fa0ce0dee1e209", "subnet-05064929dd10e295b")},
        {"public_subnets": ("subnet-0b1fa0ce0dee1e209",) * 2},
        {"port": True},
    ],
)
def test_rejects_mutable_foreign_or_unsafe_registration(changes):
    with pytest.raises(ValueError):
        service_template(replace(spec(), **changes))


def test_registration_audit_requires_real_private_routes_and_issued_certificate():
    from adapters.aws.blue_green_services import audit_service_registration

    class Session:
        region_name = "ap-northeast-2"
        public_private = False
        certificate_status = "ISSUED"

        def client(self, name):
            return self

        def get_caller_identity(self):
            return {"Account": spec().account_id}

        def describe_subnets(self, **kwargs):
            return {
                "Subnets": [
                    {
                        "SubnetId": subnet,
                        "VpcId": spec().vpc_id,
                        "State": "available",
                        "AvailabilityZone": "zone-" + str(index % 2),
                    }
                    for index, subnet in enumerate(spec().public_subnets + spec().private_subnets)
                ]
            }

        def describe_route_tables(self, **kwargs):
            return {
                "RouteTables": [
                    {
                        "Associations": [{"SubnetId": subnet}],
                        "Routes": [
                            {
                                "DestinationCidrBlock": "0.0.0.0/0",
                                "State": "active",
                                "GatewayId": "igw-123" if index < 2 or self.public_private else "local",
                            }
                        ],
                    }
                    for index, subnet in enumerate(spec().public_subnets + spec().private_subnets)
                ]
            }

        def describe_certificate(self, **kwargs):
            return {
                "Certificate": {"Status": self.certificate_status, "SubjectAlternativeNames": ["example.com"]}
            }

        def get_role(self, RoleName):
            return {
                "Role": {
                    "Arn": "arn:aws:iam::977889523182:role/" + RoleName,
                    "AssumeRolePolicyDocument": {
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": "sts:AssumeRole",
                                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                            }
                        ]
                    },
                }
            }

        def describe_clusters(self, **kwargs):
            return {"clusters": [{"status": "ACTIVE"}]}

    session = Session()
    assert audit_service_registration(spec(), session=session)["database_readiness_verified"] is False
    session.public_private = True
    with pytest.raises(ValueError, match="effective routes"):
        audit_service_registration(spec(), session=session)
    session.public_private = False
    session.certificate_status = "PENDING_VALIDATION"
    with pytest.raises(ValueError, match="Issued HTTPS"):
        audit_service_registration(spec(), session=session)
