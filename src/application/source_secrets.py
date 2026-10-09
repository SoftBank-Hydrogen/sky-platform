"""Prevent supplied credentials from becoming part of an application image."""

from __future__ import annotations

import re
from pathlib import Path


_SENSITIVE_ENV_NAME = re.compile(
    r"(?:^|_)(?:PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|APIKEY|PRIVATE_KEY|ACCESS_KEY|CREDENTIALS?|DATABASE_URL|DB_URL|REDIS_URL|MONGODB?_URI|DSN)(?:_|$)"
)
_SENSITIVE_ENV_EXACT = {"PGPASSWORD", "MYSQL_PWD"}


def reject_supplied_secrets_in_source(project: Path, environment: dict[str, str]) -> None:
    """Reject a supplied credential value found anywhere in the deployment source."""
    secrets = [value.encode() for name, value in environment.items()
               if value and (name in _SENSITIVE_ENV_EXACT or _SENSITIVE_ENV_NAME.search(name))]
    if not secrets:
        return
    for path in project.rglob("*"):
        if path.is_symlink():
            raise ValueError("CV-07: Cannot inspect a symbolic link in the deployment source")
        if not path.is_file():
            continue
        try:
            content = path.read_bytes()
        except OSError:
            raise ValueError("CV-07: Cannot inspect every deployment source file") from None
        if any(value in content for value in secrets):
            raise ValueError("CV-07: A supplied credential value appears in deployment source")
