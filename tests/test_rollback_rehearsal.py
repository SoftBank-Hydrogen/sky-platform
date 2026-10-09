"""Exercise the real Sky HTTP API and release state machine; stub only AWS/probes."""

import json
import threading
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import pytest

from adapters.aws.ecs import AwsSettings
from application.analysis import AISettings
from interfaces.http.server import App, handler_for
from scripts.rollback_rehearsal import RehearsalError, SkyClient, rehearse

ACCOUNT = "123456789012"
REGION = "ap-northeast-2"
APPLICATION = "rollback-demo-tug"
A, B = "a" * 16, "b" * 16
SERVICE = "sky-" + A + "-a1"


@pytest.fixture
def environment(tmp_path):
    app = App(tmp_path, AISettings("fixture", "fixture"), aws_settings=AwsSettings(REGION))
    images = [f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/sky-managed:{job_id}-a1" for job_id in (A, B)]
    for index, job_id in enumerate((A, B)):
        source = tmp_path / job_id / "source" / "app"
        source.mkdir(parents=True)
        app.jobs[job_id] = {
            "id": job_id,
            "mode": "agent",
            "status": "succeeded",
            "application_id": APPLICATION,
            "target": "aws-ecs-express",
            "deployment_state": "superseded" if job_id == A else "active",
            "project": str(source),
            "events": [],
            "aws": {"region": REGION},
            "plan": None,
            "application_ir": {"hypotheses": [{"kind": "sky-probe-protocol"}]},
            "result": {
                "account": ACCOUNT,
                "region": REGION,
                "target": "aws-ecs-express",
                "service": SERVICE,
                "service_arn": f"arn:aws:ecs:{REGION}:{ACCOUNT}:service/default/{SERVICE}",
                "owner_attempt": A + "-a1",
                "image": images[index],
                "images": images[: index + 1],
                "url": f"https://{SERVICE}.ecs.{REGION}.on.aws",
                "task_definition_arn": f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{SERVICE}:{index + 1}",
            },
        }
        app.save(job_id)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield app, SkyClient(f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def execute(client, **kwargs):
    return rehearse(
        client,
        current_id=B,
        previous_id=A,
        application_id=APPLICATION,
        account=ACCOUNT,
        region=REGION,
        interval=0.01,
        timeout=3,
        **kwargs,
    )


def rollback_stub(_adapter, _current, previous, _health_path, checkpoint):
    checkpoint(release_rollback_submitted=True)
    return {
        "state": "successful",
        "image": previous["image"],
        "url": previous["url"],
        "service_deployment_arn": f"arn:aws:ecs:{REGION}:{ACCOUNT}:service-deployment/default/{SERVICE}/test",
    }


def test_dry_run_and_confirmation_do_not_mutate(environment):
    app, client = environment
    with patch("interfaces.http.server.AwsExpressAdapter.rollback_release") as rollback:
        assert execute(client)["status"] == "planned"
        report = execute(client, execute=True)
        assert report["error"] == "explicit_application_confirmation_required"
        rollback.assert_not_called()
    assert app.jobs[B]["deployment_state"] == "active"
    assert app.health_history == {}


def test_real_http_rollback_and_restore_with_persisted_evidence(environment):
    app, client = environment
    with (
        patch(
            "interfaces.http.server.AwsExpressAdapter.rollback_release",
            autospec=True,
            side_effect=rollback_stub,
        ) as rollback,
        patch("application.monitoring.check_deployment", return_value={"healthy": True}),
        patch(
            "application.monitoring.probe_sky_game",
            return_value={"status": "passed", "protocol": "sky.probe.v1"},
        ),
    ):
        report = execute(client, execute=True, confirm_application=APPLICATION, require_websocket=True)
    assert report["status"] == "passed", report
    assert report["restoration"] == "verified"
    assert rollback.call_count == 2
    assert app.jobs[B]["deployment_state"] == "active"
    assert app.jobs[A]["deployment_state"] == "superseded"
    restarted = App(app.root, AISettings("fixture", "fixture"), aws_settings=AwsSettings(REGION))
    assert restarted.jobs[B]["deployment_state"] == "active"
    assert restarted.jobs[A]["release_rollback_verification"]["target_job_id"] == B
    assert restarted.jobs[B]["release_rollback_verification"]["target_job_id"] == A
    # Independent runner evidence does not overwrite Sky's certificate status.
    from application.certificate import deployment_certificate

    checks = deployment_certificate(restarted.jobs[B])["verification"]
    assert next(check for check in checks if check["name"] == "rollback_rehearsal")["status"] == "unverified"
    assert "fixture" not in json.dumps(report)


def test_failed_previous_websocket_still_restores_original(environment):
    app, client = environment
    with (
        patch(
            "interfaces.http.server.AwsExpressAdapter.rollback_release",
            autospec=True,
            side_effect=rollback_stub,
        ),
        patch("application.monitoring.check_deployment", return_value={"healthy": True}),
        patch(
            "application.monitoring.probe_sky_game",
            side_effect=[
                {"status": "passed", "protocol": "sky.probe.v1"},
                {"status": "failed"},
                {"status": "passed", "protocol": "sky.probe.v1"},
            ],
        ),
    ):
        report = execute(client, execute=True, confirm_application=APPLICATION, require_websocket=True)
    assert report["status"] == "failed"
    assert report["error"] == "websocket_exchange_failed"
    assert report["restoration"] == "verified"
    assert app.jobs[B]["deployment_state"] == "active"


def test_ambiguous_aws_failure_does_not_retry_or_attempt_restore(environment):
    app, client = environment

    def uncertain(_adapter, _current, _previous, _path, checkpoint):
        checkpoint(release_rollback_submitted=True)
        raise RuntimeError("AWS outcome unknown")

    with (
        patch(
            "interfaces.http.server.AwsExpressAdapter.rollback_release", autospec=True, side_effect=uncertain
        ) as rollback,
        patch("application.monitoring.check_deployment", return_value={"healthy": True}),
    ):
        report = execute(client, execute=True, confirm_application=APPLICATION)
    assert report["status"] == "failed"
    assert report["restoration"] == "needs_attention"
    assert rollback.call_count == 1
    assert app.jobs[B]["deployment_state"] == "needs_attention"


@pytest.mark.parametrize("mismatch", ["account", "region", "application", "image"])
def test_wrong_scope_rejected_before_mutation(environment, mismatch):
    app, client = environment
    if mismatch == "application":
        app.jobs[A]["application_id"] = "someone-else"
    elif mismatch == "image":
        app.jobs[A]["result"]["image"] = "unknown"
    else:
        app.jobs[A]["result"][mismatch] = "unexpected"
    with patch("interfaces.http.server.AwsExpressAdapter.rollback_release") as rollback:
        report = execute(client, execute=True, confirm_application=APPLICATION)
        assert report["status"] == "failed"
        rollback.assert_not_called()


def test_remote_http_and_credential_url_rejected():
    for url in (
        "http://example.com",
        "https://user:password@example.com",
        "https://example.com/?token=secret",
    ):
        with pytest.raises(RehearsalError):
            SkyClient(url)


def test_failed_initial_health_does_not_switch(environment):
    _, client = environment
    with (
        patch("application.monitoring.check_deployment", return_value={"healthy": False}),
        patch("interfaces.http.server.AwsExpressAdapter.rollback_release") as rollback,
    ):
        report = execute(client, execute=True, confirm_application=APPLICATION)
    assert report["error"] == "deployment_health_failed"
    assert report["restoration"] == "not_needed"
    rollback.assert_not_called()


def test_restore_failure_cannot_be_reported_as_success(environment):
    _, client = environment
    calls = 0

    def fail_restore(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("Restore failed")
        return rollback_stub(*args)

    with (
        patch(
            "interfaces.http.server.AwsExpressAdapter.rollback_release",
            autospec=True,
            side_effect=fail_restore,
        ),
        patch("application.monitoring.check_deployment", return_value={"healthy": True}),
    ):
        report = execute(client, execute=True, confirm_application=APPLICATION)
    assert report["status"] == "failed"
    assert report["restoration"] == "needs_attention"
    assert calls == 2
