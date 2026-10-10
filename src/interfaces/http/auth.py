"""The local-only API authentication boundary.

Hosted authentication must replace this boundary with a verified user identity;
the browser token is deliberately not treated as a multi-user credential.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from typing import Protocol

from domain.access import LoginSource, Principal, Role


class RequestAuthenticator(Protocol):
    """An injected hosted authenticator must verify request identity itself."""

    def authenticate_request(self, headers: Mapping[str, str]) -> Principal | None: ...


class LocalTokenAuthenticator:
    def __init__(self, token: str):
        if not token:
            raise ValueError("Local session token is required")
        self._token = token

    def authenticate(self, supplied_token: str | None) -> Principal | None:
        if not isinstance(supplied_token, str) or not hmac.compare_digest(supplied_token, self._token):
            return None
        return Principal(
            user_id="local_operator",
            organization_id="local_workspace",
            role=Role.ADMIN,
            login_source=LoginSource.LOCAL,
        )

    def authenticate_request(self, headers: Mapping[str, str]) -> Principal | None:
        return self.authenticate(headers.get("X-Sky-Token"))
