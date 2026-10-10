import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from adapters.aws.native_image import NativeImagePublisher

ATTEMPT = "abcdef1234567890-a1"
ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
CONFIG = "sha256:" + "a" * 64
MANIFEST = "sha256:" + "b" * 64


def publisher():
    api = Mock()
    adapter = Mock(settings=SimpleNamespace(expected_account=ACCOUNT, region=REGION))
    adapter.client.return_value = api
    command = Mock(return_value="unix:///var/run/docker.sock")
    instance = NativeImagePublisher(adapter, Mock(), Mock(), command)
    api.get_authorization_token.return_value = {
        "authorizationData": [
            {
                "proxyEndpoint": f"https://{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com",
                "authorizationToken": base64.b64encode(b"AWS:private-password").decode(),
            }
        ]
    }
    api.describe_images.return_value = {"imageDetails": [{"imageDigest": MANIFEST}]}
    api.batch_get_image.return_value = {
        "images": [{"imageManifest": json.dumps({"config": {"digest": CONFIG}})}]
    }
    return instance, api, command


def test_publisher_promotes_rehearsed_image_and_keeps_password_out_of_arguments(tmp_path):
    instance, api, command = publisher()
    plan = SimpleNamespace(source_digest="source")
    with (
        patch("adapters.aws.native_image.ImageBuilder"),
        patch(
            "adapters.aws.native_image.rehearse_image", return_value={"image_id": CONFIG, "status": "passed"}
        ),
    ):
        receipt = instance.publish(tmp_path, plan, "demo-app", ATTEMPT)
    assert receipt["image"].endswith("@" + MANIFEST)
    assert receipt["rehearsal"]["image_id"] == CONFIG
    assert api.create_repository.call_args.kwargs["imageTagMutability"] == "IMMUTABLE"
    for call in command.call_args_list:
        assert "private-password" not in str(call.args)
    assert any(c.kwargs.get("stdin") == "private-password" for c in command.call_args_list)


def test_publisher_rejects_different_ecr_image_configuration(tmp_path):
    instance, api, _ = publisher()
    api.batch_get_image.return_value = {
        "images": [{"imageManifest": json.dumps({"config": {"digest": "different"}})}]
    }
    with (
        patch("adapters.aws.native_image.ImageBuilder"),
        patch(
            "adapters.aws.native_image.rehearse_image", return_value={"image_id": CONFIG, "status": "passed"}
        ),
        pytest.raises(ValueError),
    ):
        instance.publish(tmp_path, SimpleNamespace(source_digest="source"), "demo-app", ATTEMPT)


def test_publisher_refuses_foreign_repository_deletion():
    instance, api, _ = publisher()
    receipt = {
        "account": ACCOUNT,
        "region": REGION,
        "attempt_id": ATTEMPT,
        "repository": "sky-native-" + ATTEMPT,
        "application_id": "demo-app",
        "source_digest": "source",
    }
    arn = f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/sky-native-{ATTEMPT}"
    api.describe_repositories.return_value = {"repositories": [{"repositoryArn": arn}]}
    api.list_tags_for_resource.return_value = {"tags": [{"Key": "sky-managed", "Value": "true"}]}
    with pytest.raises(ValueError):
        instance.destroy(receipt)
    api.delete_repository.assert_not_called()


def test_build_command_strips_credential_environment(monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "private-aws")
    monkeypatch.setenv("OPENAI_API_KEY", "private-openai")
    with patch(
        "adapters.aws.native_image.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="ok")
    ) as run:
        NativeImagePublisher._command(["docker", "build", "."])
    environment = run.call_args.kwargs["env"]
    assert "AWS_SECRET_ACCESS_KEY" not in environment and "OPENAI_API_KEY" not in environment
    assert "DOCKER_CONFIG" in environment
