"""Automatic execution settings remain declarations, not uploaded instructions."""

import pytest

from application.deployment_preparation import _automatic_port


@pytest.mark.parametrize("declaration", ["EXPOSE 8080", "expose 8080/tcp # HTTP"])
def test_literal_docker_http_port(tmp_path, declaration):
    (tmp_path / "Dockerfile").write_text("FROM node:22\n" + declaration + "\n")
    assert _automatic_port(tmp_path) == 8080


@pytest.mark.parametrize(
    "declaration", ["EXPOSE $PORT", "EXPOSE 8080 9090", "EXPOSE 8080/udp", "EXPOSE 99999", "# EXPOSE 8080"]
)
def test_ambiguous_or_invalid_docker_ports_are_not_guessed(tmp_path, declaration):
    (tmp_path / "Dockerfile").write_text("FROM node:22\n" + declaration + "\n")
    with pytest.raises(ValueError):
        _automatic_port(tmp_path)


def test_generated_launcher_port_without_custom_dockerfile(tmp_path):
    assert _automatic_port(tmp_path) == 3000
