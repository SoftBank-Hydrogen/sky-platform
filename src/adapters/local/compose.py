"""Same-host Docker Compose deployment with Sky-owned image and volume."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from adapters.build.image import ImageBuilder
from adapters.local.docker import LocalDockerAdapter
from application.deployment_core import DeploymentPlan, source_digest, validate_environment
from application.source_secrets import reject_supplied_secrets_in_source
from application.execution import ExecutionCapabilities
from engine.compatibility import TARGET_CAPABILITIES


class LocalComposeAdapter(LocalDockerAdapter):
    @staticmethod
    def execution_capabilities() -> ExecutionCapabilities:
        declared = TARGET_CAPABILITIES["onprem-compose"]
        return ExecutionCapabilities(
            target="onprem-compose",
            access_modes=frozenset(declared["access_modes"]),
            sqlite_volume=declared["sqlite_volume"],
            postgresql_binding=declared["postgresql_binding"],
            remote_host=declared["remote_host"],
        )

    @staticmethod
    def unavailable_reason() -> str | None:
        try:
            result = subprocess.run(
                ["docker", "compose", "version", "--short"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "Docker Compose CLI를 사용할 수 없습니다."
        return None if result.returncode == 0 else "Docker Compose CLI를 사용할 수 없습니다."

    def __init__(self, event, state_dir: Path, *, sqlite_binding: dict | None = None):
        super().__init__(event, sqlite_binding=sqlite_binding)
        self.state_dir = state_dir
        self.compose_file = None
        self.compose_digest = None

    def _compose(self, attempt_id: str, *args: str, environment: dict | None = None) -> str:
        if self.compose_file is None:
            raise ValueError("Compose 설정 파일이 없습니다.")
        command = ["docker", "compose", "-p", f"sky-{attempt_id}", "-f", str(self.compose_file), *args]
        self.event("command", "docker compose " + " ".join(args))
        process_env = os.environ.copy()
        process_env.update(environment or {})
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=300, env=process_env, check=False
        )
        if result.returncode:
            message = result.stderr[-1000:]
            for value in sorted(set((environment or {}).values()), key=len, reverse=True):
                if value:
                    message = message.replace(value, "[REDACTED]")
            raise RuntimeError("Docker Compose 실행 실패: " + message)
        return result.stdout.strip()

    def _write_compose(
        self, attempt_id: str, image: str, plan: DeploymentPlan, host_port: int, environment: dict
    ) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / f"compose-{attempt_id}.json"
        if path.exists() or path.is_symlink():
            raise ValueError("Compose 설정 파일이 이미 존재합니다.")
        service = {
            "image": image,
            "container_name": f"sky-{attempt_id}",
            "labels": {"app": "sky", "sky-attempt": attempt_id},
            "network_mode": "bridge",
            "ports": [f"127.0.0.1:{host_port}:{plan.port}"],
            "environment": {"PORT": str(plan.port), **{name: "${" + name + ":?}" for name in environment}},
            "restart": "unless-stopped",
            "mem_limit": "256m",
            "cpus": 1,
            "pids_limit": 128,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
        }
        configuration = {"services": {"app": service}}
        if self.sqlite_binding:
            name = self.sqlite_binding["volume_name"]
            service["volumes"] = [f"{name}:{self.sqlite_binding['mount_path']}"]
            configuration["volumes"] = {name: {"external": True}}
        content = json.dumps(configuration, ensure_ascii=False, sort_keys=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        self.compose_file = path
        self.compose_digest = hashlib.sha256(content.encode()).hexdigest()

    def deploy(
        self, project: Path, plan: DeploymentPlan, attempt_id: str, environment: dict | None = None
    ) -> dict:
        if not re.fullmatch(r"[a-f0-9]{16}-a[1-3]", attempt_id):
            raise ValueError("Compose 배포 시도 ID가 올바르지 않습니다.")
        if not plan.source_digest or source_digest(project) != plan.source_digest:
            raise ValueError("배포 설정 이후 소스가 변경됐습니다.")
        environment = validate_environment(environment, plan.required_env)
        reject_supplied_secrets_in_source(project, environment)
        image = f"sky/{attempt_id}:latest"
        name = f"sky-{attempt_id}"
        if self.inspect_resource("container", name) or self.inspect_resource("image", image):
            raise ValueError("같은 이름의 Docker 리소스가 이미 있습니다.")
        ImageBuilder(self.command, self.event).build(project, plan, image)
        self.prepare_sqlite_volume()
        host_port = self.available_loopback_port()
        self._write_compose(attempt_id, image, plan, host_port, environment)
        self._compose(attempt_id, "config", "-q", environment=environment)
        self._compose(attempt_id, "up", "-d", "--no-build", environment=environment)
        container = self.inspect_resource("container", name)
        labels = (container or {}).get("Config", {}).get("Labels") or {}
        if (
            not container
            or labels.get("app") != "sky"
            or labels.get("sky-attempt") != attempt_id
            or labels.get("com.docker.compose.project") != name
            or container.get("Config", {}).get("Image") != image
        ):
            raise RuntimeError("Compose 컨테이너의 소유권 또는 이미지가 예상과 다릅니다.")
        if self.sqlite_binding:
            mounts = container.get("Mounts") or []
            if not any(
                item.get("Type") == "volume"
                and item.get("Name") == self.sqlite_binding["volume_name"]
                and item.get("Destination") == self.sqlite_binding["mount_path"]
                for item in mounts
            ):
                raise RuntimeError("Compose SQLite 볼륨 연결을 확인하지 못했습니다.")
        url = f"http://127.0.0.1:{host_port}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(30):
            try:
                with opener.open(url + plan.health_path, timeout=2) as response:
                    if response.status == 200:
                        return {
                            "url": url,
                            "health_url": url + plan.health_path,
                            "container": name,
                            "image": image,
                            "compose_project": name,
                            "compose_sha256": self.compose_digest,
                            **(
                                {
                                    "sqlite_volume": self.sqlite_binding["volume_name"],
                                    "sqlite_mount": self.sqlite_binding["mount_path"],
                                }
                                if self.sqlite_binding
                                else {}
                            ),
                        }
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(1)
        raise RuntimeError("Compose 앱의 HTTP 200 확인 시간이 초과됐습니다.")

    def _checked_compose_file(self, attempt_id: str, digest: str) -> None:
        path = self.state_dir / f"compose-{attempt_id}.json"
        if (
            not re.fullmatch(r"[a-f0-9]{16}-a[1-3]", attempt_id)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or path.is_symlink()
            or not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != digest
        ):
            raise ValueError("저장된 Compose 설정의 무결성을 확인하지 못했습니다.")
        self.compose_file = path

    def _down(self, attempt_id: str) -> None:
        configuration = json.loads(self.compose_file.read_text(encoding="utf-8"))
        environment = configuration["services"]["app"]["environment"]
        placeholders = {name: "unused" for name in environment if name != "PORT"}
        self._compose(attempt_id, "down", "--remove-orphans", environment=placeholders)

    def cleanup_failure(self, attempt_id: str) -> None:
        name = f"sky-{attempt_id}"
        container = self.inspect_resource("container", name)
        labels = (container or {}).get("Config", {}).get("Labels") or {}
        if container and (
            labels.get("app") != "sky"
            or labels.get("sky-attempt") != attempt_id
            or labels.get("com.docker.compose.project") != name
        ):
            raise ValueError("Compose 실패 리소스의 소유권을 확인하지 못했습니다.")
        if self.compose_file is not None:
            self._checked_compose_file(attempt_id, self.compose_digest)
            self._down(attempt_id)
            self.compose_file.unlink()
        image = f"sky/{attempt_id}:latest"
        inspected = self.inspect_resource("image", image)
        if inspected:
            if (
                image not in (inspected.get("RepoTags") or [])
                or (inspected.get("Config", {}).get("Labels") or {}).get("app") != "sky"
            ):
                raise ValueError("Compose 이미지 소유권을 확인하지 못했습니다.")
            self.command(["docker", "image", "rm", image], timeout=30)

    def retire(self, result: dict, job_id: str) -> None:
        attempt_id = result.get("container", "").removeprefix("sky-")
        if (
            not re.fullmatch(re.escape(job_id) + r"-a[1-3]", attempt_id)
            or result.get("compose_project") != f"sky-{attempt_id}"
        ):
            raise ValueError("Compose 배포 소유권 기록이 올바르지 않습니다.")
        self._checked_compose_file(attempt_id, result.get("compose_sha256"))
        container = self.inspect_resource("container", f"sky-{attempt_id}")
        labels = (container or {}).get("Config", {}).get("Labels") or {}
        if container and (
            labels.get("app") != "sky"
            or labels.get("sky-attempt") != attempt_id
            or labels.get("com.docker.compose.project") != result["compose_project"]
        ):
            raise ValueError("Compose 컨테이너 소유권이 변경됐습니다.")
        self._down(attempt_id)
        super().retire(result, job_id)
        self.compose_file.unlink()

    def retire_orphan(self, attempt_id: str) -> None:
        """Remove only a bounded interrupted attempt; keep the application volume."""
        if not re.fullmatch(r"[a-f0-9]{16}-a[1-3]", attempt_id):
            raise ValueError("Compose 배포 시도 ID가 올바르지 않습니다.")
        name = f"sky-{attempt_id}"
        image = f"sky/{attempt_id}:latest"
        container = self.inspect_resource("container", name)
        labels = (container or {}).get("Config", {}).get("Labels") or {}
        if container:
            if (
                labels.get("app") != "sky"
                or labels.get("sky-attempt") != attempt_id
                or labels.get("com.docker.compose.project") != name
                or container.get("Config", {}).get("Image") != image
            ):
                raise ValueError("중단된 Compose 컨테이너의 소유권을 확인하지 못했습니다.")
            self.command(["docker", "rm", "-f", name], timeout=30)
        inspected = self.inspect_resource("image", image)
        if inspected:
            if (
                image not in (inspected.get("RepoTags") or [])
                or (inspected.get("Config", {}).get("Labels") or {}).get("app") != "sky"
            ):
                raise ValueError("중단된 Compose 이미지의 소유권을 확인하지 못했습니다.")
            self.command(["docker", "image", "rm", image], timeout=30)
        path = self.state_dir / f"compose-{attempt_id}.json"
        if path.is_symlink():
            raise ValueError("중단된 Compose 설정이 심볼릭 링크입니다.")
        if path.is_file():
            configuration = json.loads(path.read_text(encoding="utf-8"))
            service = configuration.get("services", {}).get("app", {})
            if (
                service.get("container_name") != name
                or service.get("image") != image
                or service.get("network_mode") != "bridge"
            ):
                raise ValueError("중단된 Compose 설정의 소유권을 확인하지 못했습니다.")
            path.unlink()
