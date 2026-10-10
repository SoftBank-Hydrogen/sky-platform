"""Authorization facts shared by future hosted HTTP and worker boundaries.

Login origin is recorded for audit only. A verified membership grants access;
neither an email domain nor a login provider grants permissions by itself.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


class LoginSource(StrEnum):
    CORPORATE_SSO = "corporate_sso"
    EXTERNAL_IDP = "external_idp"
    LOCAL = "local"


class Role(StrEnum):
    ADMIN = "admin"
    DEPLOYER = "deployer"
    VIEWER = "viewer"


class Action(StrEnum):
    READ = "read"
    DEPLOY = "deploy"
    RETIRE = "retire"
    MANAGE_MEMBERS = "manage_members"


class AccessResult(StrEnum):
    GRANTED = "granted"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"


_GRANTS = {
    Role.VIEWER: frozenset({Action.READ}),
    Role.DEPLOYER: frozenset({Action.READ, Action.DEPLOY}),
    Role.ADMIN: frozenset(Action),
}


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"Invalid {name}")
    return value


@dataclass(frozen=True, slots=True)
class Principal:
    """Identity and membership supplied by a verified authentication boundary."""

    user_id: str
    organization_id: str
    role: Role
    login_source: LoginSource

    def __post_init__(self) -> None:
        _identifier(self.user_id, "user_id")
        _identifier(self.organization_id, "organization_id")
        if not isinstance(self.role, Role) or not isinstance(self.login_source, LoginSource):
            raise TypeError("Invalid principal role or login source")


@dataclass(frozen=True, slots=True)
class ResourceOwner:
    organization_id: str
    created_by: str

    def __post_init__(self) -> None:
        _identifier(self.organization_id, "organization_id")
        _identifier(self.created_by, "created_by")

    def record(self) -> dict[str, str]:
        return {"organization_id": self.organization_id, "created_by": self.created_by}


def owner_from_record(record: Mapping[str, object]) -> ResourceOwner | None:
    """Legacy or malformed records have no claimable owner."""
    try:
        return ResourceOwner(record["organization_id"], record["created_by"])
    except (KeyError, ValueError, TypeError):
        return None


def permitted(principal: Principal, action: Action, owner: ResourceOwner | None) -> bool:
    if not isinstance(principal, Principal) or not isinstance(action, Action) or owner is None:
        return False
    return principal.organization_id == owner.organization_id and action in _GRANTS[principal.role]


def record_access(principal: Principal, action: Action, record: Mapping[str, object] | None) -> AccessResult:
    """Hide foreign records; allow ownerless legacy records only in single-user local mode."""
    if not isinstance(principal, Principal) or not isinstance(action, Action) or record is None:
        return AccessResult.NOT_FOUND
    owner = owner_from_record(record)
    if owner is None:
        legacy = "organization_id" not in record and "created_by" not in record
        if (
            legacy
            and principal.login_source is LoginSource.LOCAL
            and principal.role is Role.ADMIN
            and principal.organization_id == "local_workspace"
            and principal.user_id == "local_operator"
        ):
            return AccessResult.GRANTED
        return AccessResult.NOT_FOUND
    if owner.organization_id != principal.organization_id:
        return AccessResult.NOT_FOUND
    return AccessResult.GRANTED if permitted(principal, action, owner) else AccessResult.FORBIDDEN
