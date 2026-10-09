"""User scope must be checked separately from target compatibility."""

import pytest

from engine.deployment_policy import deployment_policy


def test_auto_target_respects_public_permission():
    private = deployment_policy("auto", False)
    public = deployment_policy("auto", True)
    assert private.selection_mode == "auto_target"
    assert "aws-ecs-express" not in private.allowed_targets
    assert "aws-ecs-express" in public.allowed_targets
    assert private.max_monthly_cost_usd is None
    with pytest.raises(ValueError, match="허용 범위"):
        private.require("aws-ecs-express", "public")
    private.require("cloud-run", "authenticated")
    with pytest.raises(ValueError, match="접근 범위"):
        private.require("cloud-run", "public")


def test_explicit_database_and_migration_intent_are_independent():
    ordinary = deployment_policy("aws-ecs-express", True)
    ordinary.require("aws-ecs-express", "public")
    with pytest.raises(ValueError, match="DB 생성 계획"):
        ordinary.require("aws-ecs-express", "public", new_managed_database=True)
    with pytest.raises(ValueError, match="데이터 이전"):
        ordinary.require("aws-ecs-express", "public", data_migration=True)
    approved = deployment_policy(
        "aws-ecs-express",
        True,
        new_managed_database_approved=True,
        allow_data_migration=True,
    )
    approved.require("aws-ecs-express", "public", new_managed_database=True, data_migration=True)
    assert approved.preserve_databases


def test_fixed_multi_target_policy_cannot_expand_selection():
    policy = deployment_policy(("local-docker", "cloud-run"), False)
    assert policy.selection_mode == "fixed_target"
    assert policy.allowed_targets == ("local-docker", "cloud-run")
    policy.require("local-docker", "loopback")
    with pytest.raises(ValueError, match="허용 범위"):
        policy.require("aws-ecs-express", "public")


def test_unknown_or_duplicate_target_is_rejected():
    with pytest.raises(ValueError, match="target scope"):
        deployment_policy(("local-docker", "local-docker"), False)
    with pytest.raises(ValueError, match="target scope"):
        deployment_policy("imaginary-cloud", False)
