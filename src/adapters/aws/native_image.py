"""Build without AWS credentials, then publish to a release-owned ECR repository."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile

from adapters.build.image import ImageBuilder
from adapters.local.rehearsal import rehearse_image


class NativeImagePublisher:
    def __init__(self, adapter, checkpoint, event, command=None):
        self.adapter, self.checkpoint, self.event = adapter, checkpoint, event
        self.command = command or self._command

    @staticmethod
    def _command(arguments, timeout=600, stdin=None, **_options):
        # User Dockerfile runs on the builder; do not inherit AWS/OpenAI credentials
        # or the host's Docker login config into the build client environment.
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            in {
                "PATH",
                "HOME",
                "DOCKER_HOST",
                "DOCKER_CONTEXT",
                "DOCKER_TLS_VERIFY",
                "DOCKER_CERT_PATH",
                "TMPDIR",
            }
        }
        if not environment.get("DOCKER_HOST"):
            context = subprocess.run(
                ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env=environment,
            )
            if context.returncode or not context.stdout.strip():
                raise RuntimeError("Cannot resolve the configured Docker builder")
            environment["DOCKER_HOST"] = context.stdout.strip()
        environment.pop("DOCKER_CONTEXT", None)
        with tempfile.TemporaryDirectory(prefix="sky-native-build-auth-") as config:
            environment["DOCKER_CONFIG"] = config
            result = subprocess.run(
                arguments,
                input=stdin,
                text=True,
                capture_output=True,
                timeout=timeout,
                env=environment,
                check=False,
            )
        if result.returncode:
            raise RuntimeError("Native image build/publication command failed; no cloud deployment reported")
        return result.stdout

    def publish(self, project, plan, application_id, attempt_id):
        if not re.fullmatch(r"[a-f0-9]{16}-a[1-3]", attempt_id):
            raise ValueError("Invalid image release attempt")
        self.adapter.identity()
        settings = self.adapter.settings
        repository = "sky-native-" + attempt_id
        registry = f"{settings.expected_account}.dkr.ecr.{settings.region}.amazonaws.com"
        image = f"{registry}/{repository}:release"
        receipt = {
            "repository": repository,
            "account": settings.expected_account,
            "region": settings.region,
            "application_id": application_id,
            "attempt_id": attempt_id,
            "source_digest": plan.source_digest,
            "status": "creating",
        }
        self.checkpoint(dict(receipt))
        ecr = self.adapter.client("ecr")
        ecr.create_repository(
            repositoryName=repository,
            imageTagMutability="IMMUTABLE",
            imageScanningConfiguration={"scanOnPush": True},
            tags=[{"Key": key, "Value": value} for key, value in self.tags(receipt).items()],
        )
        ImageBuilder(self.command, self.event).build(project, plan, image, platform="linux/amd64")
        rehearsal = rehearse_image(self.command, self.event, image, plan, attempt_id, {})
        receipt["rehearsal"] = rehearsal
        self.checkpoint(dict(receipt))
        endpoint = (
            os.environ.get("DOCKER_HOST")
            or self.command(
                ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"]
            ).strip()
        )
        authorization = ecr.get_authorization_token(registryIds=[settings.expected_account])[
            "authorizationData"
        ]
        if len(authorization) != 1 or authorization[0]["proxyEndpoint"] != "https://" + registry:
            raise ValueError("ECR authentication belongs to another registry")
        import base64

        username, password = base64.b64decode(authorization[0]["authorizationToken"]).decode().split(":", 1)
        if username != "AWS":
            raise ValueError("Unexpected ECR authentication user")
        with tempfile.TemporaryDirectory(prefix="sky-native-push-auth-") as config:
            docker = ["docker", "--config", config, "--host", endpoint]
            self.command(
                docker + ["login", "--username", "AWS", "--password-stdin", registry], stdin=password
            )
            self.command(docker + ["push", image])
        described = ecr.describe_images(repositoryName=repository, imageIds=[{"imageTag": "release"}])[
            "imageDetails"
        ]
        if len(described) != 1 or not re.fullmatch(r"sha256:[a-f0-9]{64}", described[0]["imageDigest"]):
            raise ValueError("Published ECR digest is missing")
        manifest = ecr.batch_get_image(
            repositoryName=repository, imageIds=[{"imageDigest": described[0]["imageDigest"]}]
        )["images"]
        if (
            len(manifest) != 1
            or json.loads(manifest[0]["imageManifest"]).get("config", {}).get("digest")
            != rehearsal["image_id"]
        ):
            raise ValueError("Published image differs from the rehearsed image configuration")
        receipt.update(image=f"{registry}/{repository}@{described[0]['imageDigest']}", status="published")
        self.checkpoint(dict(receipt))
        return receipt

    @staticmethod
    def tags(receipt):
        return {
            "sky-managed": "true",
            "sky-app": receipt["application_id"],
            "sky-attempt": receipt["attempt_id"],
            "sky-source": receipt["source_digest"],
        }

    def destroy(self, receipt):
        self.adapter.identity()
        settings = self.adapter.settings
        if (
            receipt["account"] != settings.expected_account
            or receipt["region"] != settings.region
            or receipt["repository"] != "sky-native-" + receipt["attempt_id"]
            or not re.fullmatch(r"[a-f0-9]{16}-a[1-3]", receipt["attempt_id"])
        ):
            raise ValueError("Image repository ownership differs")
        ecr = self.adapter.client("ecr")
        try:
            result = ecr.describe_repositories(repositoryNames=[receipt["repository"]])["repositories"]
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") != "RepositoryNotFoundException":
                raise
            receipt = dict(receipt, status="deleted")
            self.checkpoint(receipt)
            return receipt
        if len(result) != 1:
            raise ValueError("Cannot identify release repository")
        arn = result[0]["repositoryArn"]
        expected = (
            f"arn:aws:ecr:{settings.region}:{settings.expected_account}:repository/{receipt['repository']}"
        )
        if arn != expected:
            raise ValueError("Foreign ECR repository")
        tags = {tag["Key"]: tag["Value"] for tag in ecr.list_tags_for_resource(resourceArn=arn)["tags"]}
        if any(tags.get(key) != value for key, value in self.tags(receipt).items()):
            raise ValueError("ECR release tags differ")
        ecr.delete_repository(repositoryName=receipt["repository"], force=True)
        receipt = dict(receipt, status="deleted")
        self.checkpoint(receipt)
        return receipt
