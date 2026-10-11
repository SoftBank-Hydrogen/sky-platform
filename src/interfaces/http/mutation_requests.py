"""Small same-origin JSON boundary shared by explicit hosted mutation APIs."""

import json
import re
from urllib.parse import urlsplit


def validate_origin(origin):
    try:
        url = urlsplit(origin)
        valid = (
            url.scheme in {"https", "http"}
            and url.netloc
            and not url.username
            and not url.password
            and not url.path
            and not url.query
            and not url.fragment
            and origin == f"{url.scheme}://{url.netloc}"
            and url.hostname
            and (url.scheme == "https" or url.hostname in {"localhost", "127.0.0.1", "::1"})
            and url.port != 0
            and not any(ord(char) <= 32 for char in origin)
        )
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ValueError("Invalid hosted application origin")
    return origin


def read_json_object(handler, *, maximum=1024):
    if (
        handler.headers.get_all("Content-Type") != ["application/json"]
        or handler.headers.get_all("Transfer-Encoding") is not None
    ):
        raise ValueError("Invalid JSON framing")
    lengths = handler.headers.get_all("Content-Length")
    if (
        lengths is None
        or len(lengths) != 1
        or not re.fullmatch(r"[0-9]{1,4}", lengths[0])
        or not 2 <= int(lengths[0]) <= maximum
    ):
        raise ValueError("Invalid JSON size")
    handler.connection.settimeout(5)
    raw = handler.rfile.read(int(lengths[0]))
    if len(raw) != int(lengths[0]):
        raise ValueError("Incomplete JSON body")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError("Invalid JSON constant")

    body = json.loads(raw, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    if not isinstance(body, dict):
        raise ValueError("JSON object required")  # noqa: TRY004 -- malformed request contract
    return body
