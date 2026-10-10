"""Deploy a Compose service to a configured Linux VM over Docker's SSH transport."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from adapters.local.compose import LocalComposeAdapter
from application.execution import ExecutionCapabilities
from engine.compatibility import TARGET_CAPABILITIES


_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\Z")
_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")


@dataclass(frozen=True)
class VmSettings:
    ssh_host: str
    ssh_user: str
    public_host: str

    @classmethod
    def from_environment(cls) -> VmSettings:
        return cls(os.getenv("SKY_VM_SSH_HOST", ""), os.getenv("SKY_VM_SSH_USER", ""),
                   os.getenv("SKY_VM_PUBLIC_HOST", ""))

    def validate(self) -> None:
        if not _USER.fullmatch(self.ssh_user):
            raise ValueError("SKY_VM_SSH_USER에 유효한 Linux 계정을 지정하세요.")
        for value, name in ((self.ssh_host, "SKY_VM_SSH_HOST"),
                            (self.public_host, "SKY_VM_PUBLIC_HOST")):
            if not _HOST.fullmatch(value) or ".." in value:
                raise ValueError(f"{name}에 유효한 DNS 이름 또는 IPv4 주소를 지정하세요.")
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                continue
            if address.is_loopback or address.is_unspecified or address.is_multicast:
                raise ValueError(f"{name}에 VM에서 접근 가능한 주소를 지정하세요.")

    @property
    def docker_host(self) -> str:
        self.validate()
        return f"ssh://{self.ssh_user}@{self.ssh_host}"


class RemoteVmComposeAdapter(LocalComposeAdapter):
    @staticmethod
    def execution_capabilities() -> ExecutionCapabilities:
        declared = TARGET_CAPABILITIES["onprem-vm"]
        return ExecutionCapabilities("onprem-vm", frozenset(declared["access_modes"]),
                                     sqlite_volume=False, remote_host=True)

    @staticmethod
    def unavailable_reason(settings: VmSettings | None = None) -> str | None:
        settings = settings or VmSettings.from_environment()
        try:
            settings.validate()
        except ValueError as exc:
            return str(exc)
        if shutil.which("ssh") is None:
            return "SSH CLI를 사용할 수 없습니다."
        return LocalComposeAdapter.unavailable_reason()

    def __init__(self, event, state_dir: Path, settings: VmSettings):
        settings.validate()
        super().__init__(event, state_dir)
        self.settings = settings

    def _docker_environment(self) -> dict[str, str]:
        return {"DOCKER_HOST": self.settings.docker_host}

    def command(self, args: list[str], timeout: int = 300, quiet: bool = False) -> str:
        if not quiet:
            self.event("command", " ".join(args))
        process_env = os.environ.copy()
        process_env.update(self._docker_environment())
        process_env.pop("DOCKER_CONTEXT", None)
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                                env=process_env, check=False)
        if result.stdout and not quiet:
            self.event("output", result.stdout[-12000:])
        if result.stderr and not quiet:
            self.event("output", result.stderr[-12000:])
        if result.returncode:
            raise RuntimeError(f"{args[0]} {args[1]} failed (exit {result.returncode}); see logs")
        return result.stdout.strip()

    def inspect_resource(self, kind: str, name: str) -> dict | None:
        # A connection error must never be mistaken for an absent resource.
        process_env = os.environ.copy()
        process_env.update(self._docker_environment())
        process_env.pop("DOCKER_CONTEXT", None)
        result = subprocess.run(["docker", kind, "inspect", name], capture_output=True,
                                text=True, timeout=20, env=process_env, check=False)
        if result.returncode:
            if any(message in result.stderr.lower() for message in
                   ("no such object", "no such image", "no such container", "no such volume")):
                return None
            raise RuntimeError("원격 Docker 상태를 확인하지 못했습니다: " + result.stderr[-300:])
        try:
            items = json.loads(result.stdout)
        except ValueError:
            raise RuntimeError("원격 Docker 상태 응답이 올바르지 않습니다.") from None
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise RuntimeError("원격 Docker 리소스 하나를 확인할 수 없습니다.")
        return items[0]

    def _host_port(self) -> None:
        # Let the remote daemon allocate a port; a local free port says nothing about the VM.
        return None

    def _published_url(self, attempt_id: str, container_port: int, host_port: int | None) -> str:
        output = self.command(["docker", "port", f"sky-{attempt_id}", f"{container_port}/tcp"], quiet=True)
        ports = set()
        for line in output.splitlines():
            match = re.fullmatch(r"0\.0\.0\.0:(\d{1,5})", line.strip())
            if match:
                port = int(match.group(1))
                if 1 <= port <= 65535:
                    ports.add(port)
        if len(ports) != 1:
            raise RuntimeError("VM의 공개 Docker 포트를 확인하지 못했습니다.")
        return f"http://{self.settings.public_host}:{ports.pop()}"

    def _result_identity(self) -> dict:
        return {"vm_ssh_host": self.settings.ssh_host, "vm_ssh_user": self.settings.ssh_user,
                "vm_public_host": self.settings.public_host}

    def _verify_result_identity(self, result: dict) -> None:
        if any(result.get(key) != value for key, value in self._result_identity().items()):
            raise ValueError("배포 기록의 VM과 현재 설정이 달라 종료를 중단합니다.")
