"""Real Sky intake/agent/Docker game smoke with deterministic offline agent calls."""
import io
import json
import subprocess
import tempfile
import unittest.mock
import urllib.request
import uuid
from pathlib import Path

from adapters.local.docker import LocalDockerAdapter
from application.analysis import AISettings
from application.certificate import deployment_certificate
from interfaces.http.server import App, handler_for


class OfflineAgent:
    def __init__(self):
        self.index = 0
        self.actions = [
            ('read_project_files', {'paths': ['Dockerfile', 'package.json', 'server.js', 'db.js']}),
            ('configure_deployment', {'start_script': 'dockerfile', 'build_script': None,
                                      'port': 8080, 'health_path': '/health', 'required_env': []}),
            ('deploy_application', {}),
        ]
    def next(self, history):
        if self.index >= len(self.actions):
            raise AssertionError('Offline agent exceeded planned calls')
        name, arguments = self.actions[self.index]
        self.index += 1
        return [{'type': 'function_call', 'call_id': f'offline-{self.index}',
                 'name': name, 'arguments': json.dumps(arguments)}]


def main() -> None:
    application_id = 'offline-' + uuid.uuid4().hex[:16]
    archive = Path(__file__).resolve().parents[3] / 'demo-game' / 'TUG-Sky-almostfinaltest.zip'
    payload = archive.read_bytes()
    with tempfile.TemporaryDirectory(prefix='sky-p0-offline-') as temporary:
        app = App(Path(temporary), AISettings('offline-fixture', 'offline-fixture'),
                  agent_factory=lambda _: OfflineAgent(), monitor_interval=0, github_poll_interval=0)
        handler = handler_for(app).__new__(handler_for(app))
        handler.path = '/api/deployments'
        handler.headers = {'X-Sky-Token': app.token, 'X-Deploy-Target': 'local-docker',
                           'X-Application-Id': application_id, 'X-Local-Sqlite-Mount': '/app/data',
                           'Content-Length': str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        handler.json_response = unittest.mock.Mock()
        with unittest.mock.patch('interfaces.http.server.threading.Thread.start'):
            handler.do_POST()
        status, response = handler.json_response.call_args.args
        if status != 202:
            raise AssertionError((status, response))
        job_id = response['id']
        try:
            app.run_agent(job_id)
            job = app.jobs[job_id]
            if job['status'] != 'succeeded':
                print('failed_events', [(e.get('stage'), e.get('message', '')[:240])
                                        for e in job.get('events', [])[-8:]])
                raise AssertionError('Sky job did not succeed')
            with urllib.request.urlopen(job['result']['url'] + '/api/scoreboard', timeout=5) as response:
                scoreboard = json.load(response)
            assert scoreboard['rounds'] == 13, scoreboard
            assert app.check_and_record_health(job_id)['healthy']
            websocket = app.check_and_record_websocket(job_id)
            report = deployment_certificate(app.jobs[job_id])
            assert websocket['status'] == 'passed', websocket
            checks = {item['name']: item['status'] for item in report['verification']}
            assert checks['local_sqlite_mount'] == 'passed', checks
            assert checks['websocket_round_trip'] == 'passed', checks
            assert checks['data_persistence_after_restart'] == 'unverified', checks
            print(json.dumps({'job_id': job_id, 'status': job['status'], 'url': job['result']['url'],
                              'score_rows': scoreboard['rounds'], 'checks': {key: checks[key] for key in
                                         ('deployment_http', 'websocket_round_trip',
                                          'local_sqlite_mount', 'data_persistence_after_restart')}},
                              ensure_ascii=False))
        finally:
            job = app.jobs[job_id]
            if job.get('status') == 'succeeded' and job.get('result'):
                app.retire_local(job_id)
                assert app.jobs[job_id]['deployment_state'] == 'deleted'
            binding = job['local_sqlite_binding']
            adapter = LocalDockerAdapter(lambda *_: None, sqlite_binding=binding)
            volume = adapter.inspect_resource('volume', binding['volume_name'])
            if volume and (volume.get('Labels') or {}).get('sky-application') == application_id:
                attached = adapter.command(['docker', 'ps', '-a', '-q', '--filter',
                                            'volume=' + binding['volume_name']], quiet=True)
                if not attached:
                    subprocess.run(['docker', 'volume', 'rm', binding['volume_name']], check=True)


if __name__ == "__main__":
    main()
