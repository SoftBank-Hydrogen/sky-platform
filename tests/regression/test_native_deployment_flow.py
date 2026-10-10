"""Authenticated ZIP -> durable job -> native execution -> retirement/restart."""
import io
import json
from pathlib import Path
from unittest.mock import Mock, patch
import zipfile

import pytest

from adapters.aws.ecs import AwsSettings
from application.analysis import AISettings
from application.deployment_core import source_digest
from application.native_deployments import native_profile
from interfaces.http.server import App, handler_for


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('SKY_AWS_ROLE_BOUNDARY_ARN', 'arn:aws:iam::123456789012:policy/sky-runtime')
    monkeypatch.delenv('SKY_ENVIRONMENT', raising=False)
    instance = App(tmp_path, AISettings('', ''), aws_settings=AwsSettings(
        'ap-northeast-2', expected_account='123456789012'), monitor_interval=0)
    yield instance


def archive(source):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as package:
        for name, content in source.items():
            package.writestr(name, content)
    return stream.getvalue()


def request(app, path, body=b'', target='aws-lambda', public='true'):
    handler = handler_for(app).__new__(handler_for(app))
    handler.path = path
    handler.headers = {'X-Sky-Token': app.token, 'X-Application-Id': 'native-demo',
        'X-Public-Access': public, 'X-Deploy-Target': target, 'Content-Length': str(len(body))}
    handler.rfile = io.BytesIO(body)
    handler.json_response = Mock()
    handler.do_POST()
    return handler.json_response.call_args.args


def create(app, source=None):
    body = archive(source or {'handler.py': 'def handler(event, context): return {"statusCode":200,"body":"ok"}'})
    with patch.object(app, 'native_unavailable_reason', return_value=None), patch.object(app, 'start_job_worker'):
        status, payload = request(app, '/api/deployments', body)
    assert status == 202, payload
    return payload['id']


def test_native_upload_needs_no_ai_and_survives_restart(app):
    job_id = create(app)
    assert app.jobs[job_id]['mode'] == 'native_aws'
    assert app.jobs[job_id]['native_plan']['target'] == 'aws-lambda'
    assert not app.ai_settings.available
    restarted = App(app.root, AISettings('', ''), aws_settings=app.aws_settings, monitor_interval=0)
    assert restarted.jobs[job_id]['status'] == 'interrupted'
    assert restarted.jobs[job_id]['source_digest'] == app.jobs[job_id]['source_digest']


def test_native_worker_records_verified_result(app):
    job_id = create(app)
    fake = Mock()
    job=app.jobs[job_id]
    import base64
    fake.deploy.return_value = {'target': 'aws-lambda', 'application_id': job['application_id'],
        'attempt_id': job['attempt_id'], 'account': '123456789012', 'region': 'ap-northeast-2',
        'artifact_digest': base64.b64encode(bytes.fromhex(job['native_plan']['artifact_sha256'])).decode(),
        'status': 'verified', 'artifact_verified': True,
                               'http_verified': True, 'url': 'https://test.lambda-url.ap-northeast-2.on.aws/'}
    with patch.object(app, 'native_adapter', return_value=fake):
        app.run_native(job_id)
    assert app.jobs[job_id]['status'] == 'succeeded'
    fake.deploy.assert_called_once()


def test_native_worker_rejects_changed_source_and_unverified_results(app):
    job_id = create(app)
    Path(app.jobs[job_id]['project'], 'handler.py').write_text('def handler(event, context): return {}')
    with patch.object(app, 'native_adapter') as adapter:
        app.run_native(job_id)
    adapter.assert_not_called()
    assert app.jobs[job_id]['status'] == 'failed'
    assert app.jobs[job_id]['deployment_state'] == 'deleted'


def test_failed_native_stack_can_be_retired(app):
    job_id = create(app)
    def factory(job, checkpoint=None):
        fake = Mock()
        def deploy(*args, **kwargs):
            checkpoint({'stack_name': 'owned', 'status': 'creating'})
            raise TimeoutError('uncertain')
        fake.deploy.side_effect = deploy
        fake.destroy.return_value = {'status': 'deleted'}
        return fake
    with patch.object(app, 'native_adapter', side_effect=factory):
        app.run_native(job_id)
        assert app.jobs[job_id]['deployment_state'] == 'needs_attention'
        with patch('interfaces.http.server.threading.Thread.start'):
            status, payload = request(app, f'/api/jobs/{job_id}/retire')
        assert status == 202, payload
        app.retire_native(job_id)
    assert app.jobs[job_id]['deployment_state'] == 'deleted'


def test_native_rejects_no_public_permission_and_persistent_database(app):
    status, _ = request(app, '/api/deployments', archive({'handler.py': 'def handler(event, context): return {}'}), public='false')
    assert status == 400
    with patch.object(app, 'native_unavailable_reason', return_value=None):
        status, _ = request(app, '/api/deployments', archive({
            'handler.py': 'import sqlite3\ndef handler(event, context): return sqlite3.connect("data.db")'}))
    assert status == 400
    assert not app.jobs


def test_native_duplicate_application_is_rejected(app):
    create(app)
    with patch.object(app, 'native_unavailable_reason', return_value=None):
        status, _ = request(app, '/api/deployments', archive({'handler.py': 'def handler(event, context): return {}'}))
    assert status == 400
    assert len(app.jobs) == 1


def test_ec2_profile_requires_http_and_no_database(tmp_path):
    (tmp_path / 'server.py').write_text('from http.server import HTTPServer, SimpleHTTPRequestHandler\nHTTPServer(("0.0.0.0", 8080), SimpleHTTPRequestHandler).serve_forever()')
    result = native_profile(tmp_path, 'aws-ec2')
    assert result['plan']['target'] == 'aws-ec2'
    assert result['source_digest'] == source_digest(tmp_path)
    (tmp_path / 'db.py').write_text('import sqlite3\nconnection = sqlite3.connect("app.db")')
    with pytest.raises(ValueError):
        native_profile(tmp_path, 'aws-ec2')


def test_missing_aws_settings_disable_native_targets_instead_of_breaking_config(tmp_path, monkeypatch):
    monkeypatch.delenv('SKY_AWS_ROLE_BOUNDARY_ARN', raising=False)
    instance = App(tmp_path, AISettings('', ''), aws_settings=AwsSettings(), monitor_interval=0)
    assert instance.native_unavailable_reason('aws-lambda')
    assert instance.native_unavailable_reason('aws-ec2')


def test_native_certificate_requires_source_and_artifact_binding(app):
    import base64
    from application.certificate import deployment_certificate
    job_id = create(app)
    job = app.jobs[job_id]
    digest = base64.b64encode(bytes.fromhex(job['native_plan']['artifact_sha256'])).decode()
    result = {'target': 'aws-lambda', 'attempt_id': job['attempt_id'], 'stack_id': 'owned',
              'artifact_digest': digest, 'artifact_verified': True, 'http_verified': True,
              'status': 'verified', 'url': 'https://test.lambda-url.ap-northeast-2.on.aws/'}
    job.update(status='succeeded', result=result, native_receipt=dict(result))
    report = deployment_certificate(job)
    assert {c['name'] for c in report['verification'] if c['status']=='passed'} == {'source_binding','running_artifact','http_endpoint'}
    assert report['compilation_status'] == 'unrecorded'
    job['native_receipt']['artifact_digest'] = 'different'
    report = deployment_certificate(job)
    assert report['destination']['url'] is None
    assert 'running_artifact' in report['unverified']
