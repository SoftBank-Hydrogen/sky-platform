"""Sky support and target verification have separate capability states."""

import pytest

from engine.capability_registry import Capability, target_capability_model


def by_id(target: str) -> dict[str, Capability]:
    return {item.id: item for item in target_capability_model(target).capabilities}


def test_unverified_implementation_is_not_reported_as_supported():
    aws = by_id("aws-ecs-express")
    assert aws["container_runtime"].display_status == "implemented_unverified"
    assert aws["access_public"].display_status == "implemented_unverified"
    assert aws["sqlite_volume"].display_status == "unsupported_by_sky"
    assert aws["existing_rds_binding"].configurations == ("existing_rds",)
    assert aws["new_rds_provisioning"].configurations == ("create_rds",)
    assert aws["existing_rds_binding"].verification_refs == ()
    assert aws["new_rds_provisioning"].verification_refs == ()
    assert all(item.display_status != "supported" for item in aws.values())


def test_verification_is_scoped_to_one_capability():
    with pytest.raises(ValueError, match="verification reference"):
        Capability("existing_rds_binding", "unknown", "implemented", "verified")
    confirmed = Capability(
        "existing_rds_binding", "unknown", "implemented", "verified", ("live-existing-rds-001",)
    )
    assert confirmed.display_status == "supported"
    assert by_id("aws-ecs-express")["new_rds_provisioning"].display_status == "implemented_unverified"
    with pytest.raises(ValueError, match="Unimplemented"):
        Capability("new_rds_provisioning", "unknown", "unimplemented", "verified", ("ref",))


def test_onprem_capabilities_do_not_claim_remote_host():
    compose = by_id("onprem-compose")
    assert compose["sqlite_volume"].sky_adapter_support == "implemented"
    assert compose["remote_host"].display_status == "unsupported_by_sky"
    assert compose["access_public"].display_status == "unsupported_by_sky"
    assert target_capability_model("onprem-compose").schema_version == 1


def test_unknown_target_is_rejected():
    with pytest.raises(ValueError, match="Unsupported"):
        target_capability_model("imaginary-cloud")


def test_aws_backends_do_not_share_adapter_support():
    express = target_capability_model("aws-ecs-express").as_dict()
    static = target_capability_model("aws-s3-cloudfront").as_dict()
    standard = target_capability_model("aws-ecs-standard").as_dict()
    assert {model["provider"] for model in (express, static, standard)} == {"aws"}
    assert {model["backend"] for model in (express, static, standard)} == {
        "ecs_express",
        "static_hosting",
        "ecs_standard",
    }
    assert by_id("aws-s3-cloudfront")["static_files"].sky_adapter_support == "implemented"
    assert by_id("aws-s3-cloudfront")["container_runtime"].sky_adapter_support == "unimplemented"
    assert all(item.display_status == "unsupported_by_sky" for item in by_id("aws-ecs-standard").values())
