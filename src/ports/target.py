"""대상 어댑터 인터페이스. Local Docker, AWS, GCP, 원격 서버가 모두 이것을 구현한다."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domain.models import AppRequirements, DeployRequest, DeployResult, Preflight, TargetCapabilities


@runtime_checkable
class TargetAdapter(Protocol):
    name: str

    def capabilities(self) -> TargetCapabilities:
        """정적 선언. 네트워크 호출 없이 돌아와야 한다."""
        ...

    def preflight(self, requirements: AppRequirements) -> Preflight:
        """리소스를 만들지 않고 가능 여부·계획·비용을 판정한다."""
        ...

    def deploy(self, request: DeployRequest) -> DeployResult:
        """같은 deployment_id로 다시 불러도 리소스가 중복 생성되지 않아야 한다(멱등)."""
        ...

    def verify(self, result: DeployResult) -> DeployResult:
        """실제 URL 응답과 실행 중 산출물 다이제스트를 다시 확인한다."""
        ...

    def rollback(self, deployment_id: str, to_release_id: str) -> DeployResult:
        """capabilities().supports_rollback이 False면 NotImplementedError."""
        ...

    def destroy(self, deployment_id: str) -> tuple[str, ...]:
        """소유 표식이 확인된 리소스만 지우고, 지운 목록을 돌려준다. 데이터 저장소는 지우지 않는다."""
        ...
