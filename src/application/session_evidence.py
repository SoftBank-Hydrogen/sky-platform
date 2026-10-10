"""Bind trusted local replica observations without claiming live session continuity."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime


def session_binding(job: dict) -> dict:
    result, plan = job.get('result'), job.get('plan')
    if (job.get('status') != 'succeeded' or job.get('deployment_state', 'active') != 'active'
            or job.get('target') != 'local-docker' or not isinstance(result, dict)
            or not isinstance(plan, dict)):
        raise ValueError('Active local deployment with an image identity is required')
    binding = {'job_id': job.get('id'), 'application_id': job.get('application_id'),
               'source_revision': job.get('source_digest'), 'prepared_revision': plan.get('source_digest'),
               'target': job['target'], 'image': result.get('image'), 'image_id': result.get('image_id'),
               'container': result.get('container')}
    if (not isinstance(binding['job_id'], str) or not re.fullmatch(r'[a-f0-9]{16}', binding['job_id'])
            or not isinstance(binding['application_id'], str) or not binding['application_id']
            or any(not isinstance(binding[key], str) or not re.fullmatch(r'[a-f0-9]{64}', binding[key])
                   for key in ('source_revision', 'prepared_revision'))
            or not isinstance(binding['image_id'], str)
            or not re.fullmatch(r'sha256:[a-f0-9]{64}', binding['image_id'])
            or not isinstance(binding['container'], str)
            or not re.fullmatch('sky-' + binding['job_id'] + r'(?:-a[1-3])?', binding['container'])
            or binding['image'] != 'sky/' + binding['container'].removeprefix('sky-') + ':latest'):
        raise ValueError('Deployment source or runtime image identity is missing')
    return binding


def bind_session_rehearsal(job: dict, receipt: dict) -> dict:
    """Only a trusted runner may supply receipts; there is no public import route."""
    binding = session_binding(job)
    if (not isinstance(receipt, dict) or receipt.get('protocol') != 'sky-game-session-drill-v1'
            or receipt.get('scope') != 'disposable-local-docker' or receipt.get('drill_status') != 'passed'
            or receipt.get('session_continuity') != 'lost' or receipt.get('image') != binding['image']
            or receipt.get('image_id') != binding['image_id']
            or receipt.get('room_fixture') != {'minPlayers': 1, 'countdownMs': 100, 'roundMs': 30000}):
        raise ValueError('Replica evidence does not match the deployed image or supported drill')
    try:
        timestamp = datetime.fromisoformat(receipt['checked_at'])
        if timestamp.tzinfo is None:
            raise ValueError('Evidence time must include a timezone')
    except (KeyError, TypeError, ValueError):
        raise ValueError('Evidence timestamp is invalid') from None
    seed, persisted = receipt.get('seed_rounds'), receipt.get('persisted_rounds')
    if type(seed) is not int or seed < 0 or type(persisted) is not int or persisted != seed + 1:
        raise ValueError('Fixture score observation is incomplete')
    observations = receipt.get('observations')
    if not isinstance(observations, list) or len(observations) != 2:
        raise ValueError('Restart and replacement observations are both required')
    safe = []
    for item, operation in zip(observations, ('restart', 'container_replacement'), strict=True):
        if not isinstance(item, dict):
            raise TypeError('Invalid session observation')
        for key in ('before_container_id', 'after_container_id'):
            if not isinstance(item.get(key), str) or not re.fullmatch(r'[a-f0-9]{64}', item[key]):
                raise ValueError('Container identity is missing')
        for key in ('before_started_at', 'after_started_at'):
            try:
                if datetime.fromisoformat(item[key]).tzinfo is None:
                    raise ValueError('Missing timezone')
            except (KeyError, TypeError, ValueError):
                raise ValueError('Runtime generation time is invalid') from None
        taps = item.get('before_taps')
        duration = item.get('recovery_seconds')
        if (item.get('operation') != operation or item.get('old_connection') != 'closed'
                or item.get('reconnect') != 'passed' or item.get('session_continuity') != 'lost'
                or item.get('memory_state') != 'reset' or item.get('persisted_scoreboard') != 'unchanged'
                or type(item.get('close_code')) is not int or item['close_code'] != 1006
                or item.get('clean_close') is not False or type(item.get('before_round')) is not int
                or item['before_round'] < 1 or item.get('after_round') != 0
                or not isinstance(taps, dict) or set(taps) != {'A', 'B'}
                or any(type(value) is not int or value < 0 for value in taps.values()) or sum(taps.values()) < 1
                or item.get('after_taps') != {'A': 0, 'B': 0}
                or type(duration) not in (int, float) or not 0 <= duration <= 120
                or datetime.fromisoformat(item['before_started_at']) >=
                datetime.fromisoformat(item['after_started_at'])
                or timestamp < datetime.fromisoformat(item['after_started_at'])
                or (item['before_container_id'] == item['after_container_id']) != (operation == 'restart')):
            raise ValueError('Session observation is inconsistent')
        safe.append({key: deepcopy(item[key]) for key in (
            'operation', 'old_connection', 'close_code', 'clean_close', 'reconnect', 'session_continuity',
            'memory_state', 'persisted_scoreboard', 'before_round', 'after_round', 'before_taps', 'after_taps',
            'recovery_seconds', 'before_container_id', 'after_container_id', 'before_started_at', 'after_started_at')})
    observed = {key: deepcopy(receipt[key]) for key in (
        'protocol', 'scope', 'drill_status', 'session_continuity', 'image', 'image_id', 'room_fixture',
        'checked_at', 'seed_rounds', 'persisted_rounds')}
    observed['observations'] = safe
    return {'protocol': 'sky-bound-session-rehearsal-v1', 'binding': binding,
            'receipt_sha256': hashlib.sha256(json.dumps(observed, sort_keys=True,
                                                      separators=(',', ':')).encode()).hexdigest(),
            'receipt': observed}


def recorded_session_rehearsal(job: dict) -> dict | None:
    record = job.get('websocket_session_rehearsal')
    if not isinstance(record, dict):
        return None
    try:
        expected = bind_session_rehearsal(job, record.get('receipt'))
    except (ValueError, TypeError):
        return None
    return deepcopy(expected) if record == expected else None
