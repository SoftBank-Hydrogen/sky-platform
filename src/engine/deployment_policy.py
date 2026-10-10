"""The user's deployment scope, separate from target suitability and credentials."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from engine.compatibility import TARGET_CAPABILITIES, deployment_access_mode

AUTO_TARGETS = ("local-docker", "cloud-run", "aws-ecs-express", "aws-s3-cloudfront")
EXPLICIT_TARGETS = frozenset(TARGET_CAPABILITIES) | {"aws-s3-cloudfront"}


def _access_mode(target: str, public_access: bool) -> str | None:
    if target == "aws-s3-cloudfront":
        return "public" if public_access else None
    return deployment_access_mode(target, public_access)


@dataclass(frozen=True)
class DeploymentPolicy:
    schema_version: int
    selection_mode: str
    allowed_targets: tuple[str, ...]
    public_access_allowed: bool
    allow_source_changes: bool
    new_managed_database_approved: bool
    allow_data_migration: bool
    preserve_databases: bool
    max_monthly_cost_usd: float | None = None
    public_url_required: bool = False

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version not in {1, 2}:
            raise ValueError("Unsupported deployment policy schema")
        if type(self.public_url_required) is not bool or (
            self.schema_version == 1 and self.public_url_required
        ):
            raise ValueError("Invalid public URL requirement")
        if self.public_url_required and not self.public_access_allowed:
            raise ValueError("공개 URL을 요구하려면 인터넷 공개를 허용해야 합니다.")
        if type(self.selection_mode) is not str or self.selection_mode not in {"fixed_target", "auto_target"}:
            raise ValueError("Invalid policy selection mode")
        if (
            not isinstance(self.allowed_targets, tuple)
            or not self.allowed_targets
            or any(type(target) is not str for target in self.allowed_targets)
            or len(set(self.allowed_targets)) != len(self.allowed_targets)
            or any(target not in EXPLICIT_TARGETS for target in self.allowed_targets)
            or (
                self.selection_mode == "auto_target"
                and any(target not in AUTO_TARGETS for target in self.allowed_targets)
            )
        ):
            raise ValueError("Invalid policy target scope")
        if self.max_monthly_cost_usd is not None and (
            type(self.max_monthly_cost_usd) not in {int, float}
            or not math.isfinite(self.max_monthly_cost_usd)
            or self.max_monthly_cost_usd < 0
        ):
            raise ValueError("Invalid cost limit")
        for field_name in (
            "public_access_allowed",
            "allow_source_changes",
            "new_managed_database_approved",
            "allow_data_migration",
            "preserve_databases",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise ValueError("Invalid deployment policy flag")

    def require(
        self,
        target: str,
        access_mode: str,
        *,
        new_managed_database: bool = False,
        data_migration: bool = False,
    ) -> None:
        if target not in self.allowed_targets:
            raise ValueError("선택한 배포 대상이 사용자 허용 범위 밖입니다.")
        expected = _access_mode(target, self.public_access_allowed)
        if expected is None or access_mode != expected:
            raise ValueError("배포 접근 범위가 사용자 선택과 다릅니다.")
        if self.public_url_required and access_mode != "public":
            raise ValueError("공개 URL이 필요한 앱을 비공개 대상으로 배포할 수 없습니다.")
        if new_managed_database and not self.new_managed_database_approved:
            raise ValueError("새 관리형 DB 생성 계획에 대한 사용자 확인이 필요합니다.")
        if data_migration and not self.allow_data_migration:
            raise ValueError("데이터 이전에 대한 사용자 선택이 필요합니다.")

    def as_dict(self) -> dict:
        result = asdict(self)
        if self.schema_version == 1:
            result.pop("public_url_required")
        return result


def deployment_policy(
    requested: str | tuple[str, ...],
    public_access_allowed: bool,
    *,
    new_managed_database_approved: bool = False,
    allow_data_migration: bool = False,
    public_url_required: bool = False,
) -> DeploymentPolicy:
    if type(public_access_allowed) is not bool:
        raise ValueError("Public access selection must be a boolean")
    if type(public_url_required) is not bool or (public_url_required and not public_access_allowed):
        raise ValueError("공개 URL을 요구하려면 인터넷 공개를 허용해야 합니다.")
    if requested == "auto":
        allowed_targets = tuple(
            target
            for target in AUTO_TARGETS
            if _access_mode(target, public_access_allowed) is not None
            and (not public_url_required or _access_mode(target, public_access_allowed) == "public")
        )
        selection_mode = "auto_target"
    elif isinstance(requested, str):
        allowed_targets = (requested,)
        selection_mode = "fixed_target"
    elif isinstance(requested, tuple):
        allowed_targets = requested
        selection_mode = "fixed_target"
    else:
        raise ValueError("Invalid deployment target selection")
    if public_url_required and any(
        _access_mode(target, public_access_allowed) != "public" for target in allowed_targets
    ):
        raise ValueError("선택한 배포 대상은 공개 URL을 제공하지 않습니다.")
    return DeploymentPolicy(
        schema_version=2 if public_url_required else 1,
        selection_mode=selection_mode,
        allowed_targets=allowed_targets,
        public_access_allowed=public_access_allowed,
        allow_source_changes=True,
        new_managed_database_approved=new_managed_database_approved,
        allow_data_migration=allow_data_migration,
        preserve_databases=True,
        public_url_required=public_url_required,
    )


def policy_from_record(record: dict) -> DeploymentPolicy:
    """Validate a persisted policy before replay; old records have no policy and stay on their legacy path."""
    if not isinstance(record, dict):
        raise TypeError("Invalid stored deployment policy")
    expected = set(DeploymentPolicy.__dataclass_fields__)
    if record.get("schema_version") == 1:
        expected.remove("public_url_required")
    if set(record) != expected:
        raise ValueError("Invalid stored deployment policy")
    targets = record["allowed_targets"]
    if not isinstance(targets, (list, tuple)):
        raise TypeError("Invalid stored deployment policy targets")
    return DeploymentPolicy(**{**record, "allowed_targets": tuple(targets)})
