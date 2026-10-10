"""Opt-in, single-task AWS game session drill with no application score writes.

Read-only by default. --apply replaces one owned idle service task. This measures
connection loss/rejoin and persisted scoreboard values, not game round continuity,
version updates, rollback, draining, or source-to-runtime revision provenance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from adapters.aws.ecs import AwsExpressAdapter
from tests.live.smoke_game_sessions import event_line


class TaskNotReady(ValueError):
    pass


def owned_runtime(ecs, account, region, service):
    if not re.fullmatch(r'sky-[a-f0-9]{16}-a[1-3]', service):
        raise ValueError('Invalid Sky service name')
    arn = f'arn:aws:ecs:{region}:{account}:service/default/{service}'
    described = ecs.describe_express_gateway_service(serviceArn=arn, include=['TAGS'])['service']
    tags = {item['key']: item['value'] for item in described.get('tags', [])}
    configurations = described.get('activeConfigurations', [])
    if (described.get('serviceArn') != arn or described.get('status', {}).get('statusCode') != 'ACTIVE'
            or tags.get('sky-managed') != 'true' or tags.get('sky-attempt') != service.removeprefix('sky-')
            or described.get('currentDeployment') or len(configurations) != 1):
        raise ValueError('Service ownership or settled deployment is not confirmed')
    config = configurations[0]
    image = config.get('primaryContainer', {}).get('image')
    expected = f'{account}.dkr.ecr.{region}.amazonaws.com/sky-managed:' + service.removeprefix('sky-')
    if image != expected:
        raise ValueError('Current service image is not owned by this attempt')
    paths = [item['endpoint'] for item in config.get('ingressPaths', [])
             if item.get('accessType') == 'PUBLIC' and isinstance(item.get('endpoint'), str)]
    if len(paths) != 1:
        raise ValueError('Expected one public owned game URL')
    url = paths[0] if paths[0].startswith('https://') else 'https://' + paths[0]
    AwsExpressAdapter.validate_url(url, service, region)
    listing = ecs.list_tasks(cluster='default', serviceName=service, desiredStatus='RUNNING')
    arns = listing.get('taskArns', [])
    if listing.get('nextToken'):
        raise ValueError('Task listing is incomplete')
    if len(arns) != 1:
        raise TaskNotReady('Expected exactly one running task')
    task_prefix = f'arn:aws:ecs:{region}:{account}:task/default/'
    if not arns[0].startswith(task_prefix) or not re.fullmatch(r'[a-f0-9]{32}', arns[0][len(task_prefix):]):
        raise ValueError('Task belongs to a different account or cluster')
    response = ecs.describe_tasks(cluster='default', tasks=arns)
    tasks = response.get('tasks', [])
    if response.get('failures') or len(tasks) != 1:
        raise ValueError('Task description is incomplete')
    task = tasks[0]
    containers = [item for item in task.get('containers', []) if item.get('image') == image]
    if (task.get('taskArn') != arns[0] or task.get('group') != 'service:' + service
            or task.get('clusterArn') != f'arn:aws:ecs:{region}:{account}:cluster/default'
            or task.get('taskDefinitionArn') != config.get('taskDefinitionArn') or len(containers) != 1):
        raise ValueError('Task ownership or release differs')
    if (task.get('lastStatus') != 'RUNNING' or task.get('desiredStatus') != 'RUNNING'
            or containers[0].get('lastStatus') != 'RUNNING'):
        raise TaskNotReady('Service task is not running yet')
    digest = containers[0].get('imageDigest')
    if not isinstance(digest, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
        raise ValueError('Running image digest is missing')
    return {'service_arn': arn, 'image': image, 'image_digest': digest, 'task_arn': arns[0],
            'task_definition_arn': task['taskDefinitionArn'], 'url': url,
            'account': account, 'region': region}


def game_json(url, path):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(url + path, timeout=5) as response:
        if response.status != 200:
            raise ValueError('Game returned a non-200 response')
        result = json.loads(response.read(256 * 1024 + 1))
    if not isinstance(result, dict):
        raise TypeError('Invalid game JSON response')
    return result


WATCH = r"""
const crypto = require('node:crypto');
const ws = new WebSocket(process.argv[1].replace('https:', 'wss:') + '/ws');
let ready = false;
const timer = setTimeout(() => { ws.close(); process.exit(3); }, 1000000);
ws.addEventListener('open', () => ws.send(JSON.stringify({type:'join'})));
ws.addEventListener('message', event => {
  const m = JSON.parse(event.data);
  if (m.type !== 'welcome' || ready) return;
  ready = true;
  console.log(JSON.stringify({event:'ready', session_hash:crypto.createHash('sha256').update(m.id).digest('hex'),
                             phase:m.phase, round:m.round}));
});
ws.addEventListener('close', event => {
  clearTimeout(timer);
  console.log(JSON.stringify({event:'closed', code:event.code, clean:event.wasClean}));
  process.exit(ready ? 0 : 4);
});
ws.addEventListener('error', () => {});
"""


def drill(ecs, account, region, service, *, apply=False):
    before = owned_runtime(ecs, account, region, service)
    url = before['url']
    board = game_json(url, '/api/scoreboard')
    stats = game_json(url, '/stats')
    if stats.get('connections') != {'players': 0, 'others': 0} or stats.get('phase') != 'waiting':
        raise ValueError('Existing players or active game block this drill')
    if not apply:
        return {'scope': 'aws-read-only-preflight', 'runtime': before, 'rounds': board.get('rounds')}
    observer = subprocess.Popen(['node', '-e', WATCH, url], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
    reconnect = None
    try:
        old_session = event_line(observer, 15)
        if old_session.get('event') != 'ready':
            raise ValueError('Game player session was not established')
        # Recheck immediately before the only mutation. Any joined outsider blocks it.
        current = owned_runtime(ecs, account, region, service)
        stats = game_json(url, '/stats')
        if current != before or stats.get('connections') != {'players': 1, 'others': 0}:
            raise ValueError('Service or players changed during the preflight')
        if stats.get('phase') != 'waiting' or game_json(url, '/api/scoreboard') != board:
            raise ValueError('Game became active during the preflight')
        started = time.monotonic()
        stopped = ecs.stop_task(cluster='default', task=before['task_arn'],
                                reason='Sky owned idle game P7 session evidence drill')['task']
        if stopped.get('taskArn') != before['task_arn']:
            raise ValueError('Stop response task identity differs')
        deadline = started + 900
        closed = after = None
        close_seconds = recovery_seconds = None
        while time.monotonic() < deadline and (closed is None or after is None):
            if closed is None:
                try:
                    candidate = event_line(observer, 0.05)
                except TimeoutError:
                    candidate = None
                if candidate is not None:
                    if candidate.get('event') != 'closed':
                        raise ValueError('Unexpected old connection observation')
                    closed = candidate
                    close_seconds = time.monotonic() - started
            if after is None:
                try:
                    candidate = owned_runtime(ecs, account, region, service)
                    if candidate['task_arn'] != before['task_arn']:
                        if any(candidate[key] != before[key] for key in (
                                'service_arn', 'image', 'image_digest', 'task_definition_arn', 'url')):
                            raise ValueError('Replacement release or image differs')
                        after_board = game_json(url, '/api/scoreboard')
                        after = candidate
                        recovery_seconds = time.monotonic() - started
                except (TaskNotReady, OSError):
                    pass
            if closed is None or after is None:
                time.sleep(3)
        if closed is None or after is None:
            raise TimeoutError('Task HTTP recovery or old connection closure exceeded 15 minutes')
        observer.wait(timeout=5)
        if observer.returncode:
            raise ValueError('Old connection observer failed')
        if after_board != board:
            raise ValueError('Full scoreboard values changed across task replacement')
        reconnect = subprocess.Popen(['node', '-e', WATCH, url], stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
        new_session = event_line(reconnect, 15)
        if (new_session.get('event') != 'ready'
                or new_session.get('session_hash') == old_session.get('session_hash')):
            raise ValueError('A newly issued player session was not confirmed')
        return {'protocol': 'sky-aws-game-session-v1', 'scope': 'owned-aws-task-replacement',
                'checked_at': datetime.now(UTC).isoformat(), 'before': before, 'after': after,
                'old_connection': 'closed', 'close_code': closed['code'], 'clean_close': closed['clean'],
                'reconnect': 'passed', 'session_continuity': 'lost', 'new_player_session': True,
                'persisted_scoreboard': 'unchanged', 'rounds': board['rounds'],
                'scoreboard_sha256': hashlib.sha256(json.dumps(
                    board, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                'close_seconds': round(close_seconds, 3),
                'http_recovery_seconds': round(recovery_seconds, 3),
                'verification_seconds': round(time.monotonic() - started, 3),
                'unverified': ['playing-round memory state', 'version update', 'rollback',
                               'session drain', 'uploaded source revision binding']}
    finally:
        for process in (observer, reconnect):
            if process and process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service', required=True)
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--receipt', type=Path)
    args = parser.parse_args()
    import boto3
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    if session.client('sts').get_caller_identity()['Account'] != args.account:
        raise ValueError('AWS account differs from the explicit pin')
    ecs = session.client('ecs')
    journal = args.receipt.with_suffix('.journal.json') if args.receipt else None
    if journal:
        preflight = drill(ecs, args.account, args.region, args.service)
        journal.write_text(json.dumps({'status': 'planned', 'apply': args.apply,
                                      'preflight': preflight}, indent=2) + '\n')
    try:
        result = drill(ecs, args.account, args.region, args.service, apply=args.apply)
    except Exception as error:
        if journal:
            journal.write_text(json.dumps({'status': 'needs_attention', 'apply': args.apply,
                                          'preflight': preflight, 'failure_type': type(error).__name__},
                                         indent=2) + '\n')
        raise
    if args.receipt:
        args.receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        journal.write_text(json.dumps({'status': 'completed', 'apply': args.apply,
                                      'preflight': preflight, 'receipt': str(args.receipt)}, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False))
