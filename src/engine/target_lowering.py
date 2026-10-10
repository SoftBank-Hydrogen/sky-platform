"""Lower a selected infrastructure plan to adapter-facing execution settings."""

from __future__ import annotations

from engine.capability_registry import RESOURCE_CAPABILITY_IDS, target_capability_model
from engine.compatibility import TARGET_CAPABILITIES


def lower_target_configuration(plan: dict, deployment_ir: dict) -> dict:
    """Describe only settings backed by the selected adapter and explicit bindings."""
    target = plan.get("target") if isinstance(plan, dict) else None
    if target == "aws-s3-cloudfront":
        compatibility = plan.get("compatibility")
        resources = plan.get("resources")
        services = deployment_ir.get("services") if isinstance(deployment_ir, dict) else None
        requirements = deployment_ir.get("requirements") if isinstance(deployment_ir, dict) else None
        if (
            not isinstance(compatibility, dict)
            or compatibility.get("target") != target
            or compatibility.get("compatible") is not True
            or compatibility.get("access_mode") != "public"
            or resources != ["private S3 bucket", "CloudFront distribution"]
            or services != [{"id": "source-bundle", "kind": "static_site"}]
            or not isinstance(requirements, (list, tuple))
            or len(requirements) != 1
            or not isinstance(requirements[0], dict)
            or requirements[0].get("id") != "R-static-assets"
            or requirements[0].get("kind") != "static-assets"
            or not isinstance(requirements[0].get("evidence_ids"), (list, tuple))
            or not requirements[0]["evidence_ids"]
        ):
            raise ValueError("CV-09: Static plan cannot lower the selected assets")
        capabilities = {item.id: item for item in target_capability_model(target).capabilities}
        for resource in resources:
            capability = capabilities.get(RESOURCE_CAPABILITY_IDS[resource])
            if capability is None or capability.sky_adapter_support != "implemented":
                raise ValueError(f"CV-09: No implemented adapter capability for resource {resource}")
        return {
            "service": "source-bundle",
            "asset_source": "uploaded_source",
            "index_file": "index.html",
            "access_mode": "public",
            "database_mode": "none",
        }
    if target not in TARGET_CAPABILITIES:
        raise ValueError("CV-09: Unsupported execution target")
    compatibility = plan.get("compatibility")
    resources = plan.get("resources")
    services = deployment_ir.get("services") if isinstance(deployment_ir, dict) else None
    if (
        not isinstance(compatibility, dict)
        or compatibility.get("target") != target
        or compatibility.get("compatible") is not True
        or not isinstance(resources, list)
        or not resources
        or not all(isinstance(resource, str) for resource in resources)
        or len(resources) != len(set(resources))
        or not isinstance(services, list)
        or len(services) != 1
        or services[0] != {"id": "source-bundle", "kind": "container_service", "replicas": 1}
    ):
        raise ValueError("CV-09: Target plan cannot lower the selected service")
    capabilities = {item.id: item for item in target_capability_model(target).capabilities}
    for resource in resources:
        capability = capabilities.get(RESOURCE_CAPABILITY_IDS.get(resource))
        if capability is None or capability.sky_adapter_support != "implemented":
            raise ValueError(f"CV-09: No implemented adapter capability for resource {resource}")
    access_mode = compatibility.get("access_mode")
    access = capabilities.get(f"access_{access_mode}")
    if access is None or access.sky_adapter_support != "implemented":
        raise ValueError("CV-09: Target access mode is not implemented by the adapter")

    database = plan.get("database")
    sqlite_volume = plan.get("sqlite_volume")
    postgres_binding = compatibility.get("postgres_binding") is True
    local_sqlite_binding = compatibility.get("local_sqlite_binding") is True
    if postgres_binding and local_sqlite_binding:
        raise ValueError("CV-09: Conflicting database bindings")
    rds_resources = {"new RDS PostgreSQL", "existing RDS PostgreSQL", "existing Cloud SQL PostgreSQL"}
    if postgres_binding:
        if not isinstance(database, dict) or database.get("binding") not in {"create", "existing"}:
            raise ValueError("CV-09: PostgreSQL binding is missing")
        binding = database["binding"]
        required = "new RDS PostgreSQL" if binding == "create" else "existing RDS PostgreSQL"
        if target == 'cloud-run':
            if binding != 'existing':
                raise ValueError('CV-09: Cloud SQL creation is not implemented')
            required = 'existing Cloud SQL PostgreSQL'
        if (
            target not in {"aws-ecs-express", "cloud-run"}
            or not isinstance(database.get("database_id"), str)
            or not database["database_id"]
            or required not in resources
            or "one-off SQL migration task" not in resources
            or len(rds_resources.intersection(resources)) != 1
            or sqlite_volume is not None
        ):
            raise ValueError("CV-09: PostgreSQL resources disagree with the selected binding")
        database_mode = ('existing_cloud_sql' if target == 'cloud-run' else
                         'create_rds' if binding == 'create' else 'existing_rds')
    elif local_sqlite_binding:
        if (
            target not in {"local-docker", "onprem-compose"}
            or not isinstance(sqlite_volume, dict)
            or database is not None
            or rds_resources.intersection(resources)
        ):
            raise ValueError("CV-09: SQLite volume disagrees with the selected target")
        database_mode = "sqlite_volume"
    else:
        if database is not None or sqlite_volume is not None or rds_resources.intersection(resources):
            raise ValueError("CV-09: Database resource has no selected binding")
        database_mode = "none"
    return {
        "service": "source-bundle",
        "replicas": 1,
        "access_mode": access_mode,
        "database_mode": database_mode,
        "required_image_platform": TARGET_CAPABILITIES[target]["image_platform"],
        "port_source": "executable_deployment_plan",
    }
