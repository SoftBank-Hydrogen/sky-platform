"""모든 대상 어댑터가 통과해야 하는 공통 테스트.

새 어댑터는 이 클래스를 상속하고 `adapter`, `owned_resources` 픽스처만 구현한다.
"선택한 각 환경에서 같은 기준으로 배포를 증명한다"는 주장을 코드로 고정하는 장치다.
"""

from __future__ import annotations

import uuid

import pytest

from sky_platform.contract import (
    AppRequirements,
    Artifact,
    ArtifactKind,
    DataRequirement,
    DeployRequest,
    ExecutionMode,
    TargetAdapter,
    VerificationStatus,
)

DIGEST = "sha256:" + "a" * 64


def http_app(**overrides) -> AppRequirements:
    values = {"mode": ExecutionMode.HTTP_SERVER, "data": DataRequirement.NONE, "port": 8080}
    values.update(overrides)
    return AppRequirements(**values)


def request_for(requirements: AppRequirements, deployment_id: str | None = None) -> DeployRequest:
    return DeployRequest(
        deployment_id=deployment_id or f"contract-{uuid.uuid4().hex[:8]}",
        requirements=requirements,
        artifact=Artifact(kind=ArtifactKind.CONTAINER_IMAGE, digest=DIGEST),
    )


class AdapterContract:
    @pytest.fixture
    def adapter(self) -> TargetAdapter:
        raise NotImplementedError

    @pytest.fixture
    def owned_resources(self, adapter):
        """deployment_id → 대상에 실제로 남아 있는 소유 리소스 목록을 돌려주는 함수."""
        raise NotImplementedError

    def test_implements_protocol(self, adapter):
        assert isinstance(adapter, TargetAdapter)
        assert adapter.capabilities().target == adapter.name

    def test_preflight_blocks_unsupported_data_without_side_effects(self, adapter, owned_resources):
        caps = adapter.capabilities()
        unsupported = next((d for d in DataRequirement if d not in caps.data), None)
        if unsupported is None:
            pytest.skip("모든 데이터 요구를 지원한다")
        before = owned_resources(None)
        result = adapter.preflight(http_app(data=unsupported))
        assert not result.ok
        assert all(b.message for b in result.blockers), "차단에는 이유가 있어야 한다"
        assert owned_resources(None) == before

    def test_preflight_blocks_unsupported_platform(self, adapter):
        result = adapter.preflight(http_app(platform="linux/s390x"))
        if "linux/s390x" in adapter.capabilities().platforms:
            pytest.skip("해당 플랫폼을 지원한다")
        assert not result.ok

    def test_deploy_verifies_http_and_same_digest(self, adapter):
        request = request_for(http_app())
        result = adapter.deploy(request)
        try:
            assert result.verification.succeeded
            assert result.verification.running_digest == request.artifact.digest
            assert result.url
        finally:
            adapter.destroy(request.deployment_id)

    def test_deploy_is_idempotent(self, adapter, owned_resources):
        request = request_for(http_app())
        try:
            first = adapter.deploy(request)
            second = adapter.deploy(request)
            assert first.release_id == second.release_id
            assert set(first.owned_resources) == set(second.owned_resources)
            assert set(owned_resources(request.deployment_id)) == set(first.owned_resources)
        finally:
            adapter.destroy(request.deployment_id)

    def test_destroy_removes_only_owned_and_is_repeatable(self, adapter, owned_resources):
        request = request_for(http_app())
        result = adapter.deploy(request)
        removed = adapter.destroy(request.deployment_id)
        assert set(removed) == set(result.owned_resources)
        assert owned_resources(request.deployment_id) == ()
        assert adapter.destroy(request.deployment_id) == ()

    def test_unverified_is_never_success(self, adapter):
        request = request_for(http_app())
        result = adapter.deploy(request)
        try:
            v = result.verification
            if VerificationStatus.UNVERIFIED in (v.http, v.digest_match):
                assert not v.succeeded
        finally:
            adapter.destroy(request.deployment_id)

    def test_rollback_matches_capability(self, adapter):
        request = request_for(http_app())
        first = adapter.deploy(request)
        try:
            if not adapter.capabilities().supports_rollback:
                with pytest.raises(NotImplementedError):
                    adapter.rollback(request.deployment_id, first.release_id)
            else:
                rolled = adapter.rollback(request.deployment_id, first.release_id)
                assert rolled.release_id == first.release_id
                assert rolled.verification.succeeded
        finally:
            adapter.destroy(request.deployment_id)
