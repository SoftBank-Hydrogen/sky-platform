"""Stored approval decisions, bound to the verified approver and execution scope."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from domain.access import Principal
from ports.artifacts import SourceArtifact
from ports.deployment_admission import AdmittedDeployment


class ApprovalUnavailable(ValueError):
    """An approval expired, was revoked, or cannot authorize another admission."""


@dataclass(frozen=True)
class DeploymentApproval:
    id: str
    expires_at: datetime


class DeploymentApprovals(Protocol):
    def approve(
        self, principal: Principal, artifact: SourceArtifact, validated_plan: dict, *, seconds: int = 900
    ) -> DeploymentApproval: ...

    def revoke(self, principal: Principal, approval_id: str) -> bool: ...

    def submit(self, principal: Principal, approval_id: str, request_key: str) -> AdmittedDeployment: ...
