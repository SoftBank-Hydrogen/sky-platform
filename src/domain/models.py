"""모든 대상 어댑터가 주고받는 값. 대상별 세부는 여기에 넣지 않는다."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class ExecutionMode(str, Enum):
    STATIC_SITE = "static_site"
    HTTP_SERVER = "http_server"


class DataRequirement(str, Enum):
    NONE = "none"
    POSTGRESQL = "postgresql"
    SQLITE = "sqlite"  # 전환 경로를 거쳐 PostgreSQL로 배포한다
    UNSUPPORTED = "unsupported"


class Exposure(str, Enum):
    LOOPBACK = "loopback"
    AUTHENTICATED = "authenticated"
    PUBLIC = "public"


class ArtifactKind(str, Enum):
    CONTAINER_IMAGE = "container_image"
    STATIC_BUNDLE = "static_bundle"


class VerificationStatus(str, Enum):
    VERIFIED = "verified"
    FAILED = "failed"
    UNVERIFIED = "unverified"  # 확인하지 않은 것은 성공으로 세지 않는다


@dataclass(frozen=True)
class Evidence:
    """판단의 근거. 소스 위치를 가리킨다."""

    path: str
    line: int | None = None
    excerpt: str = ""


@dataclass(frozen=True)
class AppRequirements:
    """앱 분석 결과. 어떤 대상에 배포할지와 무관하다."""

    mode: ExecutionMode
    data: DataRequirement
    port: int | None = None
    required_env: tuple[str, ...] = ()
    secret_env: tuple[str, ...] = ()
    platform: str = "linux/amd64"
    evidence: tuple[Evidence, ...] = ()

    def __post_init__(self) -> None:
        if self.mode is ExecutionMode.HTTP_SERVER and self.port is None:
            raise ValueError("HTTP 서버는 포트가 필요하다")
        overlap = set(self.secret_env) - set(self.required_env)
        if overlap:
            raise ValueError(f"secret_env는 required_env의 부분집합이어야 한다: {sorted(overlap)}")


@dataclass(frozen=True)
class Artifact:
    """한 번 만들어 모든 대상에 그대로 옮기는 산출물."""

    kind: ArtifactKind
    digest: str
    platform: str = "linux/amd64"

    def __post_init__(self) -> None:
        if not _DIGEST.fullmatch(self.digest):
            raise ValueError(f"다이제스트 형식이 아니다: {self.digest!r}")


@dataclass(frozen=True)
class TargetCapabilities:
    """대상이 지원하는 것. 사전 검사는 이 선언만 보고 판정한다."""

    target: str
    modes: frozenset[ExecutionMode]
    data: frozenset[DataRequirement]
    exposures: frozenset[Exposure]
    platforms: frozenset[str] = frozenset({"linux/amd64"})
    supports_rollback: bool = False
    supports_restart_drill: bool = False


@dataclass(frozen=True)
class Blocker:
    code: str
    message: str
    evidence: tuple[Evidence, ...] = ()


@dataclass(frozen=True)
class Preflight:
    """리소스를 만들기 전 판정. blockers가 있으면 deploy를 호출하지 않는다."""

    target: str
    blockers: tuple[Blocker, ...] = ()
    planned_resources: tuple[str, ...] = ()
    monthly_cost_estimate_usd: float | None = None
    cost_notes: str = ""

    @property
    def ok(self) -> bool:
        return not self.blockers


@dataclass(frozen=True)
class SecretRef:
    """비밀값 자체가 아니라 비밀 저장소의 참조. 값은 계약 안에서 다루지 않는다."""

    env_name: str
    ref: str


@dataclass(frozen=True)
class DeployRequest:
    deployment_id: str
    requirements: AppRequirements
    artifact: Artifact
    env: dict[str, str] = field(default_factory=dict)
    secrets: tuple[SecretRef, ...] = ()
    exposure: Exposure = Exposure.PUBLIC


@dataclass(frozen=True)
class Verification:
    """배포 성공은 이 값으로만 판정한다. AI나 어댑터의 주장으로 판정하지 않는다."""

    http: VerificationStatus
    http_status: int | None
    running_digest: str | None
    digest_match: VerificationStatus
    notes: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.http is VerificationStatus.VERIFIED and self.digest_match is VerificationStatus.VERIFIED


@dataclass(frozen=True)
class DeployResult:
    target: str
    deployment_id: str
    url: str | None
    release_id: str
    owned_resources: tuple[str, ...]
    verification: Verification
