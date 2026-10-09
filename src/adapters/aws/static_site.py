"""Deploy a reviewed static bundle through a private S3 origin and CloudFront.

Each first release owns its own stack. Updating a release or using a shared
distribution needs a separate, explicit contract; this adapter never mutates
an existing stack to avoid silently replacing someone else's site.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from adapters.aws.ecs import AwsConfigurationError, AwsSettings
from application.deployment_core import source_digest
from application.infrastructure import inspect_infrastructure
from assets import ASSET_ROOT
from engine.static_site import assess_static_site

TEMPLATE = ASSET_ROOT / "infra" / "aws-static-site.json"
APP_ID = re.compile(r"[a-z][a-z0-9-]{2,30}\Z")
ATTEMPT_ID = re.compile(r"[a-f0-9]{16}-a[1-3]\Z")


class AwsStaticSiteAdapter:
    def __init__(self, settings: AwsSettings, *, command=None, sleeper=time.sleep,
                 checkpoint=None):
        settings.validate()
        if not settings.expected_account:
            raise AwsConfigurationError("정적 사이트 배포에는 AWS 계정 고정이 필요합니다.")
        self.settings = settings
        self.command = command or self._aws_command
        self.sleep = sleeper
        self.checkpoint = checkpoint or (lambda **_updates: None)

    def _aws_command(self, arguments: list[str], timeout: int = 300) -> dict:
        process = subprocess.run(
            ["aws", "--region", self.settings.region, *arguments],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        if process.returncode:
            raise AwsConfigurationError("AWS 정적 배포 명령 실패: " + process.stderr[-700:])
        if not process.stdout.strip():
            return {}
        try:
            result = json.loads(process.stdout)
        except ValueError:
            raise AwsConfigurationError("AWS 정적 배포 응답 형식이 올바르지 않습니다.") from None
        if not isinstance(result, dict):
            raise AwsConfigurationError("AWS 정적 배포 응답이 객체가 아닙니다.")
        return result

    def _identity(self) -> None:
        identity = self.command(["sts", "get-caller-identity"])
        if identity.get("Account") != self.settings.expected_account:
            raise AwsConfigurationError("AWS 계정이 정적 배포 대상 계정과 다릅니다.")

    @staticmethod
    def _names(application_id: str, attempt_id: str) -> str:
        if not APP_ID.fullmatch(application_id) or not ATTEMPT_ID.fullmatch(attempt_id):
            raise ValueError("정적 배포 앱 또는 시도 ID가 올바르지 않습니다.")
        return "sky-static-" + attempt_id

    def preflight(self, project: Path, application_id: str, attempt_id: str) -> dict:
        stack_name = self._names(application_id, attempt_id)
        assessment = assess_static_site(project, inspect_infrastructure(project))
        if assessment.status != "eligible":
            raise ValueError("정적 파일만으로 동작함을 확인하지 못했습니다: " + "; ".join(assessment.reasons))
        files = sorted(path for path in project.rglob("*") if path.is_file())
        if not files or len(files) > 500 or sum(path.stat().st_size for path in files) > 20 * 1024 * 1024:
            raise ValueError("정적 사이트는 파일 1~500개, 총 20 MiB 이하여야 합니다.")
        self._identity()
        return {"target": "aws-s3-cloudfront", "application_id": application_id,
                "attempt_id": attempt_id, "stack_name": stack_name,
                "file_count": len(files), "bytes": sum(path.stat().st_size for path in files),
                "evidence_files": list(assessment.evidence_files),
                "source_digest": source_digest(project)}

    def deploy(self, project: Path, application_id: str, attempt_id: str) -> dict:
        plan = self.preflight(project, application_id, attempt_id)
        if source_digest(project) != plan["source_digest"]:
            raise ValueError("정적 사이트 소스가 사전 검사 이후 변경됐습니다.")
        stack_name = plan["stack_name"]
        created = self.command([
            "cloudformation", "create-stack", "--stack-name", stack_name,
            "--template-body", "file://" + str(TEMPLATE),
            "--parameters", "ParameterKey=ApplicationId,ParameterValue=" + application_id,
            "ParameterKey=AttemptId,ParameterValue=" + attempt_id,
            "--tags", "Key=sky-managed,Value=true", "Key=sky-app,Value=" + application_id,
            "Key=sky-attempt,Value=" + attempt_id,
        ])
        stack_id = created.get("StackId")
        if not isinstance(stack_id, str) or not stack_id.startswith(
            f"arn:aws:cloudformation:{self.settings.region}:{self.settings.expected_account}:stack/{stack_name}/"
        ):
            raise AwsConfigurationError("새 정적 사이트 스택의 소유 ARN을 확인하지 못했습니다.")
        self.checkpoint(static_stack_id=stack_id)
        self.command(["cloudformation", "wait", "stack-create-complete", "--stack-name", stack_id], timeout=1800)
        described = self.command(["cloudformation", "describe-stacks", "--stack-name", stack_id])
        stacks = described.get("Stacks")
        if not isinstance(stacks, list) or len(stacks) != 1:
            raise AwsConfigurationError("정적 사이트 스택을 하나로 확인하지 못했습니다.")
        stack = stacks[0]
        tags = {item.get("Key"): item.get("Value") for item in stack.get("Tags", [])}
        if (stack.get("StackId") != stack_id or stack.get("StackStatus") != "CREATE_COMPLETE"
                or tags.get("sky-managed") != "true" or tags.get("sky-app") != application_id
                or tags.get("sky-attempt") != attempt_id):
            raise AwsConfigurationError("정적 사이트 스택의 소유권 또는 생성 상태가 예상과 다릅니다.")
        outputs = {item.get("OutputKey"): item.get("OutputValue") for item in stack.get("Outputs", [])}
        bucket = outputs.get("BucketName")
        distribution = outputs.get("DistributionId")
        domain = outputs.get("DomainName")
        if (bucket != f"sky-static-{self.settings.expected_account}-{self.settings.region}-{attempt_id}"
                or not isinstance(distribution, str) or not re.fullmatch(r"[A-Z0-9]{8,24}", distribution)
                or not isinstance(domain, str) or not re.fullmatch(r"[a-z0-9-]+\.cloudfront\.net", domain)):
            raise AwsConfigurationError("정적 사이트 스택 출력이 예상과 다릅니다.")
        for path in sorted(project.rglob("*")):
            if not path.is_file():
                continue
            key = path.relative_to(project).as_posix()
            content_type = mimetypes.guess_type(key)[0] or "application/octet-stream"
            if key.endswith((".js", ".mjs")):
                content_type = "text/javascript"
            self.command(["s3api", "put-object", "--bucket", bucket, "--key", key,
                          "--body", str(path), "--content-type", content_type,
                          "--cache-control", "no-cache" if key == "index.html" else "public, max-age=3600"],
                         timeout=120)
        if source_digest(project) != plan["source_digest"]:
            raise AwsConfigurationError("정적 사이트 업로드 중 소스가 변경됐습니다. 공개 성공으로 처리하지 않습니다.")
        expected = hashlib.sha256((project / "index.html").read_bytes()).hexdigest()
        url = f"https://{domain}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(60):
            try:
                request = urllib.request.Request(url + "/", headers={"Accept-Encoding": "identity"})
                with opener.open(request, timeout=10) as response:
                    if response.status == 200 and hashlib.sha256(response.read(1024 * 1024 + 1)).hexdigest() == expected:
                        return {"target": "aws-s3-cloudfront", "url": url,
                                "health_url": url + "/", "stack_id": stack_id,
                                "bucket": bucket, "distribution_id": distribution,
                                "application_id": application_id, "attempt_id": attempt_id,
                                "source_digest": plan["source_digest"],
                                "source_index_sha256": expected}
            except (OSError, urllib.error.URLError):
                pass
            self.sleep(5)
        raise AwsConfigurationError("CloudFront 공개 URL에서 원본 index.html 응답을 검증하지 못했습니다.")

    def retire(self, application_id: str, attempt_id: str, stack_id: str) -> dict:
        """Delete only a fully identified Sky stack and its private objects."""
        stack_name = self._names(application_id, attempt_id)
        prefix = (f"arn:aws:cloudformation:{self.settings.region}:"
                  f"{self.settings.expected_account}:stack/{stack_name}/")
        if not isinstance(stack_id, str) or not stack_id.startswith(prefix):
            raise AwsConfigurationError("정적 사이트 스택 ARN이 앱과 일치하지 않습니다.")
        self._identity()
        described = self.command(["cloudformation", "describe-stacks", "--stack-name", stack_id])
        stacks = described.get("Stacks")
        if not isinstance(stacks, list) or len(stacks) != 1:
            raise AwsConfigurationError("정리할 정적 사이트 스택을 하나로 확인하지 못했습니다.")
        stack = stacks[0]
        tags = {item.get("Key"): item.get("Value") for item in stack.get("Tags", [])}
        if (stack.get("StackId") != stack_id or stack.get("StackStatus") != "CREATE_COMPLETE"
                or tags.get("sky-managed") != "true" or tags.get("sky-app") != application_id
                or tags.get("sky-attempt") != attempt_id):
            raise AwsConfigurationError("정리할 정적 사이트 스택의 소유권·상태가 예상과 다릅니다.")
        outputs = {item.get("OutputKey"): item.get("OutputValue") for item in stack.get("Outputs", [])}
        bucket = outputs.get("BucketName")
        if bucket != f"sky-static-{self.settings.expected_account}-{self.settings.region}-{attempt_id}":
            raise AwsConfigurationError("정리할 S3 버킷 이름이 소유 스택과 다릅니다.")
        bucket_tags = self.command(["s3api", "get-bucket-tagging", "--bucket", bucket]).get("TagSet")
        ownership = {item.get("Key"): item.get("Value") for item in bucket_tags or []}
        if (ownership.get("sky-managed") != "true" or ownership.get("sky-app") != application_id
                or ownership.get("sky-attempt") != attempt_id):
            raise AwsConfigurationError("정리할 S3 버킷의 소유 태그가 예상과 다릅니다.")
        listing = self.command(["s3api", "list-objects-v2", "--bucket", bucket, "--max-keys", "1000"])
        objects = listing.get("Contents", [])
        if (listing.get("IsTruncated") or not isinstance(objects, list) or len(objects) > 500
                or any(not isinstance(item.get("Key"), str) for item in objects)):
            raise AwsConfigurationError("정적 사이트 객체 목록을 완전히 확인하지 못했습니다.")
        for item in objects:
            self.command(["s3api", "delete-object", "--bucket", bucket, "--key", item["Key"]])
        self.command(["cloudformation", "delete-stack", "--stack-name", stack_id])
        self.command(["cloudformation", "wait", "stack-delete-complete", "--stack-name", stack_id],
                     timeout=1800)
        return {"application_id": application_id, "attempt_id": attempt_id,
                "stack_id": stack_id, "status": "deleted", "objects_deleted": len(objects)}
