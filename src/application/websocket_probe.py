"""Bounded WebSocket round trip for the game-owned sky.probe contract."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
from datetime import datetime, timezone
from urllib.parse import urlsplit

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_HEADER = 8192
_MAX_FRAME = 4096


class WebSocketProbeError(ValueError):
    pass


def _frame(opcode: int, data: bytes) -> bytes:
    if len(data) > 125:
        raise WebSocketProbeError("프로브 메시지가 너무 깁니다.")
    mask = os.urandom(4)
    return (
        bytes((0x80 | opcode, 0x80 | len(data)))
        + mask
        + bytes(value ^ mask[index % 4] for index, value in enumerate(data))
    )


def _read_frame(source) -> tuple[int, bytes]:
    header = source.read(2)
    if len(header) != 2:
        raise WebSocketProbeError("WebSocket 응답이 중간에 끊겼습니다.")
    if header[0] & 0x70 or header[1] & 0x80:
        raise WebSocketProbeError("WebSocket 응답 프레임 형식이 올바르지 않습니다.")
    opcode = header[0] & 0x0F
    length = header[1] & 0x7F
    if length == 126:
        raw = source.read(2)
        if len(raw) != 2:
            raise WebSocketProbeError("WebSocket 응답 길이가 누락됐습니다.")
        length = int.from_bytes(raw)
    elif length == 127:
        raw = source.read(8)
        if len(raw) != 8:
            raise WebSocketProbeError("WebSocket 응답 길이가 누락됐습니다.")
        length = int.from_bytes(raw)
    if length > _MAX_FRAME:
        raise WebSocketProbeError("WebSocket 응답이 허용 크기를 초과했습니다.")
    if not header[0] & 0x80 or opcode not in {1, 8, 9, 10}:
        raise WebSocketProbeError("WebSocket 응답 프레임을 해석할 수 없습니다.")
    payload = source.read(length)
    if len(payload) != length:
        raise WebSocketProbeError("WebSocket 응답 본문이 누락됐습니다.")
    return opcode, payload


def probe_sky_game(base_url: str, *, timeout: float = 5) -> dict:
    """Verify the actual /ws ingress without joining a room or changing scores.

    The caller must first validate ownership and current health of the stored
    deployment URL. This function never follows redirects or accepts credentials.
    """
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise WebSocketProbeError("검증할 배포 URL이 올바르지 않습니다.")
    if parsed.scheme == "http" and parsed.hostname != "127.0.0.1":
        raise WebSocketProbeError("TLS 없는 WebSocket 검사는 로컬 배포만 허용합니다.")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise WebSocketProbeError("검증할 배포 포트가 올바르지 않습니다.") from None
    host = parsed.netloc
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    nonce = os.urandom(16).hex()
    expected_accept = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode("ascii")
    request = (
        f"GET /ws HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
        f"Origin: {parsed.scheme}://{host}\r\n\r\n"
    ).encode("ascii")
    try:
        with socket.create_connection((parsed.hostname, port), timeout=timeout) as raw:
            raw.settimeout(timeout)
            connection = (
                ssl.create_default_context().wrap_socket(raw, server_hostname=parsed.hostname)
                if parsed.scheme == "https"
                else raw
            )
            with connection:
                source = connection.makefile("rb")
                connection.sendall(request)
                status = source.readline(_MAX_HEADER + 1)
                if len(status) > _MAX_HEADER or not status.startswith(b"HTTP/1.1 101 "):
                    raise WebSocketProbeError("WebSocket 연결 승격이 실패했습니다.")
                headers = {}
                total = len(status)
                while True:
                    line = source.readline(_MAX_HEADER - total + 1)
                    total += len(line)
                    if not line or total > _MAX_HEADER:
                        raise WebSocketProbeError("WebSocket 연결 응답 헤더가 올바르지 않습니다.")
                    if line == b"\r\n":
                        break
                    name, separator, value = line.partition(b":")
                    if not separator:
                        raise WebSocketProbeError("WebSocket 연결 응답 헤더가 올바르지 않습니다.")
                    headers[name.strip().lower()] = value.strip()
                connection_tokens = [
                    token.strip().lower() for token in headers.get(b"connection", b"").split(b",")
                ]
                if (
                    headers.get(b"upgrade", b"").lower() != b"websocket"
                    or b"upgrade" not in connection_tokens
                    or headers.get(b"sec-websocket-accept") != expected_accept.encode()
                ):
                    raise WebSocketProbeError("WebSocket 연결 응답을 인증할 수 없습니다.")
                connection.sendall(_frame(1, json.dumps({"type": "sky.probe", "nonce": nonce}).encode()))
                for _ in range(8):
                    opcode, payload = _read_frame(source)
                    if opcode == 9:
                        connection.sendall(_frame(10, payload))
                    elif opcode == 8:
                        raise WebSocketProbeError("WebSocket이 확인 응답 전에 종료됐습니다.")
                    elif opcode == 1:
                        try:
                            message = json.loads(payload)
                        except (UnicodeError, ValueError):
                            raise WebSocketProbeError("WebSocket 확인 응답이 JSON이 아닙니다.") from None
                        if (
                            isinstance(message, dict)
                            and message.get("type") == "sky.probe.ack"
                            and message.get("nonce") == nonce
                        ):
                            connection.sendall(_frame(8, b""))
                            return {
                                "status": "passed",
                                "protocol": "sky.probe.v1",
                                "checked_at": datetime.now(timezone.utc).isoformat(),
                                "endpoint": base_url.rstrip("/") + "/ws",
                            }
                raise WebSocketProbeError("WebSocket 확인 응답을 받지 못했습니다.")
    except (OSError, ssl.SSLError, TimeoutError) as exc:
        raise WebSocketProbeError("WebSocket 연결 또는 응답을 확인할 수 없습니다.") from exc
