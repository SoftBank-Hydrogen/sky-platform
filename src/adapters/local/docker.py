"""Execute and verify a deployment on a local Docker host."""
from __future__ import annotations

import json
import re
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from adapters.build.image import ImageBuilder
from adapters.local.rehearsal import inspect_image_id
from application.deployment_core import DeploymentPlan, source_digest, validate_environment
from application.source_secrets import reject_supplied_secrets_in_source
from application.execution import ExecutionCapabilities
from engine.compatibility import TARGET_CAPABILITIES


class LocalDockerAdapter:
    @staticmethod
    def execution_capabilities() -> ExecutionCapabilities:
        declared = TARGET_CAPABILITIES["local-docker"]
        return ExecutionCapabilities(
            target="local-docker",
            access_modes=frozenset(declared["access_modes"]),
            sqlite_volume=declared["sqlite_volume"],
            remote_host=declared["remote_host"],
            postgresql_binding=declared["postgresql_binding"],
        )

    def __init__(self, event, *, platform: str | None = None,
                 sqlite_binding: dict | None = None):
        self.event = event
        self.platform = platform
        self.sqlite_binding = sqlite_binding

    def prepare_sqlite_volume(self) -> None:
        binding = self.sqlite_binding
        if binding is None:
            return
        name = binding["volume_name"]
        volume = self.inspect_resource("volume", name)
        if volume is None:
            self.command(["docker", "volume", "create",
                          "--label", "sky-managed=true",
                          "--label", f"sky-application={binding['application_id']}",
                          "--label", f"sky-mount={binding['mount_path']}", name], timeout=30)
            volume = self.inspect_resource("volume", name)
        labels = (volume or {}).get("Labels") or {}
        if (not volume or volume.get("Name") != name or volume.get("Driver") != "local"
                or labels.get("sky-managed") != "true"
                or labels.get("sky-application") != binding["application_id"]
                or labels.get("sky-mount") != binding["mount_path"]):
            raise ValueError("SQLite 볼륨 소유권·경로가 맞지 않아 배포를 중단합니다.")
        attached = self.command(
            ["docker", "ps", "-a", "-q", "--filter", f"volume={name}"], timeout=30, quiet=True)
        if attached:
            raise ValueError("이 앱의 SQLite 볼륨을 연결한 컨테이너가 있습니다. 기존 배포를 종료한 뒤 재배포하세요.")

    @staticmethod
    def available_loopback_port() -> int:
        # Docker's automatically allocated host port can change on restart.
        # Persist the mapping for a stateful local service.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]

    def command(self, args: list[str], timeout: int = 300, quiet: bool = False) -> str:
        if not quiet:
            self.event("command", " ".join(args))
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if result.stdout and not quiet:
            self.event("output", result.stdout[-12000:])
        if result.stderr and not quiet:
            self.event("output", result.stderr[-12000:])
        if result.returncode:
            raise RuntimeError(f"{args[0]} {args[1]} failed (exit {result.returncode}); see logs")
        return result.stdout.strip()

    def deploy(self, project: Path, plan: DeploymentPlan, job_id: str,
               environment: dict | None = None) -> dict:
        if not plan.source_digest or source_digest(project) != plan.source_digest:
            raise ValueError("Source changed after analysis; upload and analyze again")
        environment = validate_environment(environment, plan.required_env)
        reject_supplied_secrets_in_source(project, environment)
        original_event = self.event
        def masked_event(stage, message):
            for value in sorted(set(environment.values()), key=len, reverse=True):
                if value:
                    message = message.replace(value, "[REDACTED]")
            original_event(stage, message)
        self.event = masked_event
        name = f"sky-{job_id}"
        image = f"sky/{job_id}:latest"
        ImageBuilder(self.command, self.event).build(project, plan, image, platform=self.platform)
        image_id = inspect_image_id(self.command, image)
        created = False
        try:
            self.prepare_sqlite_volume()
            self.event("starting", "Starting container on a loopback-only port")
            host_port = self.available_loopback_port() if self.sqlite_binding else None
            run_args = [
                "docker", "run", "-d", "--name", name, "--label", "app=sky",
                "--label", f"sky-attempt={job_id}",
                "--memory", "256m", "--cpus", "1", "--pids-limit", "128",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "-e", f"PORT={plan.port}", "-p",
                f"127.0.0.1:{host_port}:{plan.port}" if host_port else f"127.0.0.1::{plan.port}",
            ]
            if self.sqlite_binding:
                binding = self.sqlite_binding
                run_args += ["--mount", f"type=volume,source={binding['volume_name']},target={binding['mount_path']}"]
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
            if self.sqlite_binding:
                container = self.inspect_resource("container", name)
                binding = self.sqlite_binding
                mounts = (container or {}).get("Mounts") or []
                if not any(mount.get("Type") == "volume"
                           and mount.get("Name") == binding["volume_name"]
                           and mount.get("Destination") == binding["mount_path"] for mount in mounts):
                    raise RuntimeError("SQLite 볼륨 연결을 확인하지 못했습니다.")
                attached = self.command(
                    ["docker", "ps", "-a", "-q", "--no-trunc",
                     "--filter", f"volume={binding['volume_name']}"],
                    timeout=30, quiet=True).splitlines()
                if len(attached) != 1 or attached[0] != container.get("Id"):
                    raise RuntimeError("SQLite 볼륨이 하나의 컨테이너에만 연결됐는지 확인하지 못했습니다.")
            binding = self.command(["docker", "port", name, f"{plan.port}/tcp"])
            url = "http://" + binding.splitlines()[0]
            if host_port and url != f"http://127.0.0.1:{host_port}":
                raise RuntimeError("SQLite 서비스의 고정 루프백 포트를 확인하지 못했습니다.")
            self.event("verifying", f"Checking HTTP response: {url}{plan.health_path}")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(30):
                try:
                    with opener.open(url + plan.health_path, timeout=1) as response:
                        if response.status == 200:
                            if image_id:
                                container = self.inspect_resource('container', name)
                                if (container is None or container.get('Image') != image_id
                                        or inspect_image_id(self.command, image) != image_id):
                                    raise RuntimeError('HTTP 검증 중 로컬 이미지 또는 컨테이너가 변경됐습니다.')
                            return {"url": url, "health_url": url + plan.health_path,
                                    "container": name, "image": image,
                                    **({"sqlite_volume": self.sqlite_binding["volume_name"],
                                        "sqlite_mount": self.sqlite_binding["mount_path"]}
                                       if self.sqlite_binding else {}),
                                    "image_id": image_id,
                                    **({"platform": self.platform} if self.platform else {})}
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
            if any(message in result.stderr.lower() for message in
                   ("no such object", "no such image", "no such container", "no such volume")):
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
