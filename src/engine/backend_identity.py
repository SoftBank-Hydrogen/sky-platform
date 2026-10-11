"""Stable target IDs with separate provider and execution-backend identities.

Persisted jobs keep their original target ID. This registry only adds meaning
for new decisions and previews; it does not rewrite historical records.
"""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class BackendIdentity:
    target: str
    provider: str
    backend: str
    sky_adapter_support: str
    selection_mode: str

    def as_dict(self) -> dict:
        return asdict(self)


BACKENDS = {
    item.target: item
    for item in (
        BackendIdentity("local-docker", "local", "docker", "implemented", "automatic"),
        BackendIdentity("onprem-compose", "onprem", "compose_same_host", "implemented", "explicit_only"),
        BackendIdentity("onprem-vm", "onprem", "compose_remote_vm", "implemented", "explicit_only"),
        BackendIdentity("cloud-run", "gcp", "cloud_run", "implemented", "automatic"),
        BackendIdentity("aws-ecs-express", "aws", "ecs_express", "implemented", "automatic"),
        BackendIdentity("aws-s3-cloudfront", "aws", "static_hosting", "implemented", "automatic"),
        BackendIdentity("aws-ecs-standard", "aws", "ecs_standard", "unimplemented", "unavailable"),
        BackendIdentity("aws-lambda", "aws", "lambda_request_handler", "unimplemented", "unavailable"),
        BackendIdentity("aws-ec2", "aws", "ec2_host", "unimplemented", "unavailable"),
    )
}


def backend_identity(target: str) -> BackendIdentity:
    try:
        return BACKENDS[target]
    except KeyError as exc:
        raise ValueError("Unsupported deployment target") from exc
