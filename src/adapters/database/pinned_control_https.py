"""No-retry private HTTPS control transport with operator-pinned network address.

The caller must audit the owning ECS task/service before each command. Address
pinning does not replace certificate/hostname verification or execution leases.
The TLS identity remains the registered DNS name even when a task IP is used.
"""

import http.client
import ipaddress
import json
import socket
import ssl
from urllib.parse import urlsplit


class PinnedHTTPSControlTransport:
    def __init__(self, endpoint, token, *, address, ssl_context, guard):
        url = urlsplit(endpoint)
        ip = ipaddress.ip_address(address)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or not ip.is_private
            or ip.is_unspecified
            or ip.is_multicast
            or not isinstance(token, str)
            or not token
            or "\r" in token
            or "\n" in token
            or ssl_context.verify_mode != ssl.CERT_REQUIRED
            or not ssl_context.check_hostname
        ):
            raise ValueError("Registered private address, TLS identity and control credential required")
        self.url, self.address, self._token = url, str(ip), token
        self.context, self.guard = ssl_context, guard

    def __call__(self, command):
        self.guard()
        connection = http.client.HTTPSConnection(self.url.hostname, self.url.port or 443, timeout=30)
        try:
            raw = socket.create_connection((self.address, self.url.port or 443), timeout=30)
            try:
                connection.sock = self.context.wrap_socket(raw, server_hostname=self.url.hostname)
            except BaseException:
                raw.close()
                raise
            body = json.dumps(command, allow_nan=False).encode()
            if len(body) > 16384:
                raise ValueError("Bounded control command required")
            connection.request(
                "POST",
                self.url.path or "/",
                body=body,
                headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json"},
            )
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError("Successful non-redirecting control response required")
            result = response.read(16385)
            if len(result) > 16384:
                raise ValueError("Bounded control receipt required")
            result = json.loads(result)
            self.guard()
            return result
        except Exception:  # noqa: BLE001 -- never expose tokens or replay a control mutation
            raise RuntimeError("Private HTTPS control outcome requires reconciliation") from None
        finally:
            connection.close()
