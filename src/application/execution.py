"""Small execution boundary for adapters still using the legacy deployment call."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from application.deployment_core import DeploymentPlan
from application.source_transform import executable_plan_digest, resolved_target_plan
from engine.compatibility import TARGET_CAPABILITIES


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
    postgres_request: object | None = field(default=None, repr=False)
    migrations: object | None = field(default=None, repr=False)
    compiled_target: dict | None = None
    new_managed_database: bool = False
    source_transform: dict | None = None


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
    if request.postgresql_binding != (request.postgres_request is not None):
        raise ValueError("PostgreSQL 연결 요구와 실행 입력이 일치하지 않습니다.")
    if request.migrations is not None and request.postgres_request is None:
        raise ValueError("SQL 마이그레이션에는 PostgreSQL 연결 요청이 필요합니다.")
    if request.new_managed_database and not request.postgresql_binding:
        raise ValueError("신규 DB 생성은 PostgreSQL 연결 요청이 필요합니다.")
    if request.compiled_target is not None:
        config = request.compiled_target.get("execution_configuration")
        if request.postgresql_binding:
            database_mode = "create_rds" if request.new_managed_database else "existing_rds"
        elif request.sqlite_binding:
            database_mode = "sqlite_volume"
        else:
            database_mode = "none"
        expected = {
            "service": "source-bundle",
            "replicas": 1,
            "access_mode": request.access_mode,
            "database_mode": database_mode,
            "required_image_platform": TARGET_CAPABILITIES[request.target]["image_platform"],
            "port_source": "executable_deployment_plan",
        }
        if (
            request.compiled_target.get("target") != request.target
            or config != expected
            or type(request.plan.port) is not int
            or not 1 <= request.plan.port <= 65535
        ):
            raise ValueError("CV-09: Compiled target and execution request disagree")
    if request.source_transform is not None:
        record = request.source_transform
        target_plan = request.compiled_target
        if (
            target_plan is None
            or record.get("schema_version") != 2
            or record.get("compilation_id") != target_plan.get("compilation_id")
            or record.get("target_plan_id") != target_plan.get("id")
            or record.get("transformed_source_revision") != request.plan.source_digest
            or record.get("executable_plan_digest") != executable_plan_digest(request.plan)
            or record.get("resolved_target") != resolved_target_plan(target_plan, request.plan)
        ):
            raise ValueError("CV-06: Resolved target and execution request disagree")
    if request.target == "cloud-run" and state.adapter.public is not (request.access_mode == "public"):
        raise ValueError("Cloud Run 어댑터의 공개 범위가 실행 요청과 다릅니다.")
    if request.target == "aws-ecs-express":
        result = state.adapter.deploy(
            request.project, request.plan, request.attempt_id, request.environment,
            postgres=request.postgres_request, migrations=request.migrations,
        )
        if not isinstance(result, dict) or result.get("target") != request.target:
            raise ValueError("AWS 배포 결과의 대상이 요청과 다릅니다.")
    else:
        result = state.adapter.deploy(request.project, request.plan, request.attempt_id, request.environment)
    if request.target == "cloud-run" and (
        not isinstance(result, dict)
        or result.get("target") != request.target
        or result.get("public") is not (request.access_mode == "public")
    ):
        raise ValueError("Cloud Run 배포 결과가 요청한 대상·접근 범위와 다릅니다.")
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
    if request.target == "onprem-vm":
        expected = f"sky-{request.attempt_id}"
        url = result.get("url") if isinstance(result, dict) else None
        try:
            parsed = urlsplit(url) if isinstance(url, str) else None
            valid_url = (parsed and parsed.scheme == "http" and parsed.hostname
                         and parsed.hostname == result.get("vm_public_host")
                         and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
                         and parsed.port is not None and not parsed.username and not parsed.password
                         and not parsed.path and not parsed.query and not parsed.fragment)
        except ValueError:
            valid_url = False
        if (not valid_url or result.get("container") != expected
                or result.get("image") != f"sky/{request.attempt_id}:latest"
                or result.get("compose_project") != expected
                or not re.fullmatch(r"[a-f0-9]{64}", result.get("compose_sha256", ""))):
            raise ValueError("원격 VM 배포 결과의 소유권 또는 주소가 올바르지 않습니다.")
    return result
