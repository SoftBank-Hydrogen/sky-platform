"""Fail closed when the service's initialized persistent state is missing."""

import json
import os
import re
import stat
import uuid
from pathlib import Path

MARKER_NAME = ".sky-service-state.json"


def state_root(root: Path) -> Path:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("state directory is missing or is a symlink; check the persistent volume mount")
    return root.resolve(strict=True)


def initialize_service_state(root: Path) -> Path:
    root = state_root(root)
    if any(root.iterdir()):
        raise ValueError("state initialization requires an empty directory; existing state is never overwritten")
    marker = root / MARKER_NAME
    descriptor = os.open(
        marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump({"kind": "sky-service-state", "version": 1, "id": uuid.uuid4().hex}, output)
        output.flush()
        os.fsync(output.fileno())
    return root


def require_service_state(root: Path) -> Path:
    root = state_root(root)
    marker = root / MARKER_NAME
    try:
        descriptor = os.open(marker, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "r", encoding="utf-8") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("state marker must be a regular file")
            content = source.read(4097)
            if len(content) > 4096:
                raise ValueError("state marker exceeds the size limit")
            record = json.loads(content)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("state marker is missing or invalid; restore the mounted state volume") from error
    if (
        not isinstance(record, dict)
        or record.get("kind") != "sky-service-state"
        or type(record.get("version")) is not int
        or record["version"] != 1
        or not isinstance(record.get("id"), str)
        or re.fullmatch(r"[0-9a-f]{32}", record["id"]) is None
    ):
        raise ValueError("state marker is invalid; restore the mounted state volume")
    return root
