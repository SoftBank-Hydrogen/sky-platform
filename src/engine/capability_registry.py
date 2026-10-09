"""Versioned target capabilities, separate from provider claims and live verification."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from engine.backend_identity import backend_identity
from engine.compatibility import TARGET_CAPABILITIES

# Keep resource execution support independent of the resources a planner emits.
RESOURCE_CAPABILITY_IDS = {
    "Docker image": "docker_image",
    "local container": "local_container",
    "same-host Compose service": "compose_service",
    "optional SQLite volume": "sqlite_volume",
    "Artifact Registry repository": "artifact_registry_repository",
    "runtime service account": "runtime_service_account",
    "Cloud Run service": "cloud_run_service",
    "CloudFormation base stack": "cloudformation_base_stack",
    "ECR repository": "ecr_repository",
    "ECS Express service": "ecs_express_service",
    "new RDS PostgreSQL": "new_rds_provisioning",
    "existing RDS PostgreSQL": "existing_rds_binding",
    "one-off SQL migration task": "sql_migration_task",
    "private S3 bucket": "private_s3_bucket",
    "CloudFront distribution": "cloudfront_distribution",
}

_IMPLEMENTED_RESOURCE_CAPABILITIES = {
    "local-docker": {"docker_image", "local_container"},
    "onprem-compose": {"docker_image", "compose_service", "sqlite_volume"},
    "cloud-run": {"artifact_registry_repository", "runtime_service_account", "cloud_run_service"},
    "aws-ecs-express": {
        "cloudformation_base_stack",
        "ecr_repository",
        "ecs_express_service",
        "new_rds_provisioning",
        "existing_rds_binding",
        "sql_migration_task",
    },
}


@dataclass(frozen=True)
class Capability:
    id: str
    provider_support: str
    sky_adapter_support: str
    verification_status: str = "unverified"
    verification_refs: tuple[str, ...] = ()
    configurations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.provider_support not in {"supported", "unsupported", "unknown"}:
            raise ValueError("Invalid provider capability status")
        if self.sky_adapter_support not in {"implemented", "unimplemented", "unknown"}:
            raise ValueError("Invalid Sky adapter capability status")
        if self.verification_status not in {"verified", "failed", "unverified"}:
            raise ValueError("Invalid capability verification status")
        if self.verification_status == "verified" and not self.verification_refs:
            raise ValueError("Verified capability requires a verification reference")
        if self.verification_status == "verified" and self.sky_adapter_support != "implemented":
            raise ValueError("Unimplemented capability cannot be verified")

    @property
    def display_status(self) -> str:
        if self.sky_adapter_support == "unimplemented":
            return "unsupported_by_sky"
        if self.sky_adapter_support == "unknown":
            return "unknown"
        if self.verification_status == "failed":
            return "failed"
        if self.verification_status == "verified":
            return "supported"
        return "implemented_unverified"

    def as_dict(self) -> dict:
        return {**asdict(self), "display_status": self.display_status}


@dataclass(frozen=True)
class TargetCapabilityModel:
    schema_version: int
    target: str
    adapter_contract_version: int
    capabilities: tuple[Capability, ...]

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "target": self.target,
            "provider": backend_identity(self.target).provider,
            "backend": backend_identity(self.target).backend,
            "adapter_contract_version": self.adapter_contract_version,
            "capabilities": [item.as_dict() for item in self.capabilities],
        }


def target_capability_model(target: str) -> TargetCapabilityModel:
    """Report implemented paths conservatively; validation refs are added only with scoped proof."""
    backend_identity(target)
    if target in {"aws-s3-cloudfront", "aws-ecs-standard"}:
        implemented = target == "aws-s3-cloudfront"
        static_capabilities = {
            "static_files": implemented,
            "private_s3_bucket": implemented,
            "cloudfront_distribution": implemented,
            "access_public": implemented,
            "container_runtime": False,
            "access_loopback": False,
            "access_authenticated": False,
            "sqlite_volume": False,
            "durable_file_volume": False,
            "background_worker": False,
            "existing_rds_binding": False,
            "new_rds_provisioning": False,
            "remote_host": False,
        }
        # Standard ECS is a documented architecture option, not a Sky adapter.
        # No provider feature is asserted until it is scoped and verified.
        return TargetCapabilityModel(
            1,
            target,
            1,
            tuple(
                Capability(name, "unknown", "implemented" if enabled else "unimplemented")
                for name, enabled in static_capabilities.items()
            ),
        )
    declared = TARGET_CAPABILITIES[target]
    available = {
        "container_runtime": True,
        "access_loopback": "loopback" in declared["access_modes"],
        "access_authenticated": "authenticated" in declared["access_modes"],
        "access_public": "public" in declared["access_modes"],
        "sqlite_volume": declared["sqlite_volume"],
        "durable_file_volume": declared["durable_files"],
        "background_worker": declared["background_worker"],
        "existing_rds_binding": declared["existing_rds_binding"],
        "new_rds_provisioning": declared["new_rds_provisioning"],
        "remote_host": declared["remote_host"],
    }
    available.update(
        {
            capability: capability in _IMPLEMENTED_RESOURCE_CAPABILITIES[target]
            for capability in RESOURCE_CAPABILITY_IDS.values()
            if capability not in available
        }
    )
    configurations = {
        "existing_rds_binding": ("existing_rds",),
        "new_rds_provisioning": ("create_rds",),
    }
    capabilities = tuple(
        Capability(
            id=name,
            provider_support="unknown",
            sky_adapter_support="implemented" if enabled else "unimplemented",
            configurations=configurations.get(name, ()),
        )
        for name, enabled in available.items()
    )
    return TargetCapabilityModel(1, target, 1, capabilities)
