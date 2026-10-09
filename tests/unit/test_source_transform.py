"""Applied source changes must match the image plan sent to an adapter."""

import copy
import json
import shutil

import pytest

from application.deployment_core import make_plan, source_digest
from application.source_transform import source_transform_record, verify_source_transform


def test_applied_changes_are_source_and_target_bound(tmp_path):
    original = tmp_path / "source"
    original.mkdir()
    (original / "package.json").write_text(json.dumps({"scripts": {"start": "node server.js"}}))
    (original / "server.js").write_text("console.log('before')")
    work = tmp_path / "work"
    shutil.copytree(original, work)
    (work / "server.js").write_text("console.log('after')")
    plan = make_plan(work, "start", None, 3000, target="local-docker")
    compilation = {
        "compilation_id": "comp-example",
        "decision_revision": 1,
        "source_revision": source_digest(original),
        "target_plan": {"id": "target-example", "target": "local-docker"},
    }
    record = source_transform_record(compilation, original, work, plan)
    assert record["transformed_source_revision"] == plan.source_digest
    assert [item["path"] for item in record["changes"]] == ["server.js"]
    assert record["changes"][0]["before_sha256"] != record["changes"][0]["after_sha256"]
    verify_source_transform(record, compilation, original, work, plan)

    mixed = copy.deepcopy(record)
    mixed["compilation_id"] = "comp-other"
    with pytest.raises(ValueError, match="Stored source transformation"):
        verify_source_transform(mixed, compilation, original, work, plan)
    (work / "server.js").write_text("console.log('changed again')")
    with pytest.raises(ValueError, match="Executable plan"):
        verify_source_transform(record, compilation, original, work, plan)


def test_compiled_target_and_generated_port_must_match(tmp_path):
    original = tmp_path / "source"
    original.mkdir()
    (original / "package.json").write_text(json.dumps({"scripts": {"start": "node server.js"}}))
    (original / "server.js").write_text("console.log('ok')")
    plan = make_plan(original, "start", None, 3000, target="local-docker")
    compilation = {
        "compilation_id": "comp-example",
        "decision_revision": 1,
        "source_revision": source_digest(original),
        "target_plan": {"id": "target-example", "target": "aws-ecs-express"},
    }
    with pytest.raises(ValueError, match="target differs"):
        source_transform_record(compilation, original, original, plan)
    compilation["target_plan"]["target"] = "local-docker"
    plan.dockerfile = plan.dockerfile.replace("EXPOSE 3000", "EXPOSE 8080")
    with pytest.raises(ValueError, match="image port"):
        source_transform_record(compilation, original, original, plan)


def test_python_generated_image_can_set_port_alongside_other_environment(tmp_path):
    original = tmp_path / "source"
    original.mkdir()
    (original / "app.py").write_text("from flask import Flask\napp = Flask(__name__)\n")
    (original / "requirements.txt").write_text("flask==3.1.1\ngunicorn==23.0.0\n")
    plan = make_plan(original, "wsgi:app.py", None, 3000, target="local-docker")
    compilation = {
        "compilation_id": "comp-example",
        "decision_revision": 1,
        "source_revision": source_digest(original),
        "target_plan": {"id": "target-example", "target": "local-docker"},
    }
    assert source_transform_record(compilation, original, original, plan)["changes"] == []


def test_versioned_transform_resolves_http_endpoint_and_preserves_old_records(tmp_path):
    original = tmp_path / "source"
    original.mkdir()
    (original / "package.json").write_text(json.dumps({"scripts": {"start": "node server.js"}}))
    (original / "server.js").write_text("console.log('ok')")
    plan = make_plan(original, "start", None, 3000, "/health", target="local-docker")
    compilation = {
        "schema_version": 2,
        "compilation_id": "comp-example",
        "decision_revision": 1,
        "source_revision": source_digest(original),
        "target_plan": {
            "id": "target-example",
            "target": "local-docker",
            "execution_configuration": {
                "service": "source-bundle",
                "port_source": "executable_deployment_plan",
            },
        },
    }
    record = source_transform_record(compilation, original, original, plan)
    assert record["schema_version"] == 2
    assert record["resolved_target"] == {
        "target_plan_id": "target-example",
        "service": "source-bundle",
        "container_protocol": "http",
        "container_port": 3000,
        "health_path": "/health",
    }
    verify_source_transform(record, compilation, original, original, plan)
    changed = copy.deepcopy(record)
    changed["resolved_target"]["container_port"] = 8080
    with pytest.raises(ValueError, match="Stored source transformation"):
        verify_source_transform(changed, compilation, original, original, plan)
    old_record = source_transform_record(compilation, original, original, plan, legacy=True)
    verify_source_transform(old_record, compilation, original, original, plan)
