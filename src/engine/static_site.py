"""Conservative source evidence for a future static-file deployment target.

This is a classification, not permission to publish. In particular, a client
bundle can depend on a same-origin API that is invisible to a file inventory.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from engine.compatibility import InfrastructureProfile

SERVER_ENTRIES = (
    "Dockerfile", "server.js", "server.cjs", "server.mjs", "server.py",
    "app.py", "main.py", "manage.py", "Procfile", "requirements.txt",
    "Gemfile", "go.mod",
)
SERVER_DIRECTORIES = ("api", "functions", "pages/api", "app/api")
SERVER_DEPENDENCIES = {
    "express", "fastify", "hono", "next", "@nestjs/core", "ws", "socket.io",
    "pg", "postgres", "better-sqlite3", "sqlite3", "prisma", "@prisma/client",
}
STATIC_ASSET_SUFFIXES = {
    ".html", ".htm", ".css", ".js", ".mjs", ".json", ".svg", ".png",
    ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".woff", ".woff2",
    ".ttf", ".wasm", ".txt", ".map", ".webmanifest", ".avif",
}
SAME_ORIGIN_API = re.compile(
    r"(?:fetch\s*\(|(?:new\s+)?WebSocket\s*\()\s*"
    r"[\x27\x22`]\s*(?:/api(?:/|\?|[\x27\x22`])|/ws(?:/|\?|[\x27\x22`]))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class StaticSiteAssessment:
    status: str
    evidence_files: tuple[str, ...]
    reasons: tuple[str, ...]

    def as_dict(self) -> dict:
        return asdict(self)


def assess_static_site(project: Path, profile: InfrastructureProfile) -> StaticSiteAssessment:
    """Identify only immediately publishable files; never infer a backend away."""
    evidence: set[str] = set()
    reasons: list[str] = []
    server_markers = False
    for name in SERVER_ENTRIES:
        if (project / name).is_file():
            server_markers = True
            evidence.add(name)
            reasons.append(f"서버 실행 또는 빌드 경계 확인 필요: {name}")
    for name in SERVER_DIRECTORIES:
        if (project / name).is_dir():
            server_markers = True
            evidence.add(name + "/")
            reasons.append(f"API 또는 함수 디렉터리 확인 필요: {name}/")
    for path in project.rglob("*"):
        if not path.is_file() or path.parent == project:
            continue
        relative = path.relative_to(project).as_posix()
        if path.name in SERVER_ENTRIES or any(
            relative.startswith(name + "/") for name in SERVER_DIRECTORIES
        ):
            server_markers = True
            evidence.add(relative)
            reasons.append(f"중첩 서버 파일 또는 API 경로 확인 필요: {relative}")
    package = project / "package.json"
    if package.is_file():
        evidence.add("package.json")
        try:
            raw = package.read_bytes()[:1024 * 1024 + 1]
            if len(raw) > 1024 * 1024:
                raise ValueError("Oversized package.json")
            data = json.loads(raw)
        except (OSError, UnicodeError, ValueError):
            return StaticSiteAssessment("unknown", tuple(sorted(evidence)), ("package.json을 해석하지 못했습니다.",))
        if not isinstance(data, dict):
            return StaticSiteAssessment("unknown", tuple(sorted(evidence)), ("package.json이 객체가 아닙니다.",))
        dependencies = {name for field in ("dependencies", "devDependencies")
                        if isinstance(data.get(field), dict) for name in data[field]}
        if dependencies & SERVER_DEPENDENCIES:
            server_markers = True
            reasons.append("서버 또는 데이터베이스 의존성이 있습니다.")
        if isinstance(data.get("scripts"), dict) and data["scripts"].get("build"):
            reasons.append("빌드 결과물을 확인해야 합니다.")
    if profile.requirements:
        reasons.append("실행·데이터 요구 신호: " + ", ".join(profile.requirements))
        evidence.update(profile.evidence)
        if any(name in profile.requirements for name in ("sqlite", "database", "background-worker", "local-files")):
            server_markers = True
    if server_markers:
        return StaticSiteAssessment("server_or_mixed", tuple(sorted(evidence)), tuple(reasons))
    if package.is_file():
        return StaticSiteAssessment("needs_build", tuple(sorted(evidence)), tuple(reasons or ("빌드 산출물 검증이 필요합니다.",)))
    index = project / "index.html"
    if not index.is_file():
        return StaticSiteAssessment("unknown", tuple(sorted(evidence)), ("배포할 루트 index.html이 없습니다.",))
    evidence.add("index.html")
    for path in project.rglob("*"):
        if path.is_file() and (path.suffix.lower() not in STATIC_ASSET_SUFFIXES
                               or any(part.startswith(".") for part in path.relative_to(project).parts)):
            evidence.add(path.relative_to(project).as_posix())
            return StaticSiteAssessment("needs_review", tuple(sorted(evidence)),
                                        ("정적 공개 파일로 확인되지 않은 파일 또는 숨김 파일이 있습니다.",))
        if path.is_file() and path.suffix.lower() in {".js", ".mjs", ".html"}:
            try:
                content = path.read_bytes()[:1024 * 1024 + 1]
                if len(content) > 1024 * 1024:
                    return StaticSiteAssessment("needs_review", tuple(sorted(evidence)),
                                                ("큰 스크립트의 API 의존성을 확인하지 못했습니다.",))
                if SAME_ORIGIN_API.search(content.decode("utf-8")):
                    evidence.add(path.relative_to(project).as_posix())
                    return StaticSiteAssessment("needs_review", tuple(sorted(evidence)),
                                                ("같은 주소의 API/WebSocket 호출이 있어 정적 파일만으로 동작하는지 확인해야 합니다.",))
            except (OSError, UnicodeError):
                return StaticSiteAssessment("needs_review", tuple(sorted(evidence)),
                                            ("스크립트의 API 의존성을 확인하지 못했습니다.",))
    if reasons:
        return StaticSiteAssessment("needs_review", tuple(sorted(evidence)), tuple(reasons))
    return StaticSiteAssessment("eligible", tuple(sorted(evidence)),
                                ("루트 index.html과 정적 파일만 확인했습니다. 배포 후 브라우저 동작은 별도 검증해야 합니다.",))
