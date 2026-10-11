"""AWS architecture fit is independent of Sky execution and missing runtime proof."""

import json

import pytest

from application.analysis import AnalysisError
from application.certificate import deployment_certificate
from application.infrastructure import inspect_infrastructure, validate_infrastructure_proposal
from engine.application_ir import application_ir
from engine.architecture_decision import architecture_decision
from engine.aws_backend_assessment import with_aws_backend_options
from engine.backend_identity import backend_identity
from engine.candidates import compare_targets
from engine.capability_registry import target_capability_model
from engine.compatibility import infrastructure_compatibility
from engine.deployment_policy import deployment_policy
from engine.runtime_signals import runtime_signals
from engine.static_site import assess_static_site
from engine.target_selection_context import automatic_target_context


def inspect(tmp_path, files):
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    return inspect_infrastructure(tmp_path)


def compare(profile):
    return compare_targets(
        profile,
        {"local-docker": None, "aws-ecs-express": None, "cloud-run": None},
        public_access=True,
    )


def by_id(profile):
    return {item["id"]: item for item in compare(profile)[1]}


def test_stateless_http_server_keeps_ecs_and_requires_lambda_transformation(tmp_path):
    profile = inspect(tmp_path, {"app.py": "import uvicorn\nuvicorn.run(app, port=8080)\n"})
    candidates = by_id(profile)
    assert candidates["aws-ecs-express"]["status"] == "eligible"
    assert candidates["aws-lambda"]["structural_status"] == "requires_transformation"
    assert candidates["aws-ec2"]["structural_status"] == "not_preferred"
    assert "LAMBDA-ENTRYPOINT-01" in candidates["aws-lambda"]["unknown_rule_ids"]
    assert candidates["aws-lambda"]["evidence_files"] == ["app.py"]
    ir = application_ir(profile, "a" * 64).as_dict()
    evidence = {item["id"] for item in ir["evidence"]}
    assert all(set(item["evidence_ids"]) <= evidence for item in candidates.values())


def test_function_handler_is_only_potentially_suitable_and_not_a_running_http_server(tmp_path):
    profile = inspect(
        tmp_path,
        {
            "handler.py": (
                "def lambda_handler(event, context):\n    return {'statusCode': 200, 'body': 'hello'}\n"
            )
        },
    )
    candidates = by_id(profile)
    function = candidates["aws-lambda"]
    assert function["structural_status"] == "potentially_compatible"
    assert function["status"] == function["execution_status"] == "unsupported_by_sky"
    assert {"LAMBDA-DURATION-01", "LAMBDA-STATE-01", "LAMBDA-EVENT-01"} <= set(function["unknown_rule_ids"])
    assert all(item["status"] != "eligible" for item in candidates.values())
    assert all(not item["selected"] for item in candidates.values())
    assert assess_static_site(tmp_path, profile).status != "eligible"


def test_host_control_selects_an_ec2_review_without_enabling_privileged_deployment(tmp_path):
    profile = inspect(
        tmp_path,
        {
            "app.py": (
                "import uvicorn\nimport subprocess as sp\n"
                "sp.run(['/sbin/modprobe', 'kvm'])\nuvicorn.run(app)\n"
            )
        },
    )
    reports, candidates = compare(profile)
    assert all(not item["compatible"] for item in reports)
    assert all("HOST-CONTROL-01" in item["violated_rule_ids"] for item in candidates[:3])
    assert all(not item["preview_eligible"] for item in reports)
    assert by_id(profile)["aws-ec2"]["structural_status"] == "potentially_compatible"
    assert by_id(profile)["aws-lambda"]["structural_status"] == "incompatible"
    assert "host-kernel-control" in {item.kind for item in application_ir(profile, "b" * 64).hypotheses}


@pytest.mark.parametrize(
    "source",
    [
        "const {WebSocketServer}=require('ws'); const rooms = new Map(); new WebSocketServer({port:80});",
        "import subprocess\nsubprocess.run(['insmod', 'driver.ko'])",
    ],
)
def test_persistent_connections_and_host_control_are_not_request_handlers(tmp_path, source):
    name = "server.js" if "WebSocketServer" in source else "app.py"
    profile = inspect(tmp_path, {name: source})
    assert by_id(profile)["aws-lambda"]["structural_status"] == "incompatible"


def test_worker_is_not_automatically_an_ec2_or_lambda_workload(tmp_path):
    profile = inspect(tmp_path, {"package.json": json.dumps({"dependencies": {"bullmq": "1"}})})
    candidates = by_id(profile)
    assert candidates["aws-lambda"]["structural_status"] == "incompatible"
    assert candidates["aws-ec2"]["structural_status"] == "not_preferred"
    assert candidates["aws-ecs-express"]["status"] == "rejected"


def test_external_postgres_does_not_disqualify_lambda_or_require_ec2(tmp_path):
    profile = inspect(
        tmp_path,
        {
            "requirements.txt": "psycopg[binary]>=3\n",
            "handler.py": "import psycopg\ndef handler(event, context):\n    return {'body': 'ok'}\n",
        },
    )
    candidates = by_id(profile)
    assert candidates["aws-lambda"]["structural_status"] == "potentially_compatible"
    assert "LAMBDA-DATABASE-01" in candidates["aws-lambda"]["unknown_rule_ids"]
    assert candidates["aws-ec2"]["structural_status"] == "not_preferred"
    assert candidates["aws-ecs-express"]["status"] == "requires_database_binding"
    capabilities = {item.id for item in target_capability_model("aws-ecs-express").capabilities}
    assert {"new_rds_provisioning", "existing_rds_binding"} <= capabilities


def test_sqlite_remains_a_transformation_requirement(tmp_path):
    profile = inspect(
        tmp_path,
        {
            "handler.py": (
                "import sqlite3\ndef handler(event, context):\n"
                "    return sqlite3.connect('app.db').execute('select 1').fetchone()\n"
            )
        },
    )
    function = by_id(profile)["aws-lambda"]
    assert function["structural_status"] == "requires_transformation"
    assert "LAMBDA-DATA-01" in function["unknown_rule_ids"]


def test_missing_evidence_cannot_prove_lambda_suitability(tmp_path):
    profile = inspect(tmp_path, {"index.html": "<h1>hello</h1>"})
    function = by_id(profile)["aws-lambda"]
    assert function["structural_status"] == "unknown"
    assert "LAMBDA-HANDLER-01" in function["unknown_rule_ids"]
    assert function["evidence_ids"] == []
    assert assess_static_site(tmp_path, profile).status == "eligible"


@pytest.mark.parametrize("target", ["aws-lambda", "aws-ec2"])
def test_unimplemented_backends_are_never_deployment_targets(target, tmp_path):
    assert backend_identity(target).selection_mode == "unavailable"
    assert all(
        item.display_status == "unsupported_by_sky" for item in target_capability_model(target).capabilities
    )
    assert target not in deployment_policy("auto", True).allowed_targets
    with pytest.raises(ValueError, match="Unsupported automatic"):
        automatic_target_context([target], True)
    with pytest.raises(ValueError, match="Invalid policy target scope"):
        deployment_policy(target, True)
    with pytest.raises(AnalysisError, match="사용할 수 없는"):
        validate_infrastructure_proposal(
            {
                "target": target,
                "workload": "stateless-http",
                "rationale": "test",
                "evidence": [{"file": "app.py", "quote": "hello"}],
            },
            {"app.py": "hello"},
            ["aws-ecs-express"],
        )


def test_architecture_decision_records_unselected_unsupported_options(tmp_path):
    profile = inspect(tmp_path, {"app.py": "import uvicorn\nuvicorn.run(app)\n"})
    candidates = [{**item, "selected": item["id"] == "aws-ecs-express"} for item in compare(profile)[1]]
    plan = {
        "target": "aws-ecs-express",
        "planner": "openai",
        "candidates": candidates,
        "compatibility": infrastructure_compatibility(profile, "aws-ecs-express", public_access=True),
    }
    decision = architecture_decision(
        application_ir(profile, "c" * 64).as_dict(), deployment_policy("auto", True), plan
    )
    options = {item.target: item for item in decision.candidates}
    assert options["aws-lambda"].eligibility == "unsupported_by_sky"
    assert options["aws-lambda"].selection == "not_selected"
    assert options["aws-ec2"].selection == "not_selected"
    assert "SKY_ADAPTER_UNIMPLEMENTED" in options["aws-lambda"].reason_codes
    candidates[1]["status"] = "unsupported_by_sky"
    with pytest.raises(ValueError, match="selection disagrees"):
        architecture_decision(
            application_ir(profile, "c" * 64).as_dict(), deployment_policy("auto", True), plan
        )


@pytest.mark.parametrize(
    "path, source",
    [
        ("app.py", "# def lambda_handler(event, context):\n# subprocess.run(['modprobe', 'kvm'])\n"),
        ("app.py", "example = \"subprocess.run(['modprobe', 'kvm'])\"\n"),
        ("app.js", "/* exports.handler = async (e) => {}; */\n// app.listen(8080)\n"),
        ("app.js", "const example = 'exports.handler = {}; app.listen(80)';\n"),
    ],
)
def test_comments_and_examples_are_not_runtime_observations(path, source):
    assert runtime_signals(path, source) == set()


def test_aliases_device_calls_and_invalid_syntax_remain_bounded():
    assert runtime_signals("app.py", "from uvicorn import run as serve\nserve(app)") == {
        "persistent-http-server"
    }
    assert runtime_signals("app.py", "open('/dev/kvm', 'rb')") == {"host-device-access"}
    assert runtime_signals("app.py", "open('/dev/null', 'w')") == set()
    assert runtime_signals("app.py", "import flask\nother.run()") == set()
    assert runtime_signals("app.py", "from flask import Flask\nweb=Flask(__name__)\nweb.run()") == {
        "persistent-http-server"
    }
    assert runtime_signals("app.py", "def incomplete(") == {"runtime-syntax-unresolved"}
    assert runtime_signals("handler.ts", "export const handler = async (event: any) => ({body: 'ok'});") == {
        "function-handler"
    }


def test_fixed_decision_and_certificate_preserve_comparisons_and_pending_evidence(tmp_path):
    profile = inspect(tmp_path, {"app.py": "import uvicorn\nuvicorn.run(app)\n"})
    ir = application_ir(profile, "d" * 64).as_dict()
    policy = deployment_policy("local-docker", False)
    plan = with_aws_backend_options(
        profile,
        {
            "target": "local-docker",
            "planner": "user",
            "compatibility": infrastructure_compatibility(profile, "local-docker", public_access=False),
        },
    )
    decision = architecture_decision(ir, policy, plan).as_dict()
    assert [item["target"] for item in decision["candidates"]] == ["local-docker", "aws-lambda", "aws-ec2"]
    job = {
        "id": "a" * 16,
        "target": "local-docker",
        "source_digest": "d" * 64,
        "application_ir": ir,
        "deployment_policy": policy.as_dict(),
        "infrastructure_plan": plan,
        "architecture_decision": decision,
    }
    trace = deployment_certificate(job)["decision_trace"]
    assert trace["status"] == "recorded"
    assert trace["unresolved_evidence_ids"] == []
    function = next(item for item in trace["candidate_evaluations"] if item["target"] == "aws-lambda")
    assert function["structural_status"] == "requires_transformation"
    assert function["execution_status"] == "unsupported_by_sky"
    assert function["evidence_ids"]
    assert "LAMBDA-DURATION-01" in function["pending_verification_rule_ids"]
    job["infrastructure_plan"]["architecture_options"][0]["selected"] = True
    assert deployment_certificate(job)["decision_trace"]["status"] == "incomplete"
    with pytest.raises(ValueError, match="cannot be selected"):
        architecture_decision(ir, policy, plan)
