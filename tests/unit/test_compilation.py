"""Compilation outputs must stay tied to the same source and decision."""

import copy

import pytest

from engine.application_ir import application_ir
from engine.architecture_decision import architecture_decision
from engine.compatibility import InfrastructureProfile, infrastructure_compatibility
from engine.compilation import compile_decision, verify_compilation
from engine.deployment_policy import deployment_policy
from engine.target_lowering import lower_target_configuration


def test_compilation_binds_both_outputs_to_one_decision():
    profile = InfrastructureProfile("unconfirmed", (), 0)
    ir = application_ir(profile, "a" * 64).as_dict()
    policy = deployment_policy("local-docker", False)
    plan = {
        "target": "local-docker",
        "planner": "user",
        "resources": ["Docker image", "local container"],
        "compatibility": infrastructure_compatibility(profile, "local-docker", public_access=False),
    }
    decision = architecture_decision(ir, policy, plan).as_dict()
    record = compile_decision(decision, ir, policy, plan)
    assert record["schema_version"] == 2
    assert record["target_plan"]["execution_configuration"] == {
        "service": "source-bundle",
        "replicas": 1,
        "access_mode": "loopback",
        "database_mode": "none",
        "required_image_platform": None,
        "port_source": "executable_deployment_plan",
    }
    assert record == compile_decision(decision, ir, policy, plan)
    for output in (record["source_patch_plan"], record["deployment_ir"], record["target_plan"]):
        assert output["compilation_id"] == record["compilation_id"]
        assert output["decision_revision"] == decision["decision_revision"]
        assert output["source_revision"] == ir["source_revision"]
    assert record["source_patch_plan"]["status"] == "pending_source_transform"
    assert record["source_patch_plan"]["changes"] == []
    verify_compilation(record, decision, ir, policy, plan)

    mixed = copy.deepcopy(record)
    mixed["target_plan"]["compilation_id"] = "comp-other"
    with pytest.raises(ValueError, match="Stored compilation"):
        verify_compilation(mixed, decision, ir, policy, plan)
    changed = copy.deepcopy(record)
    changed["source_patch_plan"]["changes"].append({"path": "server.js"})
    with pytest.raises(ValueError, match="Stored compilation"):
        verify_compilation(changed, decision, ir, policy, plan)
    changed_configuration = copy.deepcopy(record)
    changed_configuration["target_plan"]["execution_configuration"]["database_mode"] = "create_rds"
    with pytest.raises(ValueError, match="Stored compilation"):
        verify_compilation(changed_configuration, decision, ir, policy, plan)
    legacy = copy.deepcopy(record)
    legacy.pop("schema_version")
    legacy["target_plan"].pop("execution_configuration")
    verify_compilation(legacy, decision, ir, policy, plan)
    plan["resources"].append("unplanned resource")
    with pytest.raises(ValueError, match="source and plan"):
        verify_compilation(record, decision, ir, policy, plan)


def test_remote_vm_compiles_as_public_remote_compose_not_loopback():
    profile = InfrastructureProfile("unconfirmed", (), 0)
    ir = application_ir(profile, "d" * 64).as_dict()
    policy = deployment_policy("onprem-vm", True)
    plan = {
        "target": "onprem-vm",
        "planner": "user",
        "resources": ["Docker image", "remote Linux VM Compose service"],
        "compatibility": infrastructure_compatibility(profile, "onprem-vm", public_access=True),
    }
    decision = architecture_decision(ir, policy, plan).as_dict()
    compiled = compile_decision(decision, ir, policy, plan)
    assert compiled["target_plan"]["execution_configuration"]["access_mode"] == "public"
    assert compiled["target_plan"]["execution_configuration"]["database_mode"] == "none"
    assert lower_target_configuration(plan, compiled["deployment_ir"])["access_mode"] == "public"
    verify_compilation(compiled, decision, ir, policy, plan)


@pytest.mark.parametrize(
    "binding,resource,mode",
    [
        ("create", "new RDS PostgreSQL", "create_rds"),
        ("existing", "existing RDS PostgreSQL", "existing_rds"),
    ],
)
def test_lowering_requires_matching_database_resource(binding, resource, mode):
    plan = {
        "target": "aws-ecs-express",
        "resources": [
            "CloudFormation base stack",
            "ECR repository",
            "ECS Express service",
            resource,
            "one-off SQL migration task",
        ],
        "compatibility": {
            "target": "aws-ecs-express",
            "compatible": True,
            "access_mode": "public",
            "postgres_binding": True,
        },
        "database": {"binding": binding, "database_id": "sky-game"},
    }
    deployment_ir = {"services": [{"id": "source-bundle", "kind": "container_service", "replicas": 1}]}
    lowered = lower_target_configuration(plan, deployment_ir)
    assert lowered["database_mode"] == mode
    assert lowered["required_image_platform"] == "linux/amd64"
    plan["resources"].remove(resource)
    with pytest.raises(ValueError, match="CV-09.*PostgreSQL resources"):
        lower_target_configuration(plan, deployment_ir)


def test_lowering_rejects_database_without_binding_and_extra_replicas():
    plan = {
        "target": "local-docker",
        "resources": ["Docker image", "local container"],
        "compatibility": {"target": "local-docker", "compatible": True, "access_mode": "loopback"},
    }
    deployment_ir = {"services": [{"id": "source-bundle", "kind": "container_service", "replicas": 1}]}
    plan["sqlite_volume"] = {"mount_path": "/data"}
    with pytest.raises(ValueError, match="CV-09.*no selected binding"):
        lower_target_configuration(plan, deployment_ir)
    plan["compatibility"]["local_sqlite_binding"] = True
    assert lower_target_configuration(plan, deployment_ir)["database_mode"] == "sqlite_volume"
    deployment_ir["services"][0]["replicas"] = 2
    with pytest.raises(ValueError, match="CV-09.*selected service"):
        lower_target_configuration(plan, deployment_ir)
