"""Small execution boundary for adapters still using the legacy deployment call."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from application.deployment_core import DeploymentPlan


@dataclass(frozen=True)
class ExecutionCapabilities:
    target: str
    access_modes: frozenset[str]
    sqlite_volume: bool = False
    postgresql_binding: bool = False
    remote_host: bool = False
    rollback: bool = False
    restart_drill: bool = False


@dataclass(frozen=True)
class ExecutionRequest:
    target: str
    project: Path
    plan: DeploymentPlan
    attempt_id: str
    environment: dict[str, str] = field(repr=False)
    access_mode: str = "loopback"
    sqlite_binding: dict | None = None
    postgresql_binding: bool = False
    remote_host: bool = False


class ExecutionAdapter(Protocol):
    def execution_capabilities(self) -> ExecutionCapabilities: ...

    def deploy(
        self, project: Path, plan: DeploymentPlan, attempt_id: str, environment: dict[str, str]
    ) -> dict: ...


@dataclass
class ExecutionState:
    adapter: ExecutionAdapter


def execute(request: ExecutionRequest, state: ExecutionState) -> dict:
    """Check declared support, run the adapter, then validate its owned result."""
    capabilities = state.adapter.execution_capabilities()
    if capabilities.target != request.target or request.plan.target != request.target:
        raise ValueError("배포 요청·설정·어댑터의 대상이 일치하지 않습니다.")
    if request.access_mode not in capabilities.access_modes:
        raise ValueError("선택한 배포 대상의 접근 범위를 지원하지 않습니다.")
    if request.sqlite_binding and not capabilities.sqlite_volume:
        raise ValueError("선택한 배포 대상은 SQLite 볼륨을 지원하지 않습니다.")
    if request.postgresql_binding and not capabilities.postgresql_binding:
        raise ValueError("선택한 배포 대상은 PostgreSQL 연결을 지원하지 않습니다.")
    if request.remote_host and not capabilities.remote_host:
        raise ValueError("선택한 배포 대상은 원격 호스트를 지원하지 않습니다.")
    result = state.adapter.deploy(request.project, request.plan, request.attempt_id, request.environment)
    if request.target in {"local-docker", "onprem-compose"}:
        expected = f"sky-{request.attempt_id}"
        url = result.get("url") if isinstance(result, dict) else None
        try:
            parsed = urlsplit(url) if isinstance(url, str) else None
            local_url = (
                parsed
                and parsed.scheme == "http"
                and parsed.hostname == "127.0.0.1"
                and parsed.port is not None
                and not parsed.username
                and not parsed.password
                and parsed.path == ""
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            local_url = False
        if (
            not local_url
            or result.get("container") != expected
            or result.get("image") != f"sky/{request.attempt_id}:latest"
        ):
            raise ValueError("로컬 배포 결과의 소유권 또는 루프백 주소가 올바르지 않습니다.")
        if request.target == "onprem-compose" and (
            result.get("compose_project") != expected
            or not isinstance(result.get("compose_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", result["compose_sha256"])
        ):
            raise ValueError("Compose 배포 결과의 소유권 또는 설정 해시가 올바르지 않습니다.")
        if request.sqlite_binding and (
            result.get("sqlite_volume") != request.sqlite_binding.get("volume_name")
            or result.get("sqlite_mount") != request.sqlite_binding.get("mount_path")
        ):
            raise ValueError("로컬 배포 결과의 SQLite 볼륨이 요청과 다릅니다.")
    return result
