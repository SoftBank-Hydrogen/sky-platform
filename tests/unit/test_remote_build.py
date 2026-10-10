"""Queue, GitHub, image provenance and credential-isolation boundary tests."""

import io
import json
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError

from adapters.aws.build_image import EcrBuildVerifier
from adapters.aws.build_objects import S3BuildObjects
from adapters.aws.job_queue import SqsOperationQueue
from adapters.aws.source_artifacts import S3ArtifactSettings
from adapters.aws.task_protection import EcsTaskProtection
from adapters.github.remote_build import GitHubRemoteBuild, InstallationToken
from ports.artifacts import SourceArtifact
from ports.operations import Operation
from ports.remote_builds import BuildSettings, canonical, digest, request_for, request_key


@pytest.fixture
def settings():
    return BuildSettings(
        "SoftBank-Hydrogen/sky-builder",
        "main",
        "a" * 40,
        "b" * 40,
        "sky-artifacts-test",
        "123456789012",
        "ap-northeast-2",
    )


@pytest.fixture
def build_request(settings):
    source = SourceArtifact("team", "game", "a" * 32, "prepared", "b" * 64, 100, "c" * 64)
    plan = {
        "target": "aws",
        "replicas": 1,
        "source_digest": source.source_digest,
        "dockerfile_source": "existing",
        "dockerfile": "FROM node:22\n",
        "large_number": 1e20,
    }
    command = {
        "version": 1,
        "target": "aws",
        "account_id": settings.account_id,
        "region": settings.region,
        "organization_id": "team",
        "created_by": "alice",
        "job_id": "a" * 16,
        "approval_id": str(uuid4()),
        "source_ref": source.record(),
        "source_digest": source.source_digest,
        "approved_plan_json": canonical(plan).decode(),
        "plan_digest": digest(plan),
    }
    operation = Operation(
        str(uuid4()), "game", "deploy", "queued", str(uuid4()), command, {}, None, False, 1, None, None
    )
    return request_for(operation, "team", settings)


def test_dispatch_uses_only_fixed_reference_and_request_pointer(settings, build_request):
    api = Mock()
    api.call.side_effect = [{"sha": settings.workflow_sha}, None]
    GitHubRemoteBuild(settings, api).dispatch(build_request)
    call = api.call.call_args.args
    assert call[0] == "POST"
    assert call[2] == {
        "ref": "main",
        "inputs": {
            "build_id": build_request["build_id"],
            "request_key": request_key(build_request),
            "request_digest": digest(build_request),
        },
    }
    assert "approved_plan_json" not in json.dumps(call[2])


def test_dispatch_ref_drift_fails_before_workflow_submission(settings, build_request):
    api = Mock()
    api.call.return_value = {"sha": "f" * 40}
    with pytest.raises(ValueError):
        GitHubRemoteBuild(settings, api).dispatch(build_request)
    assert api.call.call_count == 1


def run(settings, build_request):
    return {
        "id": 42,
        "display_title": "sky-build:" + build_request["build_id"],
        "head_sha": settings.workflow_sha,
        "head_branch": "main",
        "event": "workflow_dispatch",
        "repository": {"full_name": settings.repository},
        "status": "completed",
        "conclusion": "success",
    }


@pytest.mark.parametrize(
    "change",
    [
        {"head_sha": "f" * 40},
        {"event": "push"},
        {"head_branch": "other"},
        {"repository": {"full_name": "evil/builder"}},
    ],
)
def test_wrong_workflow_provenance_is_rejected(settings, build_request, change):
    api = Mock()
    api.call.return_value = {"workflow_runs": [{**run(settings, build_request), **change}]}
    with pytest.raises(ValueError):
        GitHubRemoteBuild(settings, api).observe(build_request)


def test_missing_and_duplicate_run_are_not_assumed_success(settings, build_request):
    api = Mock()
    api.call.return_value = {"workflow_runs": []}
    builder = GitHubRemoteBuild(settings, api)
    assert builder.observe(build_request) is None
    api.call.return_value = {"workflow_runs": [run(settings, build_request)] * 2}
    with pytest.raises(ValueError):
        builder.observe(build_request)


def test_ecr_manifest_digest_must_match(settings, build_request):
    client = Mock()
    verifier = EcrBuildVerifier(settings, client=client)
    client.batch_get_image.return_value = {"images": [{"imageId": {"imageDigest": "sha256:" + "d" * 64}}]}
    verifier.verify(build_request, {"image_digest": "sha256:" + "d" * 64})
    assert client.batch_get_image.call_args.kwargs["imageIds"] == [
        {"imageTag": "build-" + build_request["build_id"]}
    ]
    with pytest.raises(ValueError):
        verifier.verify(build_request, {"image_digest": "sha256:" + "e" * 64})


def queue_message():
    return {
        "version": 1,
        "workspace": "team",
        "operation_id": str(uuid4()),
        "attempt_id": str(uuid4()),
        "application_id": "game",
    }


@pytest.mark.parametrize(
    "change",
    [{"version": True}, {"command": {"evil": 1}}, {"operation_id": "bad"}, {"application_id": "../evil"}],
)
def test_queue_rejects_non_identity_payloads(change):
    client = Mock()
    client.receive_message.return_value = {
        "Messages": [{"ReceiptHandle": "receipt", "Body": json.dumps({**queue_message(), **change})}]
    }
    queue = SqsOperationQueue(
        "https://sqs.ap-northeast-2.amazonaws.com/123456789012/sky.fifo",
        region="ap-northeast-2",
        account_id="123456789012",
        client=client,
    )
    assert queue.receive().message is None
    client.delete_message.assert_not_called()


def test_receive_extend_and_ack_use_only_received_handle():
    client = Mock()
    message = queue_message()
    client.receive_message.return_value = {
        "Messages": [{"ReceiptHandle": "receipt", "Body": json.dumps(message)}]
    }
    queue = SqsOperationQueue(
        "https://sqs.ap-northeast-2.amazonaws.com/123456789012/sky.fifo",
        region="ap-northeast-2",
        account_id="123456789012",
        client=client,
    )
    delivery = queue.receive()
    assert delivery.message == message
    queue.extend(delivery)
    queue.delete(delivery)
    assert client.change_message_visibility.call_args.kwargs["ReceiptHandle"] == "receipt"
    assert client.delete_message.call_args.kwargs["ReceiptHandle"] == "receipt"


def test_build_object_conflict_requires_identical_request(settings, build_request):
    client = Mock()
    objects = S3BuildObjects(
        S3ArtifactSettings(settings.bucket, settings.region, settings.account_id), client=client
    )
    client.put_object.side_effect = ClientError(
        {"ResponseMetadata": {"HTTPStatusCode": 412}, "Error": {"Code": "PreconditionFailed"}}, "PutObject"
    )
    client.get_object.side_effect = lambda **_: {"Body": io.BytesIO(canonical(build_request))}
    objects.put_request(build_request)
    assert client.put_object.call_args.kwargs["IfNoneMatch"] == "*"
    with pytest.raises(ValueError):
        objects.put_request({**build_request, "job_id": "f" * 16})


def test_build_object_reads_are_bounded_and_reject_duplicate_fields(settings):
    client = Mock()
    objects = S3BuildObjects(
        S3ArtifactSettings(settings.bucket, settings.region, settings.account_id), client=client
    )
    for data in (b'{"version":1,"version":2}', b"x" * 65537):
        body = io.BytesIO(data)
        client.get_object.return_value = {"Body": body}
        with pytest.raises(ValueError):
            objects.read("builds/test/result.json")
        assert body.closed


@pytest.mark.parametrize(
    "uri",
    [
        "https://example.com",
        "http://127.0.0.1",
        "http://169.254.170.2/evil",
        "http://169.254.170.2@evil.test",
    ],
)
def test_task_protection_cannot_target_arbitrary_hosts(uri):
    with pytest.raises(ValueError):
        EcsTaskProtection(uri)


def test_github_app_jwt_signs_and_scopes_installation_token(settings):
    import base64

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    token = InstallationToken("1", "2", pem, settings.repository)
    header, body, signature = token.jwt().split(".")
    key.public_key().verify(
        base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)),
        (header + "." + body).encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    with patch("adapters.github.remote_build.GitHubApi.call", return_value={"token": "private-token"}) as api:
        assert token() == token() == "private-token"
    assert api.call_count == 1
    assert api.call_args.args[2]["permissions"] == {"actions": "write", "contents": "read"}


def test_build_job_refuses_cloud_credentials_before_docker(build_request, tmp_path, monkeypatch):
    from scripts.remote_builder import build

    (tmp_path / "request.json").write_bytes(canonical(build_request))
    for name, value in {
        "REQUEST_DIGEST": digest(build_request),
        "BUILD_ID": build_request["build_id"],
        "GITHUB_REPOSITORY": build_request["repository"],
        "GITHUB_SHA": build_request["workflow_sha"],
        "SKY_PLATFORM_CODE_SHA": build_request["platform_code_sha"],
        "AWS_ACCESS_KEY_ID": "private",
    }.items():
        monkeypatch.setenv(name, value)
    with patch("scripts.remote_builder.command") as command, pytest.raises(ValueError):
        build(tmp_path, tmp_path / "output")
    command.assert_not_called()


def test_build_consumer_config_check_constructs_no_remote_clients(tmp_path, monkeypatch, capsys):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from interfaces.b_runtime import main

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    ca = tmp_path / "ca.pem"
    ca.write_text("test-ca")
    values = {
        "SKY_DATABASE_HOST": "state.internal",
        "SKY_DATABASE_NAME": "sky",
        "SKY_DATABASE_SECRET_ARN": "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:state",
        "SKY_DATABASE_SSLROOTCERT": str(ca),
        "SKY_AWS_REGION": "ap-northeast-2",
        "SKY_AWS_ACCOUNT_ID": "123456789012",
        "SKY_ARTIFACTS_BUCKET": "sky-artifacts-test",
        "SKY_BUILDER_REPOSITORY": "SoftBank-Hydrogen/sky-builder",
        "SKY_BUILDER_REF": "main",
        "SKY_BUILDER_SHA": "a" * 40,
        "SKY_BUILDER_PLATFORM_SHA": "b" * 40,
        "SKY_JOB_QUEUE_URL": "https://sqs.ap-northeast-2.amazonaws.com/123456789012/sky.fifo",
        "ECS_AGENT_URI": "http://169.254.170.2",
        "SKY_GITHUB_APP_ID": "1",
        "SKY_GITHUB_INSTALLATION_ID": "2",
        "SKY_GITHUB_APP_PRIVATE_KEY": pem,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    with (
        patch("boto3.client") as aws,
        patch("adapters.github.remote_build.GitHubApi.call") as github,
        patch("adapters.state.postgres.RotatingDatabaseConnection") as db,
    ):
        main(["worker", "--mode", "build", "--check-config"])
    aws.assert_not_called()
    github.assert_not_called()
    db.assert_not_called()
    assert pem not in capsys.readouterr().out


def test_build_entrypoint_reports_sanitized_failure(capsys):
    from interfaces.b_runtime import main

    with (
        patch("interfaces.b_build_runtime.run_build_consumer", side_effect=ValueError("private-key")),
        pytest.raises(SystemExit),
    ):
        main(["worker", "--mode", "build"])
    assert "private-key" not in capsys.readouterr().err


def test_builder_runs_verified_source_in_real_local_docker(build_request, tmp_path, monkeypatch):
    import shutil
    import subprocess

    from application.deployment_core import source_digest
    from application.source_artifacts import SourceArtifactService
    from domain.access import LoginSource, Principal, Role
    from scripts.remote_builder import build

    if not shutil.which("docker"):
        pytest.skip("Docker unavailable")
    if subprocess.run(
        ["docker", "image", "inspect", "node:22-bookworm-slim"], check=False, capture_output=True
    ).returncode:
        pytest.skip("Only an already-cached base image is used")
    project = tmp_path / "project"
    project.mkdir()
    dockerfile = 'FROM node:22-bookworm-slim\nWORKDIR /app\nCOPY server.js /app/server.js\nUSER node\nCMD ["node","server.js"]\n'
    (project / "Dockerfile").write_text(dockerfile)
    (project / "server.js").write_text("console.log('verified-build-fixture')")

    class Objects:
        def put(self, artifact, data):
            self.data = data

    objects = Objects()
    principal = Principal("alice", "team", Role.DEPLOYER, LoginSource.CORPORATE_SSO)
    artifact = SourceArtifactService(objects).capture_prepared(
        principal,
        SourceArtifact("team", "game", "a" * 32, "original", "b" * 64, 100, source_digest(project)),
        project,
        expected_digest=source_digest(project),
    )
    plan = {
        "target": "aws",
        "replicas": 1,
        "source_digest": artifact.source_digest,
        "runtime": "custom-dockerfile",
        "start_command": "Dockerfile CMD",
        "port": 8080,
        "build_command": None,
        "dockerfile_source": "existing",
        "dockerfile": dockerfile,
    }
    value = {
        **build_request,
        "source_ref": artifact.record(),
        "source_digest": artifact.source_digest,
        "approved_plan_json": canonical(plan).decode(),
        "plan_digest": digest(plan),
    }
    (tmp_path / "request.json").write_bytes(canonical(value))
    (tmp_path / "source.zip").write_bytes(objects.data)
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, setting in {
        "BUILD_ID": value["build_id"],
        "REQUEST_DIGEST": digest(value),
        "GITHUB_REPOSITORY": value["repository"],
        "GITHUB_SHA": value["workflow_sha"],
        "SKY_PLATFORM_CODE_SHA": value["platform_code_sha"],
    }.items():
        monkeypatch.setenv(name, setting)
    image = "sky-build:" + value["build_id"]
    try:
        build(tmp_path, tmp_path / "output")
        assert (tmp_path / "output/image.tar").stat().st_size > 0
        result = subprocess.run(
            ["docker", "run", "--rm", "--network", "none", image],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0 and result.stdout.strip() == "verified-build-fixture"
    finally:
        subprocess.run(["docker", "image", "rm", image], check=False, capture_output=True)


@pytest.mark.parametrize(
    "uri",
    [
        "http://169.254.170.2",
        "http://169.254.170.2:51679",
        "http://169.254.170.2/api/e54c6fb442cd460d83b0c565a03affa6-3812634470",
    ],
)
def test_task_protection_accepts_fargate_base_and_preserves_task_path(uri):
    protection = EcsTaskProtection(uri)
    response = Mock()
    response.read.return_value = b'{"protection":{"ProtectionEnabled":true}}'
    protection.opener = Mock()
    protection.opener.open.return_value.__enter__ = Mock(return_value=response)
    protection.opener.open.return_value.__exit__ = Mock(return_value=False)
    protection.set(True)
    request = protection.opener.open.call_args.args[0]
    assert request.full_url == uri + "/task-protection/v1/state"
    assert request.method == "PUT"
    assert json.loads(request.data) == {"ProtectionEnabled": True, "ExpiresInMinutes": 5}


@pytest.mark.parametrize(
    "uri",
    [
        "http://169.254.170.2/api/../credentials",
        "http://169.254.170.2/api/e54c6fb442cd460d83b0c565a03affa6-3812634470?redirect=evil",
        "http://169.254.170.2/api/e54c6fb442cd460d83b0c565a03affa6-3812634470#fragment",
        "http://169.254.170.2/api/not-a-task",
    ],
)
def test_task_protection_rejects_non_task_paths(uri):
    with pytest.raises(ValueError):
        EcsTaskProtection(uri)
