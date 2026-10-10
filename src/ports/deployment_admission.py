"""Internal atomic intake port; verified authentication and approval precede intake."""

from dataclasses import dataclass
from typing import Protocol

from domain.access import Principal
from ports.artifacts import SourceArtifact


@dataclass(frozen=True)
class AdmittedDeployment:
    job_id: str
    operation_id: str


class DeploymentAdmission(Protocol):
    def admit(
        self,
        principal: Principal,
        artifact: SourceArtifact,
        request_key: str,
        approved_plan: dict,
        *,
        expected_plan_digest: str,
        expected_source_digest: str,
    ) -> AdmittedDeployment: ...
