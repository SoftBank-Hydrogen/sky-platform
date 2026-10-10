"""Trusted sky-builder runner. User code executes only in the credential-free build job."""

import argparse
import base64
import json
import os
import subprocess
import sys
from dataclasses import fields
from pathlib import Path

from adapters.aws.build_objects import S3BuildObjects
from adapters.aws.source_artifacts import S3ArtifactSettings, S3SourceArtifactStore
from adapters.build.image import ImageBuilder
from application.deployment_core import DeploymentPlan
from application.source_artifacts import SourceArtifactService
from domain.access import LoginSource, Principal, Role
from ports.remote_builds import canonical, digest, identity, request_key, validate_request, validate_result


def command(args, timeout=1200):
    # Capture bounded diagnostics on disk; never print user build output or credentials.
    import tempfile

    with tempfile.TemporaryFile(mode="w+") as output:
        result = subprocess.run(
            args, check=False, stdout=output, stderr=subprocess.STDOUT, text=True, timeout=timeout
        )
        output.seek(0)
        text = output.read(65536)
    if (
        result.returncode
        and args[:3] == ["docker", "image", "inspect"]
        and "--platform" in args
        and "unknown flag: --platform" in text
    ):
        # Old CLI: still check the built single-platform image's actual OS/architecture.
        selected = args.index("--platform")
        return command(args[:selected] + args[selected + 2 :], timeout)
    if result.returncode:
        raise OSError("Builder command failed")
    return text


def inputs(directory):
    request = json.loads((directory / "request.json").read_bytes())
    artifact, plan = validate_request(request)
    if digest(request) != os.environ["REQUEST_DIGEST"] or request["build_id"] != identity(
        os.environ["BUILD_ID"]
    ):
        raise ValueError("Builder request digest/identity mismatch")
    if (request["repository"], request["workflow_sha"], request["platform_code_sha"]) != (
        os.environ["GITHUB_REPOSITORY"],
        os.environ["GITHUB_SHA"],
        os.environ["SKY_PLATFORM_CODE_SHA"],
    ):
        raise ValueError("Builder workflow/code provenance mismatch")
    return request, artifact, plan


def prepare(directory):
    # Scope and request-key restrictions precede S3 object reads.
    identity(os.environ["BUILD_ID"])
    settings = S3ArtifactSettings(
        os.environ["SKY_ARTIFACTS_BUCKET"], os.environ["AWS_REGION"], os.environ["SKY_AWS_ACCOUNT_ID"]
    )
    key = os.environ["REQUEST_KEY"]
    import re

    if not re.fullmatch(
        r"sources/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[0-9a-f]{32}/build-requests/[0-9a-f-]{36}\.json", key
    ):
        raise ValueError("Invalid immutable build request key")
    request = S3BuildObjects(settings).read(key)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "request.json").write_bytes(canonical(request))
    request, artifact, _ = inputs(directory)
    if (request["bucket"], request["account_id"], request["region"]) != (
        settings.bucket,
        settings.account_id,
        settings.region,
    ) or request_key(request) != key:
        raise ValueError("Builder AWS/source scope mismatch")
    (directory / "source.zip").write_bytes(S3SourceArtifactStore(settings).get(artifact))


def build(directory, output):
    request, artifact, raw_plan = inputs(directory)
    # This phase must run in a separate VM/job with no cloud credentials or OIDC permission.
    if any(
        os.environ.get(name)
        for name in (
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
            "ACTIONS_ID_TOKEN_REQUEST_URL",
        )
    ):
        raise ValueError("Build job must not carry AWS/OIDC credentials")
    payload = (directory / "source.zip").read_bytes()

    class Objects:
        def get(self, selected):
            if selected != artifact:
                raise ValueError("Unexpected build source")
            return payload

    principal = Principal(
        request["created_by"], artifact.organization_id, Role.DEPLOYER, LoginSource.CORPORATE_SSO
    )
    plan = DeploymentPlan(
        **{
            key: value
            for key, value in raw_plan.items()
            if key in {field.name for field in fields(DeploymentPlan)}
        }
    )
    image = "sky-build:" + request["build_id"]
    with SourceArtifactService(Objects()).restore(principal, artifact) as project:
        ImageBuilder(command, lambda *_: None).build(project, plan, image, platform="linux/amd64")
    output.mkdir(parents=True, exist_ok=True)
    command(["docker", "save", "-o", str(output / "image.tar"), image])


def publish(directory, output):
    import boto3
    from botocore.exceptions import ClientError

    request, _, _ = inputs(directory)
    settings = S3ArtifactSettings(
        os.environ["SKY_ARTIFACTS_BUCKET"], os.environ["AWS_REGION"], os.environ["SKY_AWS_ACCOUNT_ID"]
    )
    if (request["bucket"], request["account_id"], request["region"]) != (
        settings.bucket,
        settings.account_id,
        settings.region,
    ):
        raise ValueError("Publish AWS scope mismatch")
    if boto3.client("sts").get_caller_identity()["Account"] != settings.account_id:
        raise ValueError("Unexpected publish account")
    ecr = boto3.client("ecr", region_name=settings.region)
    objects = S3BuildObjects(settings)
    result_key = f"builds/{request['build_id']}/result.json"
    # app-builder may write builds/ but only read sources/. Existing image tags
    # therefore require reconciliation rather than reading/replacing a prior result.
    registry = f"{settings.account_id}.dkr.ecr.{settings.region}.amazonaws.com"
    auth = ecr.get_authorization_token(registryIds=[settings.account_id])["authorizationData"][0]
    username, password = base64.b64decode(auth["authorizationToken"]).decode().split(":", 1)
    login = subprocess.run(
        ["docker", "login", "--username", username, "--password-stdin", registry],
        check=False,
        input=password,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if login.returncode:
        raise OSError("Builder registry login failed")
    tag = "build-" + request["build_id"]
    try:
        found = ecr.describe_images(
            registryId=settings.account_id, repositoryName="sky-managed", imageIds=[{"imageTag": tag}]
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ImageNotFoundException":
            raise
        found = None
    if found is not None:
        # Crash after push: never overwrite an existing tag with a potentially different rebuild.
        raise ValueError("Existing image without result requires explicit reconciliation")
    local = "sky-build:" + request["build_id"]
    command(["docker", "load", "-i", str(output / "image.tar")])
    if (
        command(["docker", "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", local]).strip()
        != "linux/amd64"
    ):
        raise ValueError("Unexpected image architecture")
    destination = registry + "/sky-managed:" + tag
    command(["docker", "tag", local, destination])
    command(["docker", "push", destination], timeout=300)
    image_digest = ecr.describe_images(
        registryId=settings.account_id, repositoryName="sky-managed", imageIds=[{"imageTag": tag}]
    )["imageDetails"][0]["imageDigest"]
    result = {
        "version": 1,
        "build_id": request["build_id"],
        "request_digest": digest(request),
        "source_digest": request["source_digest"],
        "plan_digest": request["plan_digest"],
        "platform": "linux/amd64",
        "repository": request["repository"],
        "workflow_sha": request["workflow_sha"],
        "platform_code_sha": request["platform_code_sha"],
        "run_id": int(os.environ["GITHUB_RUN_ID"]),
        "image_digest": image_digest,
        "image": registry + "/sky-managed@" + image_digest,
    }
    validate_result(result, request, result["run_id"])
    objects.put(result_key, result)
    command(["docker", "logout", registry], timeout=30)


def main():
    from botocore.exceptions import BotoCoreError, ClientError

    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("prepare", "build", "publish"))
    parser.add_argument("--input", type=Path, default=Path("build-input"))
    parser.add_argument("--output", type=Path, default=Path("build-output"))
    args = parser.parse_args()
    try:
        if args.mode == "prepare":
            prepare(args.input)
        elif args.mode == "build":
            build(args.input, args.output)
        else:
            publish(args.input, args.output)
    except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired, BotoCoreError, ClientError):
        sys.exit("Remote builder failed; inspect scope, inputs and immutable build state.")


if __name__ == "__main__":
    main()
