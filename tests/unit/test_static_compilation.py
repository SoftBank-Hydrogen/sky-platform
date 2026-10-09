"""A static bundle is compiled as static assets, never as a container."""

from __future__ import annotations

import copy
import tempfile
from pathlib import Path

import pytest

from application.deployment_core import source_digest
from application.static_compilation import static_compilation, verify_static_compilation
from engine.architecture_decision import verify_architecture_decision
from engine.compilation import verify_compilation
from engine.deployment_policy import deployment_policy, policy_from_record
from engine.target_lowering import lower_target_configuration


def test_static_compiler_binds_source_policy_and_backend():
    with tempfile.TemporaryDirectory() as directory:
        project = Path(directory)
        (project / "index.html").write_text("<h1>Sky</h1>")
        digest = source_digest(project)
        records = static_compilation(project, digest)
        decision = records["architecture_decision"]
        compilation = records["compilation"]
        assert decision["selected_candidate"] == "aws-s3-cloudfront"
        assert decision["source_revision"] == digest
        assert records["application_ir"]["components"][0]["kind"] == "static_site"
        assert compilation["source_patch_plan"]["status"] == "not_required"
        assert compilation["deployment_ir"]["services"] == [{"id": "source-bundle", "kind": "static_site"}]
        assert compilation["target_plan"]["execution_configuration"]["asset_source"] == "uploaded_source"
        policy = policy_from_record(records["deployment_policy"])
        verify_architecture_decision(
            decision, records["application_ir"], policy, records["infrastructure_plan"]
        )
        verify_compilation(
            compilation, decision, records["application_ir"], policy, records["infrastructure_plan"]
        )
        assert verify_static_compilation(records, project, digest)
        assert "aws-s3-cloudfront" not in deployment_policy("auto", True).allowed_targets
        with pytest.raises(ValueError, match="접근 범위"):
            deployment_policy("aws-s3-cloudfront", False).require("aws-s3-cloudfront", "public")


def test_static_compiler_rejects_mixed_source_and_changed_execution_plan():
    with tempfile.TemporaryDirectory() as directory:
        project = Path(directory)
        (project / "index.html").write_text("<h1>Sky</h1>")
        digest = source_digest(project)
        records = static_compilation(project, digest)
        altered = copy.deepcopy(records)
        altered["compilation"]["target_plan"]["execution_configuration"]["asset_source"] = "other"
        with pytest.raises(ValueError, match="Stored static architecture"):
            verify_static_compilation(altered, project, digest)
        container_ir = copy.deepcopy(records["compilation"]["deployment_ir"])
        container_ir["services"] = [{"id": "source-bundle", "kind": "container_service", "replicas": 1}]
        with pytest.raises(ValueError, match="Static plan"):
            lower_target_configuration(records["infrastructure_plan"], container_ir)
        wrong_resources = copy.deepcopy(records["infrastructure_plan"])
        wrong_resources["resources"] = ["ECR repository", "ECS Express service"]
        with pytest.raises(ValueError, match="Static plan"):
            lower_target_configuration(wrong_resources, records["compilation"]["deployment_ir"])
        assert (
            verify_static_compilation(
                {"infrastructure_plan": {"target": "aws-s3-cloudfront"}}, project, digest
            )
            is False
        )
        with pytest.raises(ValueError, match="Incomplete static architecture"):
            verify_static_compilation({"compilation": records["compilation"]}, project, digest)
        (project / "server.js").write_text("require('node:http')")
        with pytest.raises(ValueError, match="eligible source"):
            static_compilation(project, source_digest(project))
