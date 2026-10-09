"""Guard explicit browser endpoint configuration before cloud deployment."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit


_BROWSER_URL_NAME = re.compile(
    r"^(?:VITE_|NEXT_PUBLIC_|REACT_APP_|PUBLIC_|CLIENT_)(?:[A-Z0-9_]*_)?"
    r"(?:API_URL|BACKEND_URL|SERVICE_URL|WS_URL|WEBSOCKET_URL|BASE_URL)$"
)


def check_browser_client_urls(environment: dict[str, str], target: str) -> None:
    """Reject explicit browser endpoints that cannot work from a cloud HTTPS page."""
    if target not in {"aws-ecs-express", "cloud-run"}:
        return
    for name, value in environment.items():
        if not _BROWSER_URL_NAME.fullmatch(name) or not value:
            continue
        if "\\" in value or any(char.isspace() or ord(char) < 32 for char in value):
            raise ValueError(f"CV-05: Invalid browser endpoint in {name}")
        try:
            parsed = urlsplit(value)
            host = parsed.hostname
            port = parsed.port
        except ValueError:
            raise ValueError(f"CV-05: Invalid browser endpoint in {name}") from None
        if not parsed.scheme and not parsed.netloc:
            if value.startswith("/") and not value.startswith("//"):
                continue
            raise ValueError(f"CV-05: Browser endpoint in {name} must be a relative path or secure URL")
        if parsed.scheme not in {"https", "wss", ""} or not host or port == 0 or parsed.username or parsed.password:
            raise ValueError(f"CV-05: Browser endpoint in {name} must use HTTPS or WSS")
        if host == "localhost" or host.endswith(".localhost"):
            raise ValueError(f"CV-05: Browser endpoint in {name} points to the user's own device")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            continue
        if address.is_loopback or address.is_unspecified:
            raise ValueError(f"CV-05: Browser endpoint in {name} points to the user's own device")
