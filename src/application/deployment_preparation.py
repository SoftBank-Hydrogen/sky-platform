"""Uploaded sources to durable previews; no uploaded code, Docker, AI or SQL execution."""

import hashlib
from dataclasses import asdict

from application.analysis import redact
from application.deployment_core import MAX_UPLOAD, analyze, make_plan, source_digest
from application.infrastructure import inspect_infrastructure, preflight_sqlite_conversion
from domain.access import Action, Principal, ResourceOwner, permitted


def _options(port, health_path):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Invalid container port")
    import re

    if (
        not isinstance(health_path, str)
        or len(health_path) > 200
        or not re.fullmatch(r"/[A-Za-z0-9/_.-]*", health_path)
        or ".." in health_path
        or "//" in health_path
    ):
        raise ValueError("Invalid HTTP health path")
    return {"port": port, "health_path": health_path, "analyzer": "static"}


def _plan(project, options):
    initial = analyze(project)
    if initial.runtime == "custom-dockerfile":
        selector = "dockerfile"
    elif initial.runtime == "nodejs":
        selector = (
            "start"
            if initial.start_command == "npm start"
            else initial.start_command.removeprefix("npm run ")
        )
    elif initial.runtime in {"python-asgi", "python-wsgi"}:
        selector = (
            ("asgi:" if initial.runtime == "python-asgi" else "wsgi:")
            + initial.start_command.split()[3].split(":")[0]
            + ".py"
        )
    else:
        selector = initial.start_command.split()[1]
    metadata = {
        key: value
        for key, value in asdict(initial).items()
        if key in {"analyzer", "framework", "rationale", "required_env", "evidence", "model"}
    }
    warnings = [warning for warning in initial.warnings if "PORT=3000" not in warning]
    warnings.append(
        "포트와 HTTP 검사 경로는 요청에서 선택한 값입니다. 실제 빌드·응답·WebSocket 검증은 아직 수행하지 않았습니다."
    )
    plan = make_plan(
        project,
        selector,
        "build" if initial.build_command == "npm run build" else None,
        port=options["port"],
        health_path=options["health_path"],
        warnings=warnings,
        **metadata,
    )
    plan.target = "aws"
    return asdict(plan)


class DeploymentPreparationService:
    def __init__(self, sources, previews):
        self.sources = sources
        self.previews = previews

    def prepare(self, principal, application_id, request_key, upload, *, port=3000, health_path="/"):
        if not isinstance(principal, Principal) or not permitted(
            principal, Action.DEPLOY, ResourceOwner(principal.organization_id, principal.user_id)
        ):
            raise PermissionError("Deployment preparation access denied")
        if not isinstance(upload, bytes) or not 0 < len(upload) <= MAX_UPLOAD:
            raise ValueError("ZIP upload exceeds 20 MiB limit")
        options = _options(port, health_path)
        lease, preview = self.previews.reserve(
            principal, application_id, request_key, hashlib.sha256(upload).hexdigest(), options
        )
        if lease is None:
            return preview
        try:
            original = self.sources.capture_upload(
                principal, application_id, lease.id.replace("-", ""), upload
            ).artifact
            with self.sources.restore(principal, original) as project:
                plan = _plan(project, options)
                profile = inspect_infrastructure(project)
                inspection = {
                    "requirements": list(profile.requirements),
                    "evidence_files": list(profile.evidence),
                    "scanned_files": profile.scanned_files,
                    "database_engines": list(profile.database_engines),
                }
                blockers = []
                if "sqlite" in profile.requirements:
                    try:
                        conversion, _ = preflight_sqlite_conversion(project, profile)
                        inspection["sqlite_conversion"] = conversion
                        blockers.append(
                            {
                                "code": "sqlite_conversion_required",
                                "message": "SQLite 데이터 이전과 앱의 PostgreSQL 코드 변환·검증이 필요합니다.",
                            }
                        )
                    except ValueError as error:
                        blockers.append(
                            {"code": "sqlite_conversion_unsupported", "message": redact(str(error))[:500]}
                        )
                others = set(profile.requirements) - {"sqlite"}
                if others:
                    blockers.append(
                        {
                            "code": "infrastructure_preparation_required",
                            "requirements": sorted(others),
                            "message": "추가 인프라와 앱 설정 연결이 필요합니다.",
                        }
                    )
                if plan["required_env"]:
                    blockers.append(
                        {
                            "code": "environment_binding_required",
                            "names": plan["required_env"],
                            "message": "필수 환경변수의 안전한 실행 설정 연결이 필요합니다.",
                        }
                    )
                # Snapshot remains unchanged at this stage. Never pretend SQL compilation rewrites application code.
                prepared = self.sources.capture_prepared(
                    principal, original, project, expected_digest=source_digest(project)
                )
                plan["source_digest"] = prepared.source_digest
                plan["replicas"] = 1
                return self.previews.finish(principal, lease, original, prepared, plan, inspection, blockers)
        except Exception:
            # Release only this generation; expired ownership cannot erase a replacement owner.
            # If release itself fails, retain the original failure and let the lease expire.
            try:
                self.previews.release(lease)
            except OSError:
                pass
            raise

    def detail(self, principal, preview_id):
        return self.previews.get(principal, preview_id)

    def approve(self, principal, preview_id, expected_digest):
        return self.previews.approve(principal, preview_id, expected_digest)
