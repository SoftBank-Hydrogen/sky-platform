"""Propose a verified ECR image through one narrowly scoped infrastructure PR."""

import base64
import json
import os
import re
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

INFRA_REPO = "SoftBank-Hydrogen/sky-infra"
SOURCE_REPO = "SoftBank-Hydrogen/sky-platform"
IMAGE_FILE = "terraform/envs/dev/platform-image.auto.tfvars"
TAG_LINE = re.compile(r'^(platform_image_tag\s*=\s*")[0-9a-f]{7}(".*)$', re.MULTILINE)


class ApiError(RuntimeError):
    def __init__(self, status: int):
        super().__init__(
            f"GitHub API request failed (HTTP {status}); check token permissions and repository access"
        )
        self.status = status


class GitHub:
    def __init__(self, token: str):
        self.token = token

    def request(self, method: str, path: str, data: dict | None = None):
        request = Request(
            "https://api.github.com/" + path,
            data=json.dumps(data).encode() if data is not None else None,
            method=method,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "sky-platform-image-pr",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            raise ApiError(exc.code) from None


def update_tag(content: str, tag: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{7}", tag):
        raise ValueError("Image tag must be a 7-character lowercase commit SHA")
    if len(TAG_LINE.findall(content)) != 1:
        raise ValueError("Expected exactly one platform_image_tag assignment")
    return TAG_LINE.sub(lambda match: match[1] + tag + match[2], content)


def read_file(api, ref: str):
    result = api.request("GET", f"repos/{INFRA_REPO}/contents/{IMAGE_FILE}?" + urlencode({"ref": ref}))
    if result.get("encoding") != "base64" or result.get("type") != "file":
        raise ValueError("Unexpected infrastructure image file")
    return result, base64.b64decode(result["content"]).decode("utf-8")


def check_diff(api, branch: str):
    result = api.request("GET", f"repos/{INFRA_REPO}/compare/main...{branch}")
    files = result.get("files")
    if (
        not isinstance(files, list)
        or len(files) > 1
        or any(file.get("filename") != IMAGE_FILE or file.get("status") != "modified" for file in files)
    ):
        raise ValueError("Refusing an infrastructure branch with unrelated changes")


def propose(api, sha: str, image: str, run_url: str, source_api=None) -> str:
    source_api = source_api or api
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("SOURCE_SHA must be the full lowercase commit SHA")
    tag = sha[:7]
    if not re.fullmatch(rf"[0-9]{{12}}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/sky-platform:{tag}", image):
        raise ValueError("Expected the verified sky-platform ECR image and matching SHA tag")
    if not re.fullmatch(r"https://github\.com/SoftBank-Hydrogen/sky-platform/actions/runs/[0-9]+", run_url):
        raise ValueError("Invalid workflow run URL")
    source = source_api.request("GET", f"repos/{SOURCE_REPO}/git/ref/heads/main")
    if source["object"]["sha"] != sha:
        return "Skipped: this source commit is no longer the latest main commit."
    _, main_content = read_file(api, "main")
    desired = update_tag(main_content, tag)
    if desired == main_content:
        return "No PR needed: infrastructure already selects this image tag."

    branch = "deploy/platform-" + sha
    try:
        api.request("GET", f"repos/{INFRA_REPO}/git/ref/heads/{branch}")
    except ApiError as exc:
        if exc.status != 404:
            raise
        main = api.request("GET", f"repos/{INFRA_REPO}/git/ref/heads/main")
        try:
            api.request(
                "POST",
                f"repos/{INFRA_REPO}/git/refs",
                {"ref": "refs/heads/" + branch, "sha": main["object"]["sha"]},
            )
        except ApiError as create_error:
            if create_error.status != 422:
                raise
            api.request("GET", f"repos/{INFRA_REPO}/git/ref/heads/{branch}")
    check_diff(api, branch)
    pulls = api.request(
        "GET",
        f"repos/{INFRA_REPO}/pulls?"
        + urlencode({"state": "all", "head": "SoftBank-Hydrogen:" + branch, "base": "main", "per_page": 100}),
    )
    existing = next((pull for pull in pulls if pull["state"] == "open"), None)
    if pulls and existing is None:
        raise ValueError("This release PR was already closed; manual review is required before re-proposing")

    branch_file, branch_content = read_file(api, branch)
    updated = update_tag(branch_content, tag)
    if update_tag(branch_content, "0000000") != update_tag(main_content, "0000000"):
        raise ValueError("Image branch contents differ from main beyond the image tag")
    if updated != branch_content:
        api.request(
            "PUT",
            f"repos/{INFRA_REPO}/contents/{IMAGE_FILE}",
            {
                "message": f"Deploy sky-platform image {tag}",
                "content": base64.b64encode(updated.encode()).decode(),
                "sha": branch_file["sha"],
                "branch": branch,
            },
        )
    check_diff(api, branch)
    if existing:
        return "Existing infrastructure PR: " + existing["html_url"]
    if source_api.request("GET", f"repos/{SOURCE_REPO}/git/ref/heads/main")["object"]["sha"] != sha:
        return "Skipped: main advanced during preparation; no PR was opened."
    pull = api.request(
        "POST",
        f"repos/{INFRA_REPO}/pulls",
        {
            "title": f"Deploy sky-platform image {tag}",
            "head": branch,
            "base": "main",
            "body": (
                f"Select the verified Sky API/worker image published to ECR.\n\n"
                f"- Source commit: https://github.com/{SOURCE_REPO}/commit/{sha}\n"
                f"- Image: \u0060{image}\u0060\n"
                f"- Build/test/publish run: {run_url}\n\n"
                "Only the platform image tag changes. Merge after checking the infrastructure plan, "
                "release readiness, and whether a newer release supersedes this one. "
                "Merging invokes the existing infrastructure deployment workflow; this PR is not auto-merged."
            ),
        },
    )
    return "Created infrastructure PR: " + pull["html_url"]


def main():
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        raise ValueError("Set SKY_INFRA_PR_TOKEN in the sky-platform dev environment secrets")
    source_token = os.environ.get("SOURCE_GITHUB_TOKEN", "")
    if not source_token:
        raise ValueError("SOURCE_GITHUB_TOKEN is required to read the platform main ref")
    result = propose(
        GitHub(token),
        os.environ.get("SOURCE_SHA", ""),
        os.environ.get("SERVICE_IMAGE", ""),
        os.environ.get("SOURCE_RUN_URL", ""),
        GitHub(source_token),
    )
    print(result)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(result + "\n")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, KeyError, OSError) as error:
        print(f"Infrastructure PR proposal failed: {error}", file=sys.stderr)
        sys.exit(1)
