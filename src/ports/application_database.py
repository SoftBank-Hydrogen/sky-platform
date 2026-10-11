"""Validate trusted allocation-worker receipts; never accept browser-supplied credentials."""

import re
from dataclasses import asdict, dataclass

from ports.artifacts import SourceArtifact


@dataclass(frozen=True)
class ApplicationDatabaseReference:
    organization_id: str
    application_id: str
    account_id: str
    region: str
    instance_id: str
    allocation_id: str
    database_name: str
    login_role: str
    secret_ref: str
    connection_limit: int
    source_digest: str

    def __post_init__(self):
        for name in ("organization_id", "application_id", "instance_id"):
            if not isinstance(getattr(self, name), str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", getattr(self, name)
            ):
                raise ValueError("Invalid application database ownership")
        if (
            not isinstance(self.account_id, str)
            or not re.fullmatch(r"[0-9]{12}", self.account_id)
            or not isinstance(self.region, str)
            or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", self.region)
            or not isinstance(self.allocation_id, str)
            or not re.fullmatch(r"[0-9a-f]{24}", self.allocation_id)
            or self.database_name != "sky_app_" + self.allocation_id
            or self.login_role != "sky_login_" + self.allocation_id
            or type(self.connection_limit) is not int
            or not 1 <= self.connection_limit <= 50
            or not isinstance(self.source_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.source_digest)
        ):
            raise ValueError("Invalid application database reference")
        if not isinstance(self.secret_ref, str) or not re.fullmatch(
            rf"arn:aws:secretsmanager:{self.region}:{self.account_id}:secret:[A-Za-z0-9/_+=.@-]+",
            self.secret_ref,
        ):
            raise ValueError("Application secret must remain in the allocated AWS scope")

    def record(self):
        return asdict(self)


def reference_from_allocation(preparation, receipt, *, account_id, region, allocation_id):
    """Caller must load receipt from the trusted allocation job, never the request body.

    Checks references and evidence shape; does not prove live SQL ACL or perform migrations.
    Dedicated/promotion receipts require their own future contract rather than guessing.
    """
    if (
        not isinstance(preparation, dict)
        or not isinstance(receipt, dict)
        or not isinstance(allocation_id, str)
        or not re.fullmatch(r"[0-9a-f]{24}", allocation_id)
    ):
        raise ValueError("Invalid trusted allocation documents")
    if (
        preparation.get("status") != "database_binding_required"
        or preparation.get("transformation", {}).get("profile") != "tug-postgres-v1"
    ):
        raise ValueError("PostgreSQL-converted source required")
    original = SourceArtifact.from_record(preparation["original_ref"])
    prepared = SourceArtifact.from_record(preparation["prepared_ref"])
    if (
        original.kind != "original"
        or prepared.kind != "prepared"
        or (original.organization_id, original.application_id, original.upload_id)
        != (prepared.organization_id, prepared.application_id, prepared.upload_id)
        or prepared.source_digest != preparation["transformation"]["source_digest"]
    ):
        raise ValueError("Prepared database source differs from transformation")
    binding = receipt.get("binding", {})
    if not isinstance(binding, dict):
        raise TypeError("Invalid allocation binding")
    if (
        receipt.get("status") != "ready"
        or receipt.get("allocation_id") != allocation_id
        or receipt.get("verified_scope") != "postgresql_role_and_database_acl"
        or binding.get("role") != "shared_workload"
        or binding.get("owner_ref") != "allocation-" + allocation_id
        or (binding.get("organization_id"), binding.get("application_id"))
        != (prepared.organization_id, prepared.application_id)
        or (binding.get("account_id"), binding.get("region")) != (account_id, region)
    ):
        raise ValueError("Allocation receipt does not own this prepared application")
    return ApplicationDatabaseReference(
        prepared.organization_id,
        prepared.application_id,
        account_id,
        region,
        binding["instance_id"],
        allocation_id,
        binding["database_name"],
        receipt["login_role"],
        receipt["secret_ref"],
        receipt["connection_limit"],
        prepared.source_digest,
    )
