"""Resolve the IAM permissions boundary supplied by the Sky service environment."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping


def managed_role_boundary(account: str, environment: Mapping[str, str] | None = None) -> str:
    """Require the boundary in the shared service; allow an unbound local A-phase."""
    values = os.environ if environment is None else environment
    stage = values.get("SKY_ENVIRONMENT", "")
    boundary = values.get("SKY_AWS_ROLE_BOUNDARY_ARN", "")
    if not boundary:
        if stage:
            raise ValueError("Sky 서비스의 앱 배포에는 SKY_AWS_ROLE_BOUNDARY_ARN이 필요합니다.")
        return ""
    if stage and re.fullmatch(r"[a-z][a-z0-9-]{0,31}", stage) is None:
        raise ValueError("Sky 서비스 환경 이름이 올바르지 않습니다.")
    name = f"sky-{stage}-deployed-app-boundary" if stage else r"sky-[A-Za-z0-9-]+"
    expected = rf"arn:aws:iam::{re.escape(account)}:policy/{name}"
    if re.fullmatch(expected, boundary) is None:
        raise ValueError("앱 역할 권한 경계가 대상 AWS 계정의 Sky 정책과 다릅니다.")
    return boundary
