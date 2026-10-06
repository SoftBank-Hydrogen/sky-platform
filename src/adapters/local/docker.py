"""Execute and verify a deployment on a local Docker host."""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from adapters.build.image import ImageBuilder
from application.deployment_core import DeploymentPlan, source_digest, validate_environment


class LocalDockerAdapter:
    def __init__(self, event):
        self.event = event

    def command(self, args: list[str], timeout: int = 300) -> str:
        self.event("command", " ".join(args))
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if result.stdout:
            self.event("output", result.stdout[-12000:])
        if result.stderr:
            self.event("output", result.stderr[-12000:])
        if result.returncode:
            raise RuntimeError(f"{args[0]} {args[1]} failed (exit {result.returncode}); see logs")
        return result.stdout.strip()

    def deploy(self, project: Path, plan: DeploymentPlan, job_id: str,
               environment: dict | None = None) -> dict:
        if not plan.source_digest or source_digest(project) != plan.source_digest:
            raise ValueError("Source changed after analysis; upload and analyze again")
        environment = validate_environment(environment, plan.required_env)
        original_event = self.event
        def masked_event(stage, message):
            for value in sorted(set(environment.values()), key=len, reverse=True):
                if value:
                    message = message.replace(value, "[REDACTED]")
            original_event(stage, message)
        self.event = masked_event
        name = f"sky-{job_id}"
        image = f"sky/{job_id}:latest"
        ImageBuilder(self.command, self.event).build(project, plan, image)
        created = False
        try:
            self.event("starting", "Starting container on a loopback-only random port")
            run_args = [
                "docker", "run", "-d", "--name", name, "--label", "app=sky",
                "--label", f"sky-attempt={job_id}",
                "--memory", "256m", "--cpus", "1", "--pids-limit", "128",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "-e", f"PORT={plan.port}", "-p", f"127.0.0.1::{plan.port}",
            ]
            if environment:
                # Private temporary file outside the build context; never store values in job state.
                with tempfile.NamedTemporaryFile(mode="w", prefix="sky-env-", encoding="utf-8") as env_file:
                    for key, value in environment.items():
                        env_file.write(f"{key}={value}\n")
                    env_file.flush()
                    self.command(run_args + ["--env-file", env_file.name, image])
            else:
                self.command(run_args + [image])
            created = True
            binding = self.command(["docker", "port", name, f"{plan.port}/tcp"])
            url = "http://" + binding.splitlines()[0]
            self.event("verifying", f"Checking HTTP response: {url}{plan.health_path}")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(30):
                try:
                    with opener.open(url + plan.health_path, timeout=1) as response:
                        if response.status == 200:
                            return {"url": url, "health_url": url + plan.health_path,
                                    "container": name, "image": image}
                except (OSError, urllib.error.URLError):
                    pass
                time.sleep(1)
            self.command(["docker", "logs", "--tail", "80", name])
            raise RuntimeError(f"App did not return HTTP 200 at {plan.health_path} within the readiness window")
        except Exception:
            if created:
                self.command(["docker", "rm", "-f", name], timeout=30)
            raise

    def cleanup_failure(self, attempt_id):
        for args in (["docker", "rm", "-f", f"sky-{attempt_id}"],
                     ["docker", "image", "rm", f"sky/{attempt_id}:latest"]):
            try:
                self.command(args, timeout=30)
            except Exception:
                self.event("cleanup", "리소스 정리 결과를 확인하지 못했습니다: " + args[-1])

    @staticmethod
    def inspect_resource(kind: str, name: str) -> dict | None:
        result = subprocess.run(["docker", kind, "inspect", name], capture_output=True, text=True, timeout=15)
        if result.returncode:
            if "No such object" in result.stderr or "No such image" in result.stderr or "No such container" in result.stderr:
                return None
            raise RuntimeError("Docker 리소스 상태를 확인하지 못했습니다: " + result.stderr.strip()[-300:])
        try:
            items = json.loads(result.stdout)
        except ValueError:
            raise RuntimeError("Docker 상태 응답이 올바르지 않습니다.") from None
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise RuntimeError("Docker 리소스 하나를 확인할 수 없습니다.")
        return items[0]

    def retire(self, result: dict, job_id: str) -> None:
        if not re.fullmatch(r"[a-f0-9]{16}", job_id):
            raise ValueError("Invalid local deployment job ID")
        names = {f"sky-{job_id}", *(f"sky-{job_id}-a{i}" for i in range(1, 4))}
        name = result.get("container")
        if name not in names:
            raise ValueError("Stored container identity is invalid")
        suffix = name.removeprefix("sky-")
        image = f"sky/{suffix}:latest"
        if result.get("image") != image:
            raise ValueError("Stored image identity is invalid")
        container = self.inspect_resource("container", name)
        if container is not None:
            labels = container.get("Config", {}).get("Labels") or {}
            if (container.get("Name") != "/" + name or labels.get("app") != "sky"
                    or labels.get("sky-attempt") not in {None, suffix}
                    or container.get("Config", {}).get("Image") != image):
                raise ValueError("Docker container ownership changed; refusing deletion")
            self.event("retiring", "관리 컨테이너를 종료합니다: " + name)
            self.command(["docker", "rm", "-f", name], timeout=30)
            if self.inspect_resource("container", name) is not None:
                raise RuntimeError("컨테이너 종료를 확인하지 못했습니다.")
        inspected_image = self.inspect_resource("image", image)
        if inspected_image is not None:
            if (image not in (inspected_image.get("RepoTags") or [])
                    or (inspected_image.get("Config", {}).get("Labels") or {}).get("app") != "sky"):
                raise ValueError("Docker image ownership changed; refusing deletion")
            self.event("retiring", "관리 이미지 태그를 삭제합니다: " + image)
            self.command(["docker", "image", "rm", image], timeout=30)
            if self.inspect_resource("image", image) is not None:
                raise RuntimeError("이미지 태그 삭제를 확인하지 못했습니다.")
