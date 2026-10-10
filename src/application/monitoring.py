"""Post-deployment health and protocol observations for an App instance."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from application.analysis import redact
from application.health import check_deployment
from application.session_evidence import bind_session_rehearsal
from application.websocket_probe import WebSocketProbeError, probe_sky_game


class MonitoringMixin:
    """Keep observations separate from deployment execution and state transitions."""

    def record_session_rehearsal(self, job_id: str, receipt: dict) -> dict:
        """Internal trusted-runner boundary; never accept public receipt uploads."""
        with self.lock:
            job = self.jobs.get(job_id)
            if not isinstance(job, dict) or job.get('id') != job_id:
                raise ValueError('Deployment not found')
            record = bind_session_rehearsal(job, receipt)
            updated = json.loads(json.dumps(job))
            updated['websocket_session_rehearsal'] = record
            # The legacy save method converts I/O failure into deployment
            # failure. Observational evidence must not alter deployment status.
            self.record_store.save_job(job_id, updated)
            job['websocket_session_rehearsal'] = record
            return json.loads(json.dumps(record))

    def check_and_record_health(self, job_id: str, source: str = "manual") -> dict:
        if source not in {"automatic", "manual"}:
            raise ValueError("Invalid health check source")
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job.get("status") != "succeeded" or not isinstance(job.get("result"), dict):
                raise ValueError("Only completed deployments can be checked")
            snapshot = json.loads(json.dumps(job))
        result = check_deployment(snapshot)
        entry = {
            "healthy": bool(result.get("healthy")),
            "checked_at": result.get("checked_at") or datetime.now(timezone.utc).isoformat(),
            "reason": redact(str(result.get("reason") or ""))[:300],
            "source": source,
        }
        with self.lock:
            current = self.jobs.get(job_id)
            if (
                not current
                or current.get("status") != "succeeded"
                or current.get("deployment_state", "active") != "active"
                or current.get("result") != snapshot["result"]
                or (
                    source == "automatic"
                    and any(
                        other is not current
                        and other.get("application_id", other["id"])
                        == current.get("application_id", current["id"])
                        and other.get("target", "local-docker") == current.get("target", "local-docker")
                        and other.get("status") in {"provisioning", "running", "waiting_input"}
                        for other in self.jobs.values()
                    )
                )
            ):
                return result
            history = (self.health_history.get(job_id, []) + [entry])[-20:]
            try:
                self.record_store.save_health(job_id, history)
                self.health_history[job_id] = history
                self.monitor_errors.pop(job_id, None)
            except OSError as exc:
                self.monitor_errors[job_id] = "상태 확인 기록 저장 실패: " + str(exc)[:200]
        return result

    def check_and_record_websocket(self, job_id: str) -> dict:
        with self.lock:
            job = self.jobs.get(job_id)
            if (
                not job
                or job.get("status") != "succeeded"
                or job.get("deployment_state", "active") != "active"
                or not isinstance(job.get("result"), dict)
            ):
                raise ValueError("실행 중인 완료 배포만 WebSocket을 확인할 수 있습니다.")
            ir = job.get("application_ir")
            hypotheses = ir.get("hypotheses") or [] if isinstance(ir, dict) else []
            if not any(
                item.get("kind") == "sky-probe-protocol" for item in hypotheses if isinstance(item, dict)
            ):
                raise ValueError("이 앱에는 sky.probe 응답 계약이 확인되지 않았습니다.")
            if job.get("target") == "cloud-run" and not job["result"].get("public"):
                raise ValueError("비공개 Cloud Run WebSocket 인증 검사는 아직 지원하지 않습니다.")
            snapshot = json.loads(json.dumps(job))
        health = check_deployment(snapshot)
        if not health["healthy"]:
            outcome = {
                "status": "failed",
                "protocol": "sky.probe.v1",
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "reason": "대상 소유권·현재 HTTP 상태를 확인하지 못했습니다.",
            }
        else:
            try:
                outcome = probe_sky_game(snapshot["result"]["url"])
            except WebSocketProbeError as exc:
                outcome = {
                    "status": "failed",
                    "protocol": "sky.probe.v1",
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "reason": str(exc),
                }
        with self.lock:
            current = self.jobs.get(job_id)
            if (
                not current
                or current.get("status") != "succeeded"
                or current.get("deployment_state", "active") != "active"
                or current.get("result") != snapshot["result"]
            ):
                raise ValueError("WebSocket 확인 중 배포 상태가 바뀌었습니다. 다시 확인하세요.")
            current["websocket_verification"] = outcome
            self.save(job_id)
        return outcome

    def monitor_once(self) -> None:
        with self.lock:
            jobs = list(self.jobs.values())
            candidates = [
                job["id"]
                for job in jobs
                if job.get("status") == "succeeded"
                and job.get("result")
                and job.get("deployment_state", "active") == "active"
                and job.get("release_rollback_state") not in {"running", "needs_attention"}
                and not any(
                    other is not job
                    and other.get("application_id", other["id"]) == job.get("application_id", job["id"])
                    and other.get("target", "local-docker") == job.get("target", "local-docker")
                    and other.get("status") in {"provisioning", "running", "waiting_input"}
                    for other in jobs
                )
            ]
        for job_id in candidates:
            try:
                self.check_and_record_health(job_id, "automatic")
            except Exception as exc:
                # Monitoring is observational; it must not alter deployment history on failure.
                with self.lock:
                    self.monitor_errors[job_id] = "자동 상태 확인 실패: " + str(exc)[:200]
                continue

    def monitor_loop(self, stop: threading.Event) -> None:
        if stop.wait(5):
            return
        while not stop.is_set():
            self.monitor_once()
            if stop.wait(self.monitor_interval):
                return
