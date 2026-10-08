"""Import public GitHub commits and watch opted-in branches for new revisions."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from adapters.local.docker import LocalDockerAdapter
from application.deployment_core import extract_project, source_digest
from application.github_source import (
    GitHubRepository,
    download_revision,
    parse_repository_url,
    resolve_revision,
    validate_branch,
)
from application.infrastructure import (
    explicit_infrastructure_plan,
    infrastructure_compatibility,
    inspect_infrastructure,
    plan_infrastructure,
    validate_infrastructure,
)

TARGETS = {"auto", "local-docker", "aws-ecs-express", "cloud-run"}


class GitHubDeploymentsMixin:
    def restore_github_sources(self):
        self.github_sources = {}
        self.github_polling = set()
        path = self.root / "github-sources.json"
        if not path.exists():
            return
        try:
            if path.is_symlink() or path.stat().st_size > 65536:
                raise ValueError("unsafe source record")
            records = json.loads(path.read_text())
            if not isinstance(records, list) or len(records) > 20:
                raise ValueError("invalid source records")
            for item in records:
                if not isinstance(item, dict):
                    raise ValueError("invalid source record")
                repository = parse_repository_url(item["repository_url"])
                branch = validate_branch(item["branch"])
                if (
                    not re.fullmatch(r"[a-f0-9]{16}", item["id"])
                    or not branch
                    or not isinstance(item["enabled"], bool)
                    or not re.fullmatch(r"[a-z][a-z0-9-]{2,30}", item["application_id"])
                    or not isinstance(item["targets"], list)
                    or not 1 <= len(item["targets"]) <= 3
                    or len(set(item["targets"])) != len(item["targets"])
                    or any(target not in TARGETS for target in item["targets"])
                    or ("auto" in item["targets"] and len(item["targets"]) != 1)
                    or type(item["public"]) is not bool
                    or not re.fullmatch(r"[a-f0-9]{40,64}", item["last_revision"])
                    or not isinstance(item["last_job_ids"], list)
                    or not 1 <= len(item["last_job_ids"]) <= 3
                    or any(
                        not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{16}", job_id)
                        for job_id in item["last_job_ids"]
                    )
                    or (item.get("last_error") is not None and not isinstance(item["last_error"], str))
                ):
                    raise ValueError("invalid source record")
                item["repository_url"] = repository.url
                self.github_sources[item["id"]] = item
            if len(self.github_sources) != len(records):
                raise ValueError("duplicate source records")
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            self.github_sources = {}
            self.recovery_warnings.append("GitHub 자동 배포 설정을 읽지 못해 자동 확인을 중단했습니다.")

    def save_github_sources(self):
        path = self.root / "github-sources.json"
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=".github-sources-", suffix=".tmp", dir=self.root)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(list(self.github_sources.values()), output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def github_source_summaries(self):
        with self.lock:
            summaries = []
            for item in self.github_sources.values():
                summary = dict(item)
                statuses = [
                    self.jobs[job_id]["status"] if job_id in self.jobs else "missing"
                    for job_id in item["last_job_ids"]
                ]
                if all(status == "succeeded" for status in statuses):
                    summary["last_deployment_status"] = "succeeded"
                elif any(status in {"failed", "cancelled", "interrupted"} for status in statuses):
                    summary["last_deployment_status"] = "failed"
                elif any(status == "missing" for status in statuses):
                    summary["last_deployment_status"] = "unknown"
                else:
                    summary["last_deployment_status"] = "running"
                summary["retryable"] = summary["last_deployment_status"] == "failed" and all(
                    status in {"succeeded", "failed", "cancelled", "interrupted"} for status in statuses
                )
                summaries.append(summary)
            return summaries

    def set_github_source_enabled(self, source_id: str, enabled: bool):
        with self.lock:
            item = self.github_sources.get(source_id)
            if not item:
                raise ValueError("연결된 GitHub 저장소를 찾을 수 없습니다.")
            previous = item["enabled"]
            item["enabled"] = enabled
            try:
                self.save_github_sources()
            except Exception:
                item["enabled"] = previous
                raise
            return dict(item)

    def remove_github_source(self, source_id: str):
        with self.lock:
            if source_id in self.github_polling:
                raise ValueError("브랜치 확인이 끝난 뒤 연결을 해제하세요.")
            item = self.github_sources.pop(source_id, None)
            if not item:
                raise ValueError("연결된 GitHub 저장소를 찾을 수 없습니다.")
            try:
                self.save_github_sources()
            except Exception:
                self.github_sources[source_id] = item
                raise
            return {"id": source_id, "disconnected": True}

    def _validate_github_request(self, application_id: str, targets: list[str], public: bool):
        if not self.ai_settings.available:
            raise ValueError("GitHub 소스 배포에는 AI 설정이 필요합니다.")
        if not re.fullmatch(r"[a-z][a-z0-9-]{2,30}", application_id):
            raise ValueError("올바른 앱 ID가 필요합니다.")
        if (
            not isinstance(targets, list)
            or not 1 <= len(targets) <= 3
            or len(set(targets)) != len(targets)
            or any(target not in TARGETS for target in targets)
            or ("auto" in targets and len(targets) != 1)
            or type(public) is not bool
        ):
            raise ValueError("배포 대상을 올바르게 선택하세요.")
        if "aws-ecs-express" in targets and not public:
            raise ValueError("AWS를 포함하려면 인터넷 공개를 허용하세요.")
        for target in targets:
            if target == "aws-ecs-express" and self.aws_settings.unavailable_reason():
                raise ValueError(self.aws_settings.unavailable_reason())
            if target == "cloud-run" and self.cloud_settings.unavailable_reason():
                raise ValueError(self.cloud_settings.unavailable_reason())

    def _reserve_single_github_job(
        self, project: Path, application_id: str, requested_target: str, public: bool, source: dict
    ) -> dict:
        profile = inspect_infrastructure(project)
        validate_infrastructure(profile, requested_target)
        if requested_target == "auto":
            available = ["local-docker"]
            if self.cloud_settings.unavailable_reason() is None:
                available.append("cloud-run")
            if public and self.aws_settings.unavailable_reason() is None:
                available.append("aws-ecs-express")
            plan = plan_infrastructure(
                project, available, public, self.infrastructure_planner_factory(self.ai_settings)
            )
            target = plan["target"]
            validate_infrastructure(profile, target)
        else:
            target = requested_target
            plan = explicit_infrastructure_plan(target, profile)
        plan["compatibility"] = infrastructure_compatibility(profile, target, public_access=public)
        if plan["compatibility"]["access_mode"] is None:
            raise ValueError("선택한 배포 대상의 공개 범위를 지원하지 않습니다.")
        digest = source_digest(project)
        job_id = uuid.uuid4().hex[:16]
        directory = self.root / job_id
        directory.mkdir()
        (directory / ".uncommitted-upload").touch(mode=0o600)
        try:
            copied = directory / "source"
            shutil.copytree(project, copied)
            if source_digest(copied) != digest:
                raise ValueError("GitHub 소스가 복사 중 변경됐습니다.")
            job = {
                "id": job_id,
                "mode": "agent",
                "target": target,
                "requested_target": requested_target,
                "infrastructure_plan": plan,
                "application_id": application_id,
                "public": plan["compatibility"]["access_mode"] == "public",
                "status": "running",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "plan": None,
                "diff": "",
                "changes": [],
                "steps": 0,
                "attempts": 0,
                "project": str(copied),
                "infrastructure_profile": profile.as_dict(),
                "events": [],
                "source_digest": digest,
                "github_source": source,
            }
            if target == "cloud-run":
                job["cloud"] = asdict(self.cloud_settings)
            elif target == "aws-ecs-express":
                job["aws"] = asdict(self.aws_settings)
            with self.lock:
                self.ensure_application_available(application_id, target)
                previous = [
                    old
                    for old in self.jobs.values()
                    if old.get("application_id") == application_id
                    and old.get("target") == target
                    and old.get("status") == "succeeded"
                    and old.get("deployment_state", "active") == "active"
                    and old.get("result")
                ]
                if target == "aws-ecs-express" and previous:
                    latest = max(previous, key=lambda item: item.get("created_at", ""))
                    if latest["result"].get("database") is not None:
                        raise ValueError(
                            "기존 PostgreSQL 서비스는 GitHub 자동 배포로 업데이트할 수 없습니다."
                        )
                    job["prior_result"] = latest["result"]
                    job["replaces_job_id"] = latest["id"]
                if target == "local-docker" and source.get("subscription_id") and previous:
                    matching = [
                        old
                        for old in previous
                        if old.get("github_source", {}).get("subscription_id") == source["subscription_id"]
                    ]
                    if matching:
                        job["git_replaces_local_job_id"] = max(
                            matching, key=lambda item: item.get("created_at", "")
                        )["id"]
                self.jobs[job_id] = job
                self.save(job_id)
            self.clear_upload_marker(directory)
            return {"id": job_id, "status": job["status"], "target": target}
        except Exception:
            with self.lock:
                self.jobs.pop(job_id, None)
            if not (directory / "job.json").exists():
                shutil.rmtree(directory, ignore_errors=True)
            raise

    def _reserve_github_revision(
        self,
        repository: GitHubRepository,
        branch: str,
        commit: str,
        application_id: str,
        targets: list[str],
        public: bool,
        subscription_id: str | None = None,
    ) -> tuple[dict, list[str]]:
        self._validate_github_request(application_id, targets, public)
        source = {
            "repository_url": repository.url,
            "branch": branch,
            "commit": commit,
            "subscription_id": subscription_id,
        }
        with tempfile.TemporaryDirectory(prefix=".github-import-", dir=self.root) as temporary:
            root = Path(temporary)
            archive = root / "source.zip"
            download_revision(repository, commit, archive)
            project = extract_project(archive, root / "extracted")
            if len(targets) == 1:
                job = self._reserve_single_github_job(project, application_id, targets[0], public, source)
                return job, [job["id"]]
            group = self.create_deployment_group(project, application_id, targets, public, source=source)
            return group, [child["job_id"] for child in group["targets"]]

    def _start_github_jobs(self, result: dict, job_ids: list[str]):
        if len(job_ids) == 1:
            self.start_job_worker(job_ids[0], self.run_agent)
        else:
            self.start_group_worker(result["id"])

    def _discard_unstarted_github_jobs(self, job_ids: list[str]):
        with self.lock:
            for job_id in job_ids:
                self.jobs.pop(job_id, None)
        for job_id in job_ids:
            shutil.rmtree(self.root / job_id, ignore_errors=True)

    def create_github_deployment(
        self,
        repository_url: str,
        branch: str | None,
        application_id: str,
        targets: list[str],
        public: bool,
        auto_deploy: bool,
    ) -> dict:
        repository = parse_repository_url(repository_url)
        self._validate_github_request(application_id, targets, public)
        if type(auto_deploy) is not bool:
            raise ValueError("자동 배포 선택이 올바르지 않습니다.")
        if auto_deploy and not self.github_poll_interval:
            raise ValueError("서버의 GitHub 자동 확인이 꺼져 있습니다.")
        with self.lock:
            if auto_deploy and (
                len(self.github_sources) >= 20
                or any(item["application_id"] == application_id for item in self.github_sources.values())
            ):
                raise ValueError(
                    "이 앱에는 이미 GitHub 자동 배포가 연결돼 있거나 연결 수 제한에 도달했습니다."
                )
        actual_branch, commit = resolve_revision(repository, branch)
        source_id = uuid.uuid4().hex[:16] if auto_deploy else None
        result, job_ids = self._reserve_github_revision(
            repository, actual_branch, commit, application_id, targets, public, source_id
        )
        if auto_deploy:
            item = {
                "id": source_id,
                "repository_url": repository.url,
                "branch": actual_branch,
                "application_id": application_id,
                "targets": targets,
                "public": public,
                "enabled": True,
                "last_revision": commit,
                "last_job_ids": job_ids,
                "last_error": None,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
            try:
                with self.lock:
                    if len(self.github_sources) >= 20 or any(
                        source["application_id"] == application_id for source in self.github_sources.values()
                    ):
                        raise ValueError("이 앱에는 이미 GitHub 자동 배포가 연결돼 있습니다.")
                    self.github_sources[source_id] = item
                    self.save_github_sources()
            except Exception:
                with self.lock:
                    self.github_sources.pop(source_id, None)
                self._discard_unstarted_github_jobs(job_ids)
                raise
        self._start_github_jobs(result, job_ids)
        return {
            "deployment": result,
            "repository_url": repository.url,
            "branch": actual_branch,
            "commit": commit,
            "source_id": source_id,
        }

    def poll_github_source(self, source_id: str, retry_failed: bool = False) -> dict:
        with self.lock:
            if source_id in self.github_polling:
                return {"changed": False, "in_progress": True}
            self.github_polling.add(source_id)
        try:
            return self._poll_github_source_once(source_id, retry_failed)
        finally:
            with self.lock:
                self.github_polling.discard(source_id)

    def _poll_github_source_once(self, source_id: str, retry_failed: bool = False) -> dict:
        with self.lock:
            item = self.github_sources.get(source_id)
            if not item or not item["enabled"]:
                raise ValueError("활성 GitHub 자동 배포 연결을 찾을 수 없습니다.")
            snapshot = dict(item)
        repository = parse_repository_url(snapshot["repository_url"])
        try:
            branch, commit = resolve_revision(repository, snapshot["branch"])
            if retry_failed and commit != snapshot["last_revision"]:
                raise ValueError("브랜치에 새 커밋이 있습니다. 지금 확인으로 새 커밋을 배포하세요.")
            if retry_failed and commit == snapshot["last_revision"]:
                with self.lock:
                    statuses = [
                        self.jobs[job_id]["status"] if job_id in self.jobs else "missing"
                        for job_id in snapshot["last_job_ids"]
                    ]
                if not (
                    any(status in {"failed", "cancelled", "interrupted"} for status in statuses)
                    and all(
                        status in {"succeeded", "failed", "cancelled", "interrupted"} for status in statuses
                    )
                ):
                    raise ValueError("다시 시도할 실패한 GitHub 배포가 없습니다.")
            if commit != snapshot["last_revision"] or retry_failed:
                with self.lock:
                    pending = any(
                        job.get("github_source", {}).get("subscription_id") == source_id
                        and job.get("status") in {"planned", "provisioning", "running", "waiting_input"}
                        for job in self.jobs.values()
                    )
                if pending:
                    return {
                        "changed": False,
                        "pending": True,
                        "commit": commit,
                        "job_ids": snapshot["last_job_ids"],
                    }
                result, job_ids = self._reserve_github_revision(
                    repository,
                    branch,
                    commit,
                    snapshot["application_id"],
                    snapshot["targets"],
                    snapshot["public"],
                    source_id,
                )
                try:
                    with self.lock:
                        current = self.github_sources.get(source_id)
                        if (
                            not current
                            or not current["enabled"]
                            or current["last_revision"] != snapshot["last_revision"]
                        ):
                            raise ValueError("GitHub 자동 배포 설정이 변경됐습니다. 작업 상태를 확인하세요.")
                        current.update(
                            last_revision=commit,
                            last_job_ids=job_ids,
                            last_error=None,
                            checked_at=datetime.now(timezone.utc).isoformat(),
                        )
                        self.save_github_sources()
                except Exception:
                    with self.lock:
                        current = self.github_sources.get(source_id)
                        if current:
                            current.update(
                                last_revision=snapshot["last_revision"],
                                last_job_ids=snapshot["last_job_ids"],
                            )
                    self._discard_unstarted_github_jobs(job_ids)
                    raise
                self._start_github_jobs(result, job_ids)
                return {"changed": True, "commit": commit, "job_ids": job_ids}
            with self.lock:
                current = self.github_sources.get(source_id)
                if current:
                    current.update(last_error=None, checked_at=datetime.now(timezone.utc).isoformat())
                    self.save_github_sources()
            return {"changed": False, "commit": commit, "job_ids": snapshot["last_job_ids"]}
        except Exception as exc:
            with self.lock:
                current = self.github_sources.get(source_id)
                if current:
                    current.update(
                        last_error=str(exc)[:300], checked_at=datetime.now(timezone.utc).isoformat()
                    )
                    self.save_github_sources()
            raise

    def github_poll_loop(self, stop: threading.Event, interval: int):
        if stop.wait(5):
            return
        while not stop.is_set():
            with self.lock:
                source_ids = [item["id"] for item in self.github_sources.values() if item["enabled"]]
            for source_id in source_ids:
                if stop.is_set():
                    return
                try:
                    self.poll_github_source(source_id)
                except Exception:
                    continue
            if stop.wait(interval):
                return

    def retire_replaced_github_local(self, job_id: str):
        with self.lock:
            current = self.jobs.get(job_id)
            previous = self.jobs.get(current.get("git_replaces_local_job_id")) if current else None
            if not current or not previous or current.get("status") != "succeeded":
                return
            snapshot = dict(previous)
        try:
            LocalDockerAdapter(lambda *_: None).retire(snapshot["result"], snapshot["id"])
        except Exception as exc:
            self.event(job_id, "previous_local_cleanup_failed", str(exc)[:300])
            return
        with self.lock:
            previous = self.jobs.get(snapshot["id"])
            if previous and previous.get("deployment_state") == "active":
                previous["deployment_state"] = "deleted"
                previous["retired_at"] = datetime.now(timezone.utc).isoformat()
                self.save(previous["id"])
