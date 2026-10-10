"""Immutable remote build requests and strict, digest-bound result contracts."""

import hashlib
import json
import re
from dataclasses import dataclass
from uuid import UUID

from ports.artifacts import SourceArtifact


def canonical(value):
    data = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    if len(data) > 65536:
        raise ValueError("Build document exceeds limit")
    return data


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def identity(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Invalid build identity")
    return value


@dataclass(frozen=True)
class BuildSettings:
    repository: str
    ref: str
    workflow_sha: str
    platform_code_sha: str
    bucket: str
    account_id: str
    region: str
    workflow: str = "build.yml"

    def __post_init__(self):
        import ipaddress

        if (
            not isinstance(self.bucket, str)
            or not 3 <= len(self.bucket) <= 63
            or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*[a-z0-9]", self.bucket)
            or any(value in self.bucket for value in ("..", ".-", "-."))
        ):
            raise ValueError("Invalid builder artifact bucket")
        try:
            ipaddress.ip_address(self.bucket)
        except ValueError:
            pass
        else:
            raise ValueError("Builder bucket cannot be an IP address")
        if (
            not isinstance(self.account_id, str)
            or not re.fullmatch(r"[0-9]{12}", self.account_id)
            or not isinstance(self.region, str)
            or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", self.region)
        ):
            raise ValueError("Invalid builder AWS scope")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository):
            raise ValueError("Invalid builder repository")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.ref) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+\.ya?ml", self.workflow
        ):
            raise ValueError("Invalid builder ref/workflow")
        if any(not re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (self.workflow_sha, self.platform_code_sha)):
            raise ValueError("Pin builder and platform code commits")

    @classmethod
    def from_environment(cls, env):
        return cls(
            *(
                env.get(name, "")
                for name in (
                    "SKY_BUILDER_REPOSITORY",
                    "SKY_BUILDER_REF",
                    "SKY_BUILDER_SHA",
                    "SKY_BUILDER_PLATFORM_SHA",
                    "SKY_ARTIFACTS_BUCKET",
                    "SKY_AWS_ACCOUNT_ID",
                    "SKY_AWS_REGION",
                )
            )
        )


def request_for(operation, workspace, settings):
    c = operation.command
    if operation.kind != "deploy" or c.get("version") != 1 or c.get("target") != "aws":
        raise ValueError("Only admitted AWS deployments are supported")
    if (c.get("account_id"), c.get("region")) != (settings.account_id, settings.region):
        raise ValueError("Build destination differs from admitted scope")
    artifact = SourceArtifact.from_record(c["source_ref"])
    if (
        artifact.kind != "prepared"
        or c["organization_id"] != artifact.organization_id
        or c["source_digest"] != artifact.source_digest
    ):
        raise ValueError("Build source differs from approval")
    identity(c["approval_id"])
    text = c["approved_plan_json"]
    if not isinstance(text, str) or len(text.encode()) > 65536:
        raise ValueError("Stored canonical plan is required")
    plan = json.loads(text)
    if digest(plan) != c["plan_digest"] or plan.get("source_digest") != artifact.source_digest:
        raise ValueError("Build plan differs from approval")
    if plan.get("target") != "aws" or plan.get("replicas") != 1:
        raise ValueError("Unsupported deployment plan")
    document = {
        "version": 1,
        "build_id": identity(operation.id),
        "workspace": workspace,
        "job_id": c["job_id"],
        "created_by": c["created_by"],
        "source_ref": artifact.record(),
        "approved_plan_json": text,
        "plan_digest": c["plan_digest"],
        "source_digest": c["source_digest"],
        "bucket": settings.bucket,
        "account_id": settings.account_id,
        "region": settings.region,
        "repository": settings.repository,
        "workflow_sha": settings.workflow_sha,
        "platform_code_sha": settings.platform_code_sha,
    }
    validate_request(document)
    return document


def validate_request(request):
    fields = {
        "version",
        "build_id",
        "workspace",
        "job_id",
        "created_by",
        "source_ref",
        "approved_plan_json",
        "plan_digest",
        "source_digest",
        "bucket",
        "account_id",
        "region",
        "repository",
        "workflow_sha",
        "platform_code_sha",
    }
    if (
        not isinstance(request, dict)
        or set(request) != fields
        or type(request["version"]) is not int
        or request["version"] != 1
    ):
        raise ValueError("Invalid build request")
    canonical(request)
    identity(request["build_id"])
    for name in ("workspace", "created_by"):
        if not isinstance(request[name], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request[name]):
            raise ValueError("Invalid build scope")
    if not re.fullmatch(r"[0-9a-f]{16}", request["job_id"]):
        raise ValueError("Invalid build job")
    BuildSettings(
        request["repository"],
        "main",
        request["workflow_sha"],
        request["platform_code_sha"],
        request["bucket"],
        request["account_id"],
        request["region"],
    )
    artifact = SourceArtifact.from_record(request["source_ref"])
    plan = json.loads(request["approved_plan_json"])
    if (
        artifact.kind != "prepared"
        or artifact.source_digest != request["source_digest"]
        or digest(plan) != request["plan_digest"]
    ):
        raise ValueError("Invalid approved build source/plan")
    if (
        not isinstance(plan, dict)
        or plan.get("source_digest") != artifact.source_digest
        or plan.get("target") != "aws"
        or plan.get("replicas") != 1
    ):
        raise ValueError("Invalid approved plan scope")
    if (
        plan.get("dockerfile_source") not in {"existing", "generated"}
        or not isinstance(plan.get("dockerfile"), str)
        or not plan["dockerfile"]
    ):
        raise ValueError("Validated Dockerfile required")
    return artifact, plan


def request_key(request):
    artifact = SourceArtifact.from_record(request["source_ref"])
    return f"sources/{artifact.organization_id}/{artifact.application_id}/{artifact.upload_id}/build-requests/{request['build_id']}.json"


def validate_result(result, request, run_id):
    expected = {
        "version": 1,
        "build_id": request["build_id"],
        "request_digest": digest(request),
        "source_digest": request["source_digest"],
        "plan_digest": request["plan_digest"],
        "repository": request["repository"],
        "workflow_sha": request["workflow_sha"],
        "platform_code_sha": request["platform_code_sha"],
        "run_id": run_id,
        "platform": "linux/amd64",
    }
    if (
        not isinstance(result, dict)
        or set(result) != set(expected) | {"image", "image_digest"}
        or any(type(result.get(k)) is not type(v) or result[k] != v for k, v in expected.items())
    ):
        raise ValueError("Remote build result scope mismatch")
    if not isinstance(result["image_digest"], str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", result["image_digest"]
    ):
        raise ValueError("Invalid built image digest")
    image = f"{request['account_id']}.dkr.ecr.{request['region']}.amazonaws.com/sky-managed@{result['image_digest']}"
    if result["image"] != image:
        raise ValueError("Remote result points outside the managed repository")
    return result
