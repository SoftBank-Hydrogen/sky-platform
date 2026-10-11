"""Ownership and location of a workload or control-plane database."""

import re
from dataclasses import dataclass

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_ROLES = {"shared_workload", "dedicated_workload", "sky_state"}


@dataclass(frozen=True)
class DatabaseBinding:
    organization_id: str
    application_id: str
    role: str
    account_id: str
    region: str
    instance_id: str
    database_name: str
    owner_ref: str

    def __post_init__(self) -> None:
        for name in (
            "organization_id",
            "application_id",
            "region",
            "instance_id",
            "database_name",
            "owner_ref",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise ValueError(f"Invalid database binding {name}")
        if (
            not isinstance(self.role, str)
            or self.role not in _ROLES
            or not isinstance(self.account_id, str)
            or not re.fullmatch(r"[0-9]{12}", self.account_id)
        ):
            raise ValueError("Invalid database binding role or account")
