"""Bounded, public GitHub source snapshots for Sky deployments."""

from __future__ import annotations

import os
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from application.deployment_core import MAX_UPLOAD

OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
COMMIT = re.compile(r"[a-f0-9]{40,64}\Z")
REF = re.compile(r"[A-Za-z0-9_./-]{1,100}\Z")


@dataclass(frozen=True)
class GitHubRepository:
    owner: str
    name: str

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.name}"


def parse_repository_url(raw: str) -> GitHubRepository:
    if not isinstance(raw, str) or len(raw) > 250:
        raise ValueError("공개 GitHub 저장소 URL을 입력하세요.")
    parts = urlsplit(raw.strip())
    if (
        parts.scheme != "https"
        or parts.netloc.lower() != "github.com"
        or parts.username
        or parts.password
        or parts.port
        or parts.query
        or parts.fragment
    ):
        raise ValueError("https://github.com/소유자/저장소 형식만 지원합니다.")
    path = parts.path.rstrip("/").split("/")
    if len(path) != 3 or path[0] != "":
        raise ValueError("저장소의 첫 화면 URL을 입력하세요. 파일·브랜치 링크는 지원하지 않습니다.")
    owner, name = path[1], path[2].removesuffix(".git")
    if not OWNER.fullmatch(owner) or not REPOSITORY.fullmatch(name) or name in {".", ".."}:
        raise ValueError("GitHub 저장소 주소가 올바르지 않습니다.")
    return GitHubRepository(owner, name)


def validate_branch(branch: str | None) -> str | None:
    if branch is None or branch == "":
        return None
    if (
        not isinstance(branch, str)
        or not REF.fullmatch(branch)
        or branch.startswith(("/", ".", "-"))
        or branch.endswith(("/", "."))
        or "//" in branch
        or ".." in branch
        or branch.endswith(".lock")
        or any(part.startswith(".") for part in branch.split("/"))
    ):
        raise ValueError("브랜치 이름이 올바르지 않습니다.")
    return branch


def resolve_revision(repository: GitHubRepository, branch: str | None) -> tuple[str, str]:
    branch = validate_branch(branch)
    default_branch = branch is None
    command = ["git", "-c", "credential.helper=", "ls-remote", "--symref", repository.url + ".git"]
    command.extend(["refs/heads/" + branch] if branch else ["HEAD"])
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ASKPASS": os.devnull,
    }
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=25, env=environment, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("GitHub 브랜치를 조회하지 못했습니다. 네트워크와 Git 설치를 확인하세요.") from None
    if result.returncode or len(result.stdout) > 4096:
        raise ValueError("공개 GitHub 저장소에 접근하지 못했습니다. 주소와 공개 설정을 확인하세요.")
    lines = result.stdout.splitlines()
    if not branch:
        symbols = [
            line.split("\t", 1)[0].removeprefix("ref: refs/heads/")
            for line in lines
            if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD")
        ]
        if len(symbols) != 1:
            raise ValueError("기본 브랜치를 확인하지 못했습니다. 브랜치를 직접 입력하세요.")
        branch = validate_branch(symbols[0])
    reference = "HEAD" if default_branch else "refs/heads/" + branch
    matches = [
        line.split("\t", 1)[0]
        for line in lines
        if line.endswith("\t" + reference) and COMMIT.fullmatch(line.split("\t", 1)[0])
    ]
    if not branch or len(matches) != 1 or not COMMIT.fullmatch(matches[0]):
        raise ValueError("GitHub 브랜치의 커밋을 확인하지 못했습니다.")
    return branch, matches[0]


class GitHubRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        parts = urlsplit(newurl)
        if parts.scheme != "https" or parts.hostname not in {"github.com", "codeload.github.com"}:
            raise ValueError("GitHub 아카이브의 이동 주소가 안전하지 않습니다.")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def download_revision(repository: GitHubRepository, commit: str, destination: Path) -> None:
    if not COMMIT.fullmatch(commit):
        raise ValueError("확인되지 않은 커밋은 다운로드할 수 없습니다.")
    url = repository.url + "/archive/" + commit + ".zip"
    request = urllib.request.Request(url, headers={"User-Agent": "Sky-Deployment/1"})
    opener = urllib.request.build_opener(GitHubRedirect())
    try:
        with opener.open(request, timeout=45) as response:
            if int(response.headers.get("Content-Length", "0")) > MAX_UPLOAD:
                raise ValueError("GitHub 소스 ZIP이 20 MiB 제한을 초과했습니다.")
            with destination.open("wb") as output:
                remaining = MAX_UPLOAD + 1
                while remaining:
                    chunk = response.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    output.write(chunk)
                    remaining -= len(chunk)
                if remaining == 0:
                    raise ValueError("GitHub 소스 ZIP이 20 MiB 제한을 초과했습니다.")
    except urllib.error.HTTPError as exc:
        destination.unlink(missing_ok=True)
        if exc.code in {403, 404}:
            raise ValueError(
                "공개 GitHub 소스를 다운로드하지 못했습니다. 공개 설정과 커밋을 확인하세요."
            ) from None
        raise ValueError(f"GitHub 소스 다운로드가 실패했습니다 (HTTP {exc.code}).") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        destination.unlink(missing_ok=True)
        raise ValueError("GitHub 소스를 다운로드하지 못했습니다. 네트워크를 확인하세요.") from None
