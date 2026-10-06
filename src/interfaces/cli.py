"""Command-line entry point for the Sky control plane."""

import os
import re
import stat
from pathlib import Path

from interfaces.http.server import serve

ENV_NAME = re.compile(r"(?:OPENAI_API_KEY|SKY_[A-Z][A-Z0-9_]*)\Z")
MAX_ENV_BYTES = 64 * 1024


def load_local_env(path: Path) -> None:
    """Load a private local .env; explicit process environment always takes precedence."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        if path.is_symlink():
            raise ValueError(".env는 심볼릭 링크가 아닌 일반 파일이어야 합니다.") from None
        return
    except OSError as exc:
        raise ValueError(".env는 심볼릭 링크가 아닌 일반 파일이어야 합니다.") from exc
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(".env는 심볼릭 링크가 아닌 일반 파일이어야 합니다.")
        if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ValueError(".env 권한을 chmod 600으로 제한하세요.")
        content_bytes = source.read(MAX_ENV_BYTES + 1)
    if len(content_bytes) > MAX_ENV_BYTES:
        raise ValueError(".env 파일이 64 KiB 제한을 초과했습니다.")
    try:
        content = content_bytes.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError(".env는 UTF-8 파일이어야 합니다.") from exc
    values = {}
    for line_number, raw in enumerate(content.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        name, separator, value = line.partition("=")
        name = name.strip()
        value = value.strip()
        if not separator or not ENV_NAME.fullmatch(name):
            raise ValueError(f".env {line_number}행의 변수 이름이 올바르지 않습니다.")
        if value.startswith(('"', "'")):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f".env {line_number}행의 따옴표가 닫히지 않았습니다.")
            value = value[1:-1]
        elif any(character.isspace() for character in value):
            raise ValueError(f".env {line_number}행의 값에 공백이 있습니다. 따옴표로 묶으세요.")
        if "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError(f".env {line_number}행에 사용할 수 없는 문자가 있습니다.")
        if name in values:
            raise ValueError(f".env {line_number}행에 중복된 변수가 있습니다.")
        values[name] = value
    for name, value in values.items():
        os.environ.setdefault(name, value)


def main() -> None:
    load_local_env(Path.cwd() / ".env")
    serve(product_name="Sky", default_state_dir=".sky")
