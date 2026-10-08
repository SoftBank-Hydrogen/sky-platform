"""Manual same-host Compose smoke for the real team game ZIP.

This exercises Compose itself; it does not claim Sky exposes a Compose target yet.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from adapters.local.docker import LocalDockerAdapter
from application.deployment_core import extract_project
from application.infrastructure import inspect_infrastructure
from application.local_sqlite import preflight_local_sqlite
from application.websocket_probe import probe_sky_game


def command(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise RuntimeError(f"{' '.join(args[:3])} failed: {result.stderr[-1500:]}")
    return result.stdout.strip()


def wait_for_game(url: str) -> dict:
    last_error = None
    for _ in range(30):
        try:
            with urllib.request.urlopen(url + '/health', timeout=2) as response:
                if response.status != 200:
                    raise AssertionError(response.status)
            with urllib.request.urlopen(url + '/api/scoreboard', timeout=2) as response:
                return json.load(response)
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
            time.sleep(1)
    raise AssertionError(f'Compose game did not become ready: {last_error}')


def main() -> None:
    archive = Path(__file__).resolve().parents[3] / 'demo-game' / 'TUG-Sky-almostfinaltest.zip'
    suffix = uuid.uuid4().hex[:12]
    application_id = 'compose-' + suffix
    project_name = 'sky-' + application_id
    container_name = project_name + '-app'
    image = 'sky/' + application_id + ':smoke'
    adapter = LocalDockerAdapter(lambda *_: None)
    host_port = adapter.available_loopback_port()
    url = f'http://127.0.0.1:{host_port}'
    with tempfile.TemporaryDirectory(prefix='sky-compose-smoke-') as temporary:
        root = Path(temporary)
        source = extract_project(archive, root / 'source')
        binding = preflight_local_sqlite(source, inspect_infrastructure(source), application_id, '/app/data')
        adapter.sqlite_binding = binding
        adapter.prepare_sqlite_volume()
        compose_file = root / 'compose.json'
        compose_file.write_text(json.dumps({
            'services': {'app': {
                'build': {'context': str(source), 'dockerfile': 'Dockerfile',
                          'labels': {'app': 'sky', 'sky-smoke': application_id}},
                'image': image, 'container_name': container_name,
                'labels': {'app': 'sky', 'sky-smoke': application_id},
                'network_mode': 'bridge', 'ports': [f'127.0.0.1:{host_port}:8080'],
                'environment': {'PORT': '8080'},
                'volumes': [f"{binding['volume_name']}:/app/data"],
                'restart': 'unless-stopped', 'mem_limit': '256m', 'cpus': 1,
                'pids_limit': 128, 'cap_drop': ['ALL'],
                'security_opt': ['no-new-privileges:true'],
            }},
            'volumes': {binding['volume_name']: {'external': True}},
        }), encoding='utf-8')
        compose = ['docker', 'compose', '-p', project_name, '-f', str(compose_file)]
        started = False
        try:
            command(*compose, 'config', '-q')
            started = True
            command(*compose, 'up', '-d', '--build')
            container = adapter.inspect_resource('container', container_name)
            labels = (container or {}).get('Config', {}).get('Labels') or {}
            if (not container or labels.get('sky-smoke') != application_id
                    or labels.get('com.docker.compose.project') != project_name):
                raise AssertionError('Compose container ownership was not verified')
            initial = wait_for_game(url)['rounds']
            if initial != 13:
                raise AssertionError(f'Expected 13 seeded rounds, got {initial}')
            if probe_sky_game(url)['status'] != 'passed':
                raise AssertionError('WebSocket probe failed')
            script = ("const db=require('./db').openScores();"
                      "db.saveRound({startedAt:1,endedAt:2,winner:'A',"
                      "taps:{A:3,B:1},players:{A:1,B:1}});db.close()")
            command(*compose, 'exec', '-T', 'app', 'node', '-e', script)
            if wait_for_game(url)['rounds'] != initial + 1:
                raise AssertionError('New score was not stored')
            command(*compose, 'restart', 'app')
            if wait_for_game(url)['rounds'] != initial + 1:
                raise AssertionError('Score did not survive Compose restart')
            if probe_sky_game(url)['status'] != 'passed':
                raise AssertionError('WebSocket failed after Compose restart')
            print(json.dumps({'target': 'same-host-compose', 'http': 'passed',
                              'websocket': 'passed', 'seed_rows': initial,
                              'rows_after_restart': initial + 1, 'url_stable': True},
                             ensure_ascii=False))
        finally:
            if started:
                container = adapter.inspect_resource('container', container_name)
                labels = (container or {}).get('Config', {}).get('Labels') or {}
                if not container or labels.get('sky-smoke') == application_id:
                    command(*compose, 'down', '--remove-orphans')
            image_info = adapter.inspect_resource('image', image)
            if image_info and (image_info.get('Config', {}).get('Labels') or {}).get('sky-smoke') == application_id:
                command('docker', 'image', 'rm', image)
            volume = adapter.inspect_resource('volume', binding['volume_name'])
            if volume and (volume.get('Labels') or {}).get('sky-application') == application_id:
                command('docker', 'volume', 'rm', binding['volume_name'])


if __name__ == '__main__':
    main()
