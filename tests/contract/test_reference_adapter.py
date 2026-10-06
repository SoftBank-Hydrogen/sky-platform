"""메모리 안에서만 동작하는 참조 어댑터로 계약 테스트 자체를 검증한다.

실제 어댑터가 생기면 같은 방식으로 tests/contract/test_<target>.py를 추가한다.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from contract import (
    AppRequirements,
    Blocker,
    DataRequirement,
    DeployRequest,
    DeployResult,
    ExecutionMode,
    Exposure,
    Preflight,
    TargetCapabilities,
    Verification,
    VerificationStatus,
)

from .suite import AdapterContract


class ReferenceAdapter:
    name = "reference"

    def __init__(self) -> None:
        self.resources: dict[str, tuple[str, ...]] = {}
        self.digests: dict[str, str] = {}
        self.releases: dict[str, list[str]] = {}

    def capabilities(self) -> TargetCapabilities:
        return TargetCapabilities(
            target=self.name,
            modes=frozenset({ExecutionMode.HTTP_SERVER}),
            data=frozenset({DataRequirement.NONE}),
            exposures=frozenset({Exposure.LOOPBACK}),
            supports_rollback=True,
        )

    def preflight(self, requirements: AppRequirements) -> Preflight:
        caps = self.capabilities()
        blockers = []
        if requirements.mode not in caps.modes:
            blockers.append(Blocker("mode", f"{requirements.mode.value} 실행 방식을 지원하지 않는다"))
        if requirements.data not in caps.data:
            blockers.append(Blocker("data", f"{requirements.data.value} 데이터 요구를 지원하지 않는다"))
        if requirements.platform not in caps.platforms:
            blockers.append(Blocker("platform", f"{requirements.platform} 플랫폼을 지원하지 않는다"))
        return Preflight(target=self.name, blockers=tuple(blockers))

    def _result(self, deployment_id: str, release_id: str) -> DeployResult:
        return DeployResult(
            target=self.name,
            deployment_id=deployment_id,
            url=f"http://127.0.0.1/{deployment_id}",
            release_id=release_id,
            owned_resources=self.resources[deployment_id],
            verification=Verification(
                http=VerificationStatus.VERIFIED,
                http_status=200,
                running_digest=self.digests[deployment_id],
                digest_match=VerificationStatus.VERIFIED,
            ),
        )

    def deploy(self, request: DeployRequest) -> DeployResult:
        if not self.preflight(request.requirements).ok:
            raise ValueError("preflight blocked")
        did = request.deployment_id
        if did in self.resources:
            if self.digests[did] != request.artifact.digest:
                raise ValueError("deployment_id already belongs to another artifact")
            return self._result(did, self.releases[did][-1])
        self.resources.setdefault(did, (f"container/{did}",))
        self.digests[did] = request.artifact.digest
        releases = self.releases.setdefault(did, [])
        release_id = f"{did}-r{len(releases) + 1}"
        releases.append(release_id)
        return self._result(did, release_id)

    def verify(self, result: DeployResult) -> DeployResult:
        return self._result(result.deployment_id, result.release_id)

    def rollback(self, deployment_id: str, to_release_id: str) -> DeployResult:
        if to_release_id not in self.releases.get(deployment_id, []):
            raise KeyError(to_release_id)
        return self._result(deployment_id, to_release_id)

    def destroy(self, deployment_id: str) -> tuple[str, ...]:
        self.digests.pop(deployment_id, None)
        self.releases.pop(deployment_id, None)
        return self.resources.pop(deployment_id, ())


class TestReferenceAdapter(AdapterContract):
    @pytest.fixture
    def adapter(self):
        return ReferenceAdapter()

    @pytest.fixture
    def owned_resources(self, adapter):
        def lookup(deployment_id):
            if deployment_id is None:
                return tuple(sorted(r for rs in adapter.resources.values() for r in rs))
            return adapter.resources.get(deployment_id, ())

        return lookup


def test_unverified_digest_is_not_success():
    v = Verification(
        http=VerificationStatus.VERIFIED,
        http_status=200,
        running_digest=None,
        digest_match=VerificationStatus.UNVERIFIED,
    )
    assert not v.succeeded
    assert replace(v, digest_match=VerificationStatus.VERIFIED).succeeded
