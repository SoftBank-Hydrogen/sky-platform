import pytest

from sky_platform.contract import (
    AppRequirements,
    Artifact,
    ArtifactKind,
    DataRequirement,
    ExecutionMode,
)


def test_http_server_requires_port():
    with pytest.raises(ValueError):
        AppRequirements(mode=ExecutionMode.HTTP_SERVER, data=DataRequirement.NONE)


def test_static_site_needs_no_port():
    AppRequirements(mode=ExecutionMode.STATIC_SITE, data=DataRequirement.NONE)


def test_secret_env_must_be_declared_as_required():
    with pytest.raises(ValueError):
        AppRequirements(
            mode=ExecutionMode.HTTP_SERVER,
            data=DataRequirement.NONE,
            port=3000,
            secret_env=("API_KEY",),
        )


@pytest.mark.parametrize("digest", ["latest", "sha256:abc", "sha256:" + "G" * 64])
def test_artifact_rejects_non_digest(digest):
    with pytest.raises(ValueError):
        Artifact(kind=ArtifactKind.CONTAINER_IMAGE, digest=digest)
