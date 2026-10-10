"""Offline release proposal tests: no GitHub writes or AWS requests."""

import base64
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts.propose_infra_image import IMAGE_FILE, INFRA_REPO, SOURCE_REPO, ApiError, propose, update_tag

SHA = "abcdef0" + "1" * 33
IMAGE = "265233844540.dkr.ecr.ap-northeast-2.amazonaws.com/sky-platform:abcdef0"
RUN = "https://github.com/SoftBank-Hydrogen/sky-platform/actions/runs/123"
CONTENT = '# image tag\nplatform_image_tag = "0000000"\n'


class FakeGitHub:
    def __init__(self):
        self.source_sha = SHA
        self.files = {"main": CONTENT}
        self.pulls = []
        self.writes = []
        self.unrelated = False
        self.advance = False

    def request(self, method, path, data=None):
        if method != "GET":
            self.writes.append((method, path, data))
        route = urlsplit(path)
        query = parse_qs(route.query)
        if route.path == f"repos/{SOURCE_REPO}/git/ref/heads/main":
            return {"object": {"sha": self.source_sha}}
        if "/compare/main..." in path:
            branch = path.split("...")[-1]
            return {
                "files": (
                    [{"filename": "main.tf", "status": "modified"}]
                    if self.unrelated
                    else (
                        [{"filename": IMAGE_FILE, "status": "modified"}]
                        if self.files[branch] != self.files["main"]
                        else []
                    )
                )
            }
        if "/git/ref/heads/" in path:
            branch = path.split("/git/ref/heads/")[-1]
            if branch not in self.files:
                raise ApiError(404)
            return {"object": {"sha": "b" * 40}}
        if path == f"repos/{INFRA_REPO}/git/refs":
            self.files[data["ref"].removeprefix("refs/heads/")] = self.files["main"]
            return {}
        if "/contents/" in path:
            if method == "PUT":
                assert data["sha"] == "blob-sha"
                self.files[data["branch"]] = base64.b64decode(data["content"]).decode()
                if self.advance:
                    self.source_sha = "2" * 40
                return {}
            content = self.files[query["ref"][0]]
            return {
                "type": "file",
                "encoding": "base64",
                "sha": "blob-sha",
                "content": base64.b64encode(content.encode()).decode(),
            }
        if route.path == f"repos/{INFRA_REPO}/pulls":
            if method == "POST":
                pull = {
                    **data,
                    "state": "open",
                    "html_url": "https://github.com/SoftBank-Hydrogen/sky-infra/pull/3",
                }
                self.pulls.append(pull)
                return pull
            return self.pulls
        raise AssertionError((method, path))


def test_release_creates_only_image_change_and_reuses_existing_pr():
    api = FakeGitHub()
    result = propose(api, SHA, IMAGE, RUN)
    assert "Created infrastructure PR" in result
    branch = "deploy/platform-" + SHA
    assert api.files[branch] == CONTENT.replace("0000000", "abcdef0")
    assert api.files["main"] == CONTENT
    assert api.pulls[0]["base"] == "main"
    assert SHA in api.pulls[0]["body"]
    before = len(api.writes)
    assert "Existing infrastructure PR" in propose(api, SHA, IMAGE, RUN)
    assert len(api.writes) == before


def test_current_tag_is_a_noop():
    api = FakeGitHub()
    api.files["main"] = CONTENT.replace("0000000", "abcdef0")
    assert "No PR needed" in propose(api, SHA, IMAGE, RUN)
    assert not api.writes


def test_stale_source_does_not_modify_infra():
    api = FakeGitHub()
    api.source_sha = "2" * 40
    assert "Skipped" in propose(api, SHA, IMAGE, RUN)
    assert not api.writes


def test_main_advances_during_preparation_no_pr_is_created():
    api = FakeGitHub()
    api.advance = True
    assert "Skipped" in propose(api, SHA, IMAGE, RUN)
    assert not api.pulls


def test_reject_unrelated_changes_before_writing_image():
    api = FakeGitHub()
    api.files["deploy/platform-" + SHA] = CONTENT
    api.unrelated = True
    with pytest.raises(ValueError, match="unrelated"):
        propose(api, SHA, IMAGE, RUN)
    assert not api.writes


def test_closed_pr_is_not_reopened_or_replaced():
    api = FakeGitHub()
    propose(api, SHA, IMAGE, RUN)
    api.pulls[0]["state"] = "closed"
    before = len(api.writes)
    with pytest.raises(ValueError, match="already closed"):
        propose(api, SHA, IMAGE, RUN)
    assert len(api.writes) == before


@pytest.mark.parametrize("content", ["other = 1", CONTENT + CONTENT, 'platform_image_tag = "latest"'])
def test_malformed_tfvars_is_rejected(content):
    with pytest.raises(ValueError, match="exactly one"):
        update_tag(content, "abcdef0")


@pytest.mark.parametrize(
    "sha,image,run",
    [
        ("short", IMAGE, RUN),
        (SHA, IMAGE.replace("abcdef0", "1234567"), RUN),
        (SHA, IMAGE.replace("sky-platform", "other"), RUN),
        (SHA, IMAGE, "https://example.com/run"),
    ],
)
def test_invalid_release_inputs_cannot_write(sha, image, run):
    api = FakeGitHub()
    with pytest.raises(ValueError):
        propose(api, sha, image, run)
    assert not api.writes


def test_source_read_uses_separate_read_only_client():
    infra = FakeGitHub()

    class SourceClient:
        def request(self, method, path, data=None):
            assert method == "GET"
            assert path == f"repos/{SOURCE_REPO}/git/ref/heads/main"
            return {"object": {"sha": "2" * 40}}

    assert "Skipped" in propose(infra, SHA, IMAGE, RUN, SourceClient())
    assert not infra.writes
