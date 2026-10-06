"""Run a cloud-bound OCI image locally before it is uploaded."""
from __future__ import annotations

import json
import re
import tempfile
import time
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_IMAGE_ID = re.compile(r'sha256:[a-f0-9]{64}')
_LOOPBACK_PORT = re.compile(r'127\.0\.0\.1:([1-9][0-9]{0,4})')


def inspect_image_id(command, image: str) -> str:
    image_id = command(['docker', 'image', 'inspect', '--format', '{{.Id}}', image],
                       timeout=30, quiet=True).strip()
    if not _IMAGE_ID.fullmatch(image_id):
        raise RuntimeError('로컬 이미지 ID를 확인할 수 없습니다.')
    return image_id


def rehearse_image(command, event, image: str, plan, attempt_id: str,
                   environment: dict[str, str]) -> dict:
    """Probe one bounded loopback container and remove it before cloud upload."""
    if not re.fullmatch(r'[a-f0-9]{16}-a[1-3]', attempt_id):
        raise ValueError('리허설 시도 ID가 올바르지 않습니다.')
    image_id = inspect_image_id(command, image)
    container = 'sky-rehearsal-' + attempt_id
    args = ['docker', 'run', '-d', '--name', container, '--platform', 'linux/amd64',
            '--label', 'app=sky', '--label', 'sky-attempt=' + attempt_id,
            '--memory', '256m', '--cpus', '1', '--pids-limit', '128', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '-e', 'PORT=' + str(plan.port),
            '-p', '127.0.0.1::' + str(plan.port)]
    created = False
    try:
        if environment:
            with tempfile.NamedTemporaryFile(mode='w', prefix='sky-rehearsal-env-',
                                             encoding='utf-8') as env_file:
                for name, value in environment.items():
                    env_file.write(name + '=' + value + '\n')
                env_file.flush()
                command(args + ['--env-file', env_file.name, image], timeout=60, private=True)
        else:
            command(args + [image], timeout=60, private=True)
        created = True
        binding = command(['docker', 'port', container, str(plan.port) + '/tcp'],
                          timeout=30, quiet=True).strip()
        match = _LOOPBACK_PORT.fullmatch(binding)
        if not match or int(match.group(1)) > 65535:
            raise RuntimeError('리허설 컨테이너의 루프백 포트를 확인할 수 없습니다.')
        url = 'http://' + binding + plan.health_path
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        event('rehearsal', '동일 이미지를 로컬에서 실행해 HTTP 응답을 확인합니다.')
        for _ in range(60):
            try:
                with opener.open(url, timeout=2) as response:
                    if response.status == 200:
                        if inspect_image_id(command, image) != image_id:
                            raise RuntimeError('리허설 도중 이미지가 변경됐습니다.')
                        event('rehearsal', '로컬 HTTP 리허설 통과')
                        return {'status': 'passed', 'image_id': image_id,
                                'platform': 'linux/amd64', 'health_path': plan.health_path}
            except (OSError, urllib.error.HTTPError):
                pass
            time.sleep(1)
        command(['docker', 'logs', '--tail', '80', container], timeout=30)
        raise RuntimeError('로컬 리허설에서 HTTP 200 응답을 받지 못했습니다.')
    finally:
        if created:
            command(['docker', 'rm', '-f', container], timeout=30, quiet=True)
        else:
            # Docker may create a named container even when `run` returns an error.
            try:
                labels = json.loads(command(['docker', 'inspect', '--format',
                                              '{{json .Config.Labels}}', container],
                                             timeout=15, quiet=True))
            except Exception:
                labels = None
            if isinstance(labels, dict) and labels.get('app') == 'sky' \
                    and labels.get('sky-attempt') == attempt_id:
                command(['docker', 'rm', '-f', container], timeout=30, quiet=True)
