import base64
import hashlib
import json
from unittest.mock import Mock

import pytest

from adapters.aws.ecs import AwsSettings
from adapters.aws.native import AwsEc2Adapter, AwsLambdaAdapter, lambda_bundle

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
ATTEMPT = "abcdef1234567890-a1"
IMAGE = ACCOUNT + ".dkr.ecr." + REGION + ".amazonaws.com/sky-demo@sha256:" + "a" * 64


@pytest.fixture
def clients(monkeypatch):
    monkeypatch.setenv("SKY_AWS_ROLE_BOUNDARY_ARN", f"arn:aws:iam::{ACCOUNT}:policy/sky-runtime")
    monkeypatch.delenv("SKY_ENVIRONMENT", raising=False)
    result = {name: Mock() for name in ("sts", "cloudformation", "lambda", "ec2", "ecr", "ssm", "iam")}
    result["iam"].get_policy.return_value = {"Policy": {"DefaultVersionId": "v1"}}
    result["iam"].get_policy_version.return_value = {
        "PolicyVersion": {"Document": {"Version": "2012-10-17", "Statement": []}}
    }
    result["iam"].simulate_custom_policy.side_effect = lambda **kw: {
        "EvaluationResults": [
            {"EvalActionName": action, "EvalDecision": "allowed"} for action in kw["ActionNames"]
        ]
    }
    result["sts"].get_caller_identity.return_value = {"Account": ACCOUNT}
    return result


def adapter(cls, clients, checkpoint=None):
    return cls(
        AwsSettings(region=REGION, expected_account=ACCOUNT),
        clients=clients,
        checkpoint=checkpoint,
        probe=Mock(),
    )


def configure_stack(instance, clients, outputs):
    name = f"sky-{instance.backend}-{ATTEMPT}"
    arn = f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/{name}/test"
    clients["cloudformation"].create_stack.return_value = {"StackId": arn}

    def describe(**kwargs):
        call = clients["cloudformation"].create_stack.call_args.kwargs
        return {
            "Stacks": [
                {
                    "StackId": arn,
                    "Tags": call["Tags"],
                    "Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in outputs.items()],
                }
            ]
        }

    clients["cloudformation"].describe_stacks.side_effect = describe


def test_bundle_is_deterministic_and_does_not_execute_source(tmp_path):
    (tmp_path / "handler.py").write_text(
        'raise RuntimeError("never execute")\ndef handler(event, context): return {}\n'
    )
    assert lambda_bundle(tmp_path, "handler.handler") == lambda_bundle(tmp_path, "handler.handler")


@pytest.mark.parametrize(
    "source",
    [
        "import flask\ndef handler(event, context): return {}",
        "async def handler(event, context): return {}",
        "def handler(event): return {}",
    ],
)
def test_reject_unsupported_handlers(tmp_path, source):
    (tmp_path / "handler.py").write_text(source)
    with pytest.raises(ValueError):
        lambda_bundle(tmp_path, "handler.handler")


def test_bundle_rejects_secrets_and_symlinks(tmp_path):
    (tmp_path / "handler.py").write_text("def handler(event, context): return {}")
    (tmp_path / ".env").write_text("SECRET=value")
    with pytest.raises(ValueError):
        lambda_bundle(tmp_path, "handler.handler")
    (tmp_path / ".env").unlink()
    (tmp_path / "link.py").symlink_to(tmp_path / "handler.py")
    with pytest.raises(ValueError):
        lambda_bundle(tmp_path, "handler.handler")


def test_lambda_publish_verify_and_owned_delete(tmp_path, clients):
    (tmp_path / "handler.py").write_text('def handler(event, context): return {"statusCode":200,"body":"ok"}')
    digest = base64.b64encode(hashlib.sha256(lambda_bundle(tmp_path, "handler.handler")).digest()).decode()
    checkpoints = []
    instance = adapter(AwsLambdaAdapter, clients, checkpoints.append)
    configure_stack(instance, clients, {"Function": "sky-lambda-" + ATTEMPT})
    clients["lambda"].update_function_code.return_value = {"CodeSha256": digest, "Version": "1"}
    clients["lambda"].get_function.return_value = {"Configuration": {"CodeSha256": digest, "Version": "1"}}
    clients["lambda"].get_function_url_config.return_value = {
        "FunctionUrl": f"https://abc.lambda-url.{REGION}.on.aws/"
    }
    receipt = instance.deploy(tmp_path, "demo-app", ATTEMPT, public_access=True)
    assert receipt["status"] == "verified"
    assert "stack_id" not in checkpoints[0]
    permissions = clients["lambda"].add_permission.call_args_list
    assert {c.kwargs["Action"] for c in permissions} == {"lambda:InvokeFunction", "lambda:InvokeFunctionUrl"}
    assert permissions[1].kwargs["InvokedViaFunctionUrl"] is True
    template = json.loads(clients["cloudformation"].create_stack.call_args.kwargs["TemplateBody"])
    assert template["Resources"]["Role"]["Properties"]["PermissionsBoundary"].endswith("sky-runtime")
    assert instance.destroy(receipt)["status"] == "deleted"


def test_fail_closed_account_boundary_and_public_permission(tmp_path, clients, monkeypatch):
    instance = adapter(AwsLambdaAdapter, clients)
    with pytest.raises(ValueError):
        instance.deploy(tmp_path, "demo-app", ATTEMPT)
    clients["sts"].get_caller_identity.return_value = {"Account": "999999999999"}
    with pytest.raises(RuntimeError):
        instance.identity()
    clients["cloudformation"].create_stack.assert_not_called()
    monkeypatch.delenv("SKY_AWS_ROLE_BOUNDARY_ARN")
    with pytest.raises(ValueError):
        adapter(AwsLambdaAdapter, clients)


def test_ec2_pinned_image_runtime_verification_and_isolation(clients):
    ec2 = clients["ec2"]
    ec2.describe_subnets.return_value = {
        "Subnets": [{"OwnerId": ACCOUNT, "State": "available", "VpcId": "vpc-12345678"}]
    }
    ec2.describe_route_tables.return_value = {
        "RouteTables": [
            {
                "Routes": [
                    {"DestinationCidrBlock": "0.0.0.0/0", "State": "active", "GatewayId": "igw-12345678"}
                ]
            }
        ]
    }
    ec2.describe_vpc_attribute.side_effect = lambda **kw: {
        kw["Attribute"][0].upper() + kw["Attribute"][1:]: {"Value": True}
    }
    ec2.describe_images.return_value = {"Images": [{"Architecture": "x86_64", "State": "available"}]}
    ec2.describe_managed_prefix_lists.return_value = {
        "PrefixLists": [{"OwnerId": "AWS", "PrefixListId": "pl-12345678"}]
    }
    clients["ecr"].describe_images.return_value = {"imageDetails": [{"imageDigest": "sha256:" + "a" * 64}]}
    clients["ecr"].batch_get_image.return_value = {
        "images": [{"imageId": {"imageDigest": "sha256:" + "a" * 64}, "imageManifest": "{}"}]
    }
    clients["ssm"].get_parameter.return_value = {"Parameter": {"Value": "ami-12345678"}}
    clients["ssm"].send_command.return_value = {
        "Command": {"CommandId": "12345678-1234-1234-1234-123456789012"}
    }
    clients["ssm"].get_command_invocation.return_value = {
        "Status": "Success",
        "StandardOutputContent": json.dumps({"Config": {"Image": IMAGE}, "State": {"Running": True}})
        + "\n"
        + json.dumps({"Architecture": "amd64", "Os": "linux", "RepoDigests": [IMAGE]}),
    }
    instance = adapter(AwsEc2Adapter, clients)
    configure_stack(instance, clients, {"Instance": "i-12345678901234567", "Domain": "abc.cloudfront.net"})
    receipt = instance.deploy(
        IMAGE, "demo-app", ATTEMPT, subnet_id="subnet-12345678", public_access=True, stateless=True
    )
    assert receipt["status"] == "verified"
    template = json.loads(clients["cloudformation"].create_stack.call_args.kwargs["TemplateBody"])
    launch = template["Resources"]["LaunchTemplate"]["Properties"]["LaunchTemplateData"]
    assert launch["MetadataOptions"]["HttpPutResponseHopLimit"] == 1
    bootstrap = launch["UserData"]["Fn::Base64"]
    assert "--cap-drop ALL" in bootstrap and "DOCKER-USER" in bootstrap
    assert "--privileged" not in bootstrap and "docker.sock" not in bootstrap
    clients["ssm"].get_command_invocation.return_value["StandardOutputContent"] = "{}"
    with pytest.raises(ValueError):
        instance.verify(receipt)


@pytest.mark.parametrize(
    "image", ["nginx:latest", IMAGE.replace(ACCOUNT, "999999999999"), IMAGE + "; malicious"]
)
def test_ec2_rejects_mutable_foreign_or_shell_image(clients, image):
    with pytest.raises(ValueError):
        adapter(AwsEc2Adapter, clients).deploy(
            image, "demo-app", ATTEMPT, subnet_id="subnet-12345678", public_access=True, stateless=True
        )
    clients["cloudformation"].create_stack.assert_not_called()


def test_destroy_refuses_foreign_receipt(clients):
    instance = adapter(AwsLambdaAdapter, clients)
    with pytest.raises(ValueError):
        instance.destroy(
            {"account": "999999999999", "region": REGION, "stack_name": "foreign", "stack_id": "foreign"}
        )
    clients["cloudformation"].delete_stack.assert_not_called()


def test_ownership_drift_blocks_delete(clients):
    instance = adapter(AwsLambdaAdapter, clients)
    configure_stack(instance, clients, {})
    receipt = instance.start("demo-app", ATTEMPT, {"Resources": {}}, "digest")
    clients["cloudformation"].describe_stacks.side_effect = None
    clients["cloudformation"].describe_stacks.return_value = {
        "Stacks": [{"StackId": receipt["stack_id"], "Tags": [{"Key": "sky-managed", "Value": "true"}]}]
    }
    with pytest.raises(ValueError):
        instance.destroy(receipt)
    clients["cloudformation"].delete_stack.assert_not_called()


def test_uncertain_create_keeps_recovery_key(clients):
    checkpoints = []
    instance = adapter(AwsLambdaAdapter, clients, checkpoints.append)
    clients["cloudformation"].create_stack.side_effect = TimeoutError("uncertain request")
    with pytest.raises(TimeoutError):
        instance.start("demo-app", ATTEMPT, {"Resources": {}}, "digest")
    assert checkpoints[0]["stack_name"] == "sky-lambda-" + ATTEMPT
    assert clients["cloudformation"].create_stack.call_count == 1
    clients["cloudformation"].delete_stack.assert_not_called()


def test_lambda_digest_mismatch_never_exposes_function(tmp_path, clients):
    (tmp_path / "handler.py").write_text("def handler(event, context): return {}")
    instance = adapter(AwsLambdaAdapter, clients)
    configure_stack(instance, clients, {"Function": "sky-lambda-" + ATTEMPT})
    clients["lambda"].update_function_code.return_value = {"CodeSha256": "different", "Version": "1"}
    with pytest.raises(ValueError):
        instance.deploy(tmp_path, "demo-app", ATTEMPT, public_access=True)
    clients["lambda"].create_function_url_config.assert_not_called()


def test_native_cli_rejects_duplicate_receipt(tmp_path):
    from interfaces.native_aws import main

    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}")
    with pytest.raises(SystemExit) as error:
        main(["lambda", "deploy", "--receipt", str(receipt)])
    assert error.value.code == 2
    assert receipt.read_text() == "{}"


def test_aws_sdk_request_shapes(clients, tmp_path):
    """Validate generated requests against botocore, beyond permissive Python mocks."""
    from botocore import xform_name
    from botocore.session import get_session
    from botocore.validate import validate_parameters

    test_lambda_publish_verify_and_owned_delete(tmp_path, clients)
    test_ec2_pinned_image_runtime_verification_and_isolation(clients)
    session = get_session()
    for service, client in clients.items():
        model = session.get_service_model(service)
        names = {xform_name(name): name for name in model.operation_names}
        for name, child in client._mock_children.items():
            if name not in names:
                continue
            shape = model.operation_model(names[name]).input_shape
            for call in child.call_args_list:
                validate_parameters(call.kwargs, shape)


def test_ec2_boundary_missing_ssm_fails_before_allocating_any_resources(clients):
    clients["iam"].simulate_custom_policy.side_effect = None
    clients["iam"].simulate_custom_policy.return_value = {"EvaluationResults": []}
    with pytest.raises(ValueError, match="SSM agent permissions"):
        adapter(AwsEc2Adapter, clients).deploy(
            IMAGE, "demo-app", ATTEMPT, subnet_id="subnet-12345678", public_access=True, stateless=True
        )
    clients["cloudformation"].create_stack.assert_not_called()
    clients["ec2"].describe_subnets.assert_not_called()
