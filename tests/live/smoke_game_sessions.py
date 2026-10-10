"""Measure real game connection/state loss on disposable local Docker copies.

Never restarts a user's existing container. The selected image must export the
Sky game createTugServer/openScores contracts. AWS/update/rollback are not covered.
"""
from __future__ import annotations

import argparse
import json
import selectors
import subprocess
import time
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

from adapters.local.docker import LocalDockerAdapter
from tests.live.smoke_sqlite_migration import docker

WATCH = r"""
const endpoint = process.argv[1];
const ws = new WebSocket(endpoint.replace('http:', 'ws:') + '/ws');
let welcomed = false, sent = false, reported = false;
const timer = setTimeout(() => { ws.close(); process.exit(3); }, 45000);
ws.addEventListener('open', () => ws.send(JSON.stringify({type:'join'})));
ws.addEventListener('message', event => {
  const m = JSON.parse(event.data);
  if (m.type === 'welcome') welcomed = true;
  if (m.type === 'state' && m.phase === 'playing' && !sent) {
    sent = true; ws.send(JSON.stringify({type:'tap', n:3}));
  }
  if (m.type === 'state' && welcomed && !reported && m.taps.A + m.taps.B > 0) {
    reported = true;
    console.log(JSON.stringify({event:'ready', phase:m.phase, round:m.round, taps:m.taps}));
  }
});
ws.addEventListener('close', event => {
  clearTimeout(timer);
  console.log(JSON.stringify({event:'closed', code:event.code, clean:event.wasClean}));
  process.exit(reported ? 0 : 4);
});
ws.addEventListener('error', () => {});
"""
REJOIN = r"""
const endpoint = process.argv[1];
const ws = new WebSocket(endpoint.replace('http:', 'ws:') + '/ws');
const timer = setTimeout(() => process.exit(3), 10000);
ws.addEventListener('open', () => ws.send(JSON.stringify({type:'join'})));
ws.addEventListener('message', event => {
  const m = JSON.parse(event.data);
  if (m.type !== 'welcome') return;
  console.log(JSON.stringify({phase:m.phase, round:m.round, taps:m.taps}));
  clearTimeout(timer); ws.close(); process.exit(0);
});
ws.addEventListener('error', () => process.exit(4));
"""


def game_json(url, path):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url + path, timeout=2) as response:
        return json.load(response)


def wait_for_game(url):
    for _ in range(30):
        try:
            return game_json(url, '/api/scoreboard')
        except (OSError, ValueError):
            time.sleep(1)
    raise TimeoutError('Disposable game did not become ready')


def event_line(process, timeout):
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        if not selector.select(timeout):
            raise TimeoutError('Game session observation timed out')
        line = process.stdout.readline()
    if not line:
        raise AssertionError('Game session observer exited before recording an event')
    return json.loads(line)


def measure(image):
    suffix = uuid.uuid4().hex[:12]
    name, volume = 'sky-session-' + suffix, 'sky-session-data-' + suffix
    port = LocalDockerAdapter.available_loopback_port()
    url = f'http://127.0.0.1:{port}'
    image_id = docker('image', 'inspect', '--format', '{{.Id}}', image)
    launch = ("require('./server').createTugServer({room:{minPlayers:1,countdownMs:100,"
              "roundMs:30000}}).listen(8080,'0.0.0.0')")
    created_volume = created_container = False
    observer = None

    def start():
        nonlocal created_container
        # Docker run can create the named container even when startup fails.
        created_container = True
        docker('run', '-d', '--name', name, '--label', 'sky-session-drill=' + suffix,
               '--memory', '256m', '--cpus', '1', '--pids-limit', '128', '--cap-drop', 'ALL',
               '--security-opt', 'no-new-privileges', '-p', f'127.0.0.1:{port}:8080',
               '-v', volume + ':/app/data', image, 'node', '--experimental-sqlite', '-e', launch)

    try:
        docker('volume', 'create', '--label', 'sky-session-drill=' + suffix, volume)
        created_volume = True
        start()
        initial = wait_for_game(url)['rounds']
        # Seed exactly one fixture row in the disposable copy, never user data.
        docker('exec', name, 'node', '--experimental-sqlite', '-e',
               "const db=require('./db').openScores();db.saveRound({startedAt:1,endedAt:2,"
               "winner:'A',taps:{A:3,B:1},players:{A:1,B:1}});db.close()")
        expected = game_json(url, '/api/scoreboard')
        if expected['rounds'] != initial + 1:
            raise AssertionError('Fixture score was not persisted')
        observations = []
        for operation in ('restart', 'container_replacement'):
            before_runtime = json.loads(docker('inspect', name))[0]
            observer = subprocess.Popen(['node', '-e', WATCH, url], stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True)
            before = event_line(observer, 15)
            if before.get('event') != 'ready' or before.get('phase') != 'playing':
                raise AssertionError('Game was not playing before the transition')
            started = time.monotonic()
            if operation == 'restart':
                docker('restart', '-t', '1', name, timeout=60)
            else:
                docker('rm', '-f', name)
                created_container = False
                start()
            closed = event_line(observer, 15)
            observer.wait(timeout=5)
            if closed.get('event') != 'closed' or observer.returncode:
                raise AssertionError('Old WebSocket connection termination was not observed')
            after_board = wait_for_game(url)
            after_runtime = json.loads(docker('inspect', name))[0]
            if (before_runtime['State']['StartedAt'] == after_runtime['State']['StartedAt']
                    or before_runtime['Image'] != image_id or after_runtime['Image'] != image_id):
                raise AssertionError('Process restart or execution image identity was not confirmed')
            if operation == 'container_replacement' and before_runtime['Id'] == after_runtime['Id']:
                raise AssertionError('Container replacement was not confirmed')
            joined = subprocess.run(['node', '-e', REJOIN, url], capture_output=True, text=True,
                                    check=True, timeout=15)
            after = json.loads(joined.stdout)
            if after['round'] != 0 or after['taps'] != {'A': 0, 'B': 0}:
                raise AssertionError('Expected process-local game reset was not observed')
            if after_board != expected:
                raise AssertionError('Persisted scoreboard values changed across the transition')
            observations.append({'operation': operation, 'old_connection': 'closed',
                'close_code': closed['code'], 'clean_close': closed['clean'],
                'reconnect': 'passed', 'session_continuity': 'lost',
                'memory_state': 'reset', 'persisted_scoreboard': 'unchanged',
                'before_round': before['round'], 'after_round': after['round'],
                'before_taps': before['taps'], 'after_taps': after['taps'],
                'before_container_id': before_runtime['Id'], 'after_container_id': after_runtime['Id'],
                'before_started_at': before_runtime['State']['StartedAt'],
                'after_started_at': after_runtime['State']['StartedAt'],
                'recovery_seconds': round(time.monotonic() - started, 3)})
            observer = None
        return {'protocol': 'sky-game-session-drill-v1', 'checked_at': datetime.now(UTC).isoformat(),
                'scope': 'disposable-local-docker', 'image_id': image_id,
                'room_fixture': {'minPlayers': 1, 'countdownMs': 100, 'roundMs': 30000},
                'drill_status': 'passed', 'session_continuity': 'lost',
                'seed_rounds': initial, 'persisted_rounds': expected['rounds'],
                'observations': observations,
                'unverified': ['AWS task replacement', 'version update', 'rollback', 'session drain']}
    finally:
        if observer and observer.poll() is None:
            observer.terminate()
            observer.wait(timeout=5)
        if created_container:
            docker('rm', '-f', name)
        if created_volume:
            docker('volume', 'rm', volume)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--receipt', type=Path)
    args = parser.parse_args()
    result = measure(args.image)
    if args.receipt:
        args.receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False))
