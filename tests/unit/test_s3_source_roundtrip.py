"""Full source service -> S3 adapter -> restored build workspace, with no AWS calls."""

import io
import zipfile

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from adapters.aws.source_artifacts import S3ArtifactSettings, S3SourceArtifactStore
from application.deployment_core import source_digest
from application.source_artifacts import SourceArtifactService
from domain.access import LoginSource, Principal, Role


class MemoryS3:
    def __init__(self):
        self.objects = {}
        self.timeout_after_first_put = False

    def put_object(self, **request):
        assert request["IfNoneMatch"] == "*"
        assert request["ExpectedBucketOwner"] == "977889523182"
        key = request["Key"]
        if key in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.objects[key] = request
        if self.timeout_after_first_put:
            self.timeout_after_first_put = False
            raise EndpointConnectionError(endpoint_url="simulated-ambiguous-send")
        return {}

    def get_object(self, **request):
        value = self.objects[request["Key"]]
        return {
            "Body": io.BytesIO(value["Body"]),
            "ContentLength": value["ContentLength"],
            "Metadata": value["Metadata"],
            "ServerSideEncryption": value["ServerSideEncryption"],
        }


def configured():
    s3 = MemoryS3()
    service = SourceArtifactService(
        S3SourceArtifactStore(
            S3ArtifactSettings("sky-test-artifacts", "ap-northeast-2", "977889523182"), client=s3
        )
    )
    principal = Principal("user1", "org1", Role.DEPLOYER, LoginSource.EXTERNAL_IDP)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("server.py", "print('hello')")
        archive.writestr(".env", "DO_NOT_PERSIST=secret")
    return service, principal, output.getvalue(), s3


def test_full_source_flow_never_changes_original():
    service, principal, upload, s3 = configured()
    original = service.capture_upload(principal, "game1", "a" * 32, upload)
    original_bytes = s3.objects[original.artifact.key]["Body"]
    with service.restore(principal, original.artifact) as project:
        assert not (project / ".env").exists()
        (project / "Dockerfile").write_text("FROM python:3.12-slim")
        digest = source_digest(project)
        prepared = service.capture_prepared(principal, original.artifact, project, expected_digest=digest)
    with service.restore(principal, prepared) as project:
        assert source_digest(project) == digest
        assert (project / "Dockerfile").is_file()
    assert len(s3.objects) == 2
    assert s3.objects[original.artifact.key]["Body"] == original_bytes


def test_retry_after_remote_acceptance_and_lost_response_does_not_duplicate():
    service, principal, upload, s3 = configured()
    s3.timeout_after_first_put = True
    with pytest.raises(OSError, match="uncertain"):
        service.capture_upload(principal, "game1", "a" * 32, upload)
    assert len(s3.objects) == 1
    retried = service.capture_upload(principal, "game1", "a" * 32, upload)
    assert retried.artifact.key in s3.objects and len(s3.objects) == 1
    with service.restore(principal, retried.artifact) as project:
        assert (project / "server.py").read_text() == "print('hello')"
