from copy import deepcopy
from threading import RLock
from unittest.mock import Mock

import pytest

from application.certificate import deployment_certificate
from application.monitoring import MonitoringMixin
from application.session_evidence import bind_session_rehearsal, recorded_session_rehearsal


def fixture():
    job_id = 'a' * 16
    image = f'sky/{job_id}-a1:latest'
    job = {'id': job_id, 'application_id': 'game-demo', 'target': 'local-docker', 'status': 'succeeded',
           'source_digest': 'b' * 64, 'plan': {'source_digest': 'c' * 64},
           'application_ir': {'hypotheses': [{'kind': 'websocket'}]},
           'result': {'url': 'http://127.0.0.1:8080', 'container': f'sky-{job_id}-a1',
                      'image': image, 'image_id': 'sha256:' + 'd' * 64}}
    observations = []
    for operation in ('restart', 'container_replacement'):
        observations.append({'operation': operation, 'old_connection': 'closed', 'close_code': 1006,
            'clean_close': False, 'reconnect': 'passed', 'session_continuity': 'lost', 'memory_state': 'reset',
            'persisted_scoreboard': 'unchanged', 'before_round': 1, 'after_round': 0,
            'before_taps': {'A': 3, 'B': 0}, 'after_taps': {'A': 0, 'B': 0}, 'recovery_seconds': 2.5,
            'before_container_id': 'e' * 64,
            'after_container_id': 'e' * 64 if operation == 'restart' else 'f' * 64,
            'before_started_at': '2026-10-11T00:00:00Z', 'after_started_at': '2026-10-11T00:00:03Z'})
    receipt = {'protocol': 'sky-game-session-drill-v1', 'scope': 'disposable-local-docker',
               'drill_status': 'passed', 'session_continuity': 'lost', 'image': image,
               'image_id': job['result']['image_id'], 'room_fixture': {
                   'minPlayers': 1, 'countdownMs': 100, 'roundMs': 30000},
               'checked_at': '2026-10-11T00:00:04Z', 'seed_rounds': 13, 'persisted_rounds': 14,
               'observations': observations}
    return job, receipt


def test_bound_replica_record_is_separate_from_live_session_continuity():
    job, receipt = fixture()
    receipt['password'] = 'synthetic-secret'
    job['websocket_session_rehearsal'] = bind_session_rehearsal(job, receipt)
    assert recorded_session_rehearsal(job) == job['websocket_session_rehearsal']
    certificate = deployment_certificate(job)
    checks = {item['name']: item for item in certificate['verification']}
    assert checks['websocket_session_rehearsal']['status'] == 'passed'
    assert checks['websocket_session_continuity']['status'] == 'unverified'
    assert 'synthetic-secret' not in str(certificate)
    job['websocket_session_rehearsal']['receipt']['observations'][0]['recovery_seconds'] = 1
    assert recorded_session_rehearsal(job) is None


@pytest.mark.parametrize('key', ['id', 'application_id', 'source_digest', 'target', 'status'])
def test_different_deployment_cannot_reuse_record(key):
    job, receipt = fixture()
    job['websocket_session_rehearsal'] = bind_session_rehearsal(job, receipt)
    job[key] = 'other'
    assert recorded_session_rehearsal(job) is None


@pytest.mark.parametrize('key', ['image', 'image_id', 'container'])
def test_changed_runtime_cannot_reuse_record(key):
    job, receipt = fixture()
    job['websocket_session_rehearsal'] = bind_session_rehearsal(job, receipt)
    job['result'][key] = 'changed'
    assert recorded_session_rehearsal(job) is None


@pytest.mark.parametrize('key,value', [
    ('scope', 'live-aws'), ('image_id', 'sha256:' + 'f' * 64), ('persisted_rounds', 13),
    ('observations', []), ('session_continuity', 'preserved'), ('checked_at', 'invalid')])
def test_invalid_receipts_fail_closed(key, value):
    job, receipt = fixture()
    receipt[key] = value
    with pytest.raises(ValueError):
        bind_session_rehearsal(job, receipt)


@pytest.mark.parametrize('key,value', [
    ('old_connection', 'open'), ('reconnect', 'failed'), ('memory_state', 'preserved'),
    ('before_started_at', '2026-10-11T00:00:03Z'), ('recovery_seconds', float('nan')),
    ('after_container_id', 'f' * 64), ('before_taps', {'A': 0, 'B': 0}), ('clean_close', True)])
def test_inconsistent_transition_is_rejected(key, value):
    job, receipt = fixture()
    receipt['observations'][0][key] = value
    with pytest.raises(ValueError):
        bind_session_rehearsal(job, receipt)


def test_trusted_recorder_persists_and_restores_memory_on_save_failure():
    job, receipt = fixture()
    app = MonitoringMixin()
    app.lock, app.jobs, app.record_store = RLock(), {job['id']: job}, Mock()
    recorded = app.record_session_rehearsal(job['id'], receipt)
    assert app.record_store.save_job.call_args.args[0] == job['id']
    assert recorded == recorded_session_rehearsal(job)
    before = deepcopy(job)
    app.record_store.save_job.side_effect = OSError('State unavailable')
    receipt['checked_at'] = '2026-10-11T00:00:05Z'
    with pytest.raises(OSError):
        app.record_session_rehearsal(job['id'], receipt)
    assert job == before
