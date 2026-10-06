"""Cloud Run execution adapter. Cloud identity stays outside model context."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from planning.analysis import redact
from deployment.core import ImageBuilder, validate_environment


class CloudConfigurationError(RuntimeError):
    retryable = False


@dataclass(frozen=True)
class CloudRunSettings:
    project: str = ""
    region: str = ""
    repository: str = "sky"
    service_account: str = ""

    @classmethod
    def from_environment(cls):
        return cls(os.getenv('SKY_GCP_PROJECT', ''), os.getenv('SKY_GCP_REGION', ''),
                   os.getenv('SKY_GCP_REPOSITORY', 'sky'), os.getenv('SKY_GCP_SERVICE_ACCOUNT', ''))

    @property
    def runtime_identity(self):
        return self.service_account or f'sky-runtime@{self.project}.iam.gserviceaccount.com'

    def validate(self):
        if not re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]', self.project):
            raise CloudConfigurationError('SKY_GCP_PROJECT에 유효한 프로젝트 ID를 설정하세요.')
        if not re.fullmatch(r'[a-z]+-[a-z]+[0-9]+', self.region):
            raise CloudConfigurationError('SKY_GCP_REGION에 리전을 설정하세요.')
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', self.repository):
            raise CloudConfigurationError('Invalid Artifact Registry repository name')
        if not re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]@' + re.escape(self.project) + r'\.iam\.gserviceaccount\.com', self.runtime_identity):
            raise CloudConfigurationError('Runtime service account must belong to the configured project')

    def unavailable_reason(self):
        try:
            self.validate()
            if not shutil.which('gcloud'):
                return 'gcloud CLI 설치와 로그인이 필요합니다.'
        except CloudConfigurationError as exc:
            return str(exc)
        return None


class CloudRunAdapter:
    def __init__(self, event, settings: CloudRunSettings, public=False):
        self.output = event
        self.settings, self.public = settings, public
        self.sensitive = []
        self.service_attempted = False
        self.pushed = False
        self.built = False
        self.image = None

    def event(self, stage, message):
        for value in sorted(set(self.sensitive), key=len, reverse=True):
            if value:
                message = message.replace(value, '[REDACTED]')
        self.output(stage, redact(message))

    def command(self, args, timeout=300, stdin=None, private=False):
        self.event('command', ' '.join(args))
        result = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=timeout)
        if not private:
            if result.stdout:
                self.event('output', result.stdout[-12000:])
            if result.stderr:
                self.event('output', result.stderr[-12000:])
        if result.returncode:
            error = result.stderr.lower()
            message = f'{args[0]} command failed (exit {result.returncode})'
            if any(word in error for word in ('permission_denied', 'permission denied', 'unauthenticated', 'billing', 'not have permission')):
                raise CloudConfigurationError('클라우드 인증·권한·결제 설정을 확인하세요. ' + message)
            raise RuntimeError(message)
        return result.stdout.strip()

    def gcloud(self, args, **kwargs):
        return self.command(['gcloud', *args, '--project', self.settings.project, '--quiet'], **kwargs)

    def prepare_infrastructure(self):
        self.settings.validate()
        if not shutil.which('gcloud'):
            raise CloudConfigurationError('gcloud CLI 설치와 로그인이 필요합니다.')
        self.event('infrastructure', 'Cloud Run API, 이미지 저장소, 실행 서비스 계정 준비')
        try:
            self.gcloud(['services', 'enable', 'run.googleapis.com', 'artifactregistry.googleapis.com', 'iam.googleapis.com'])
            repos = json.loads(self.gcloud(['artifacts', 'repositories', 'list', '--location', self.settings.region, '--format=json'], private=True))
            existing = next((r for r in repos if r['name'].endswith('/' + self.settings.repository)), None)
            if existing and existing.get('format') != 'DOCKER':
                raise CloudConfigurationError('Configured Artifact Registry repository is not Docker format')
            if not existing:
                self.gcloud(['artifacts', 'repositories', 'create', self.settings.repository,
                             '--repository-format=docker', '--location', self.settings.region])
            accounts = json.loads(self.gcloud(['iam', 'service-accounts', 'list', '--format=json'], private=True))
            if not any(a.get('email') == self.settings.runtime_identity for a in accounts):
                if self.settings.service_account:
                    raise CloudConfigurationError('Configured runtime service account does not exist')
                self.gcloud(['iam', 'service-accounts', 'create', 'sky-runtime', '--display-name=Sky runtime'])
        except CloudConfigurationError:
            raise
        except Exception as exc:
            raise CloudConfigurationError('클라우드 기반 리소스 준비 실패: ' + str(exc)) from None

    def deploy(self, project, plan, attempt_id, environment=None):
        if not re.fullmatch(r'[a-f0-9]{16}-a[1-3]', attempt_id):
            raise ValueError('Invalid deployment attempt ID')
        if plan.target != 'cloud-run':
            raise ValueError('Cloud Run adapter requires a cloud-run plan')
        environment = validate_environment(environment, plan.required_env)
        self.sensitive.extend(environment.values())
        self.prepare_infrastructure()
        service = f'sky-{attempt_id}'
        registry = f'{self.settings.region}-docker.pkg.dev'
        self.image = f'{registry}/{self.settings.project}/{self.settings.repository}/{service}:latest'
        # Refuse to modify a service that existed before this attempt.
        existing = json.loads(self.gcloud(['run', 'services', 'list', '--region', self.settings.region,
                                         '--filter', f'metadata.name={service}', '--format=json'], private=True))
        if existing:
            raise CloudConfigurationError('Deployment service already exists; refusing to overwrite it')
        ImageBuilder(self.command, self.event).build(project, plan, self.image, platform='linux/amd64')
        self.built = True
        try:
            self.event('uploading', '빌드한 이미지를 Artifact Registry에 업로드합니다.')
            token = self.gcloud(['auth', 'print-access-token'], private=True)
            if not token:
                raise CloudConfigurationError('No Google Cloud access token is available')
            self.sensitive.append(token)
            endpoint = os.getenv('DOCKER_HOST') or self.command(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'], private=True)
            with tempfile.TemporaryDirectory(prefix='sky-docker-auth-') as config:
                docker = ['docker', '--config', config, '--host', endpoint]
                self.command(docker + ['login', '-u', 'oauth2accesstoken', '--password-stdin', 'https://' + registry], stdin=token, private=True)
                # Push can partially succeed; cleanup should inspect the exact attempt image.
                self.pushed = True
                self.command(docker + ['push', self.image])
        except Exception as exc:
            raise CloudConfigurationError('이미지 업로드 실패: ' + str(exc)) from None
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', prefix='sky-cloud-env-', encoding='utf-8') as env_file:
            # JSON is also valid YAML; all values remain strings, including commas and equals signs.
            json.dump(environment, env_file)
            env_file.flush()
            args = ['run', 'deploy', service, '--image', self.image, '--region', self.settings.region,
                    '--port', str(plan.port), '--service-account', self.settings.runtime_identity,
                    '--cpu=1', '--memory=512Mi', '--min-instances=0', '--max-instances=1',
                    '--concurrency=20', '--timeout=60s', '--env-vars-file', env_file.name,
                    '--labels', f'sky-managed=true,sky-attempt={attempt_id}', '--format=json',
                    '--allow-unauthenticated' if self.public else '--no-allow-unauthenticated']
            self.service_attempted = True
            self.event('deploying', 'Cloud Run 서비스를 배포합니다.')
            try:
                deployed = json.loads(self.gcloud(args, timeout=600, private=True))
            except Exception:
                self.read_logs(service)
                raise
        url = deployed.get('status', {}).get('url', '')
        self.validate_url(url)
        identity_token = None
        if not self.public:
            try:
                identity_token = self.gcloud(['auth', 'print-identity-token'], private=True)
                if not identity_token:
                    raise RuntimeError('empty identity token')
                self.sensitive.append(identity_token)
            except Exception as exc:
                raise CloudConfigurationError('비공개 서비스 접속용 ID 토큰을 가져오지 못했습니다: ' + str(exc)) from None
        self.event('verifying', '실제 Cloud Run URL에서 HTTP 응답 확인')
        try:
            self.verify(url + plan.health_path, identity_token)
        except Exception:
            self.read_logs(service)
            raise
        return {'url': url, 'health_url': url + plan.health_path, 'service': service,
                'image': self.image, 'target': 'cloud-run', 'public': self.public,
                'project': self.settings.project, 'region': self.settings.region,
                'revision': deployed.get('status', {}).get('latestReadyRevisionName')}

    @staticmethod
    def validate_url(url):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != 'https' or not (parsed.hostname or '').endswith('.run.app')
                or parsed.username or parsed.password or parsed.port or parsed.path not in ('', '/')
                or parsed.query or parsed.fragment):
            raise CloudConfigurationError('Cloud Run returned an unexpected service URL')

    def verify(self, url, token):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(NoRedirect())
        headers = {'Authorization': 'Bearer ' + token} if token else {}
        for _ in range(12):
            try:
                with opener.open(urllib.request.Request(url, headers=headers), timeout=5) as response:
                    if response.status == 200:
                        return
            except urllib.error.HTTPError as exc:
                code = exc.code
                exc.close()
                if code in (401, 403):
                    raise CloudConfigurationError('Cloud Run 접속 권한 또는 공개 접근 설정을 확인하세요.') from None
            except OSError:
                pass
            time.sleep(2)
        raise RuntimeError('Cloud Run app did not return HTTP 200')

    def read_logs(self, service):
        try:
            self.gcloud(['logging', 'read', f'resource.type="cloud_run_revision" AND resource.labels.service_name="{service}"',
                         '--limit=30', '--freshness=15m', '--format=value(textPayload,jsonPayload.message)'], timeout=30)
        except Exception:
            self.event('logs', 'Cloud Run 실행 로그를 가져오지 못했습니다.')

    def retire(self, result: dict, attempt_id: str):
        self.settings.validate()
        if not re.fullmatch(r'[a-f0-9]{16}-a[1-3]', attempt_id):
            raise ValueError('Invalid Cloud Run deployment attempt')
        service = 'sky-' + attempt_id
        image_path = f'{self.settings.region}-docker.pkg.dev/{self.settings.project}/{self.settings.repository}/{service}'
        image = image_path + ':latest'
        if (result.get('service') != service or result.get('image') != image
                or result.get('project') != self.settings.project
                or result.get('region') != self.settings.region
                or result.get('target') != 'cloud-run'):
            raise ValueError('Stored Cloud Run resource identity is invalid')
        self.validate_url(result.get('url', ''))

        def listed_service():
            items = json.loads(self.gcloud(['run', 'services', 'list', '--region', self.settings.region,
                                           '--filter', f'metadata.name={service}', '--format=json'], private=True))
            if not isinstance(items, list):
                raise RuntimeError('Cloud Run service list is invalid')
            matches = [item for item in items if item.get('metadata', {}).get('name') == service]
            if len(matches) > 1:
                raise RuntimeError('Cloud Run service identity is ambiguous')
            return bool(matches)

        if listed_service():
            details = json.loads(self.gcloud(['run', 'services', 'describe', service,
                                              '--region', self.settings.region, '--format=json'], private=True))
            metadata = details.get('metadata', {})
            labels = metadata.get('labels', {})
            containers = details.get('spec', {}).get('template', {}).get('spec', {}).get('containers', [])
            if (metadata.get('name') != service or labels.get('sky-managed') != 'true'
                    or labels.get('sky-attempt') != attempt_id
                    or details.get('status', {}).get('url') != result['url']
                    or len(containers) != 1 or containers[0].get('image') != image):
                raise ValueError('Cloud Run service ownership changed; refusing deletion')
            self.event('retiring', '관리 Cloud Run 서비스를 삭제합니다: ' + service)
            self.gcloud(['run', 'services', 'delete', service, '--region', self.settings.region], timeout=180)
        for _ in range(30):
            if not listed_service():
                break
            time.sleep(2)
        else:
            raise RuntimeError('Cloud Run 서비스 삭제 완료를 확인하지 못했습니다.')

        def image_rows():
            rows = json.loads(self.gcloud(['artifacts', 'docker', 'images', 'list', image_path,
                                           '--include-tags', '--format=json'], private=True))
            if not isinstance(rows, list) or any(not isinstance(row, dict)
                                                 or row.get('package') != image_path
                                                 or not isinstance(row.get('tags'), list) for row in rows):
                raise RuntimeError('Artifact Registry 이미지 목록을 확인하지 못했습니다.')
            return rows

        tagged = [row for row in image_rows() if 'latest' in row['tags']]
        if len(tagged) > 1:
            raise RuntimeError('Artifact Registry 이미지 태그가 여러 버전에 연결돼 있습니다.')
        if tagged:
            self.event('retiring', '관리 Artifact Registry 이미지를 삭제합니다: ' + image)
            # Without --delete-tags, gcloud refuses to remove a version reused by other tags.
            self.gcloud(['artifacts', 'docker', 'images', 'delete', image], timeout=180)
            if any('latest' in row['tags'] for row in image_rows()):
                raise RuntimeError('Artifact Registry 이미지 태그 삭제를 확인하지 못했습니다.')

    def cleanup_failure(self, attempt_id):
        service = f'sky-{attempt_id}'
        service_absent = not self.service_attempted
        if self.service_attempted:
            try:
                services = json.loads(self.gcloud(['run', 'services', 'list', '--region', self.settings.region,
                                                  '--filter', f'metadata.name={service}', '--format=json'], private=True))
                matches = [item for item in services if item.get('metadata', {}).get('name') == service]
                if not matches:
                    service_absent = True
                elif len(matches) == 1:
                    labels = matches[0].get('metadata', {}).get('labels', {})
                    if labels.get('sky-attempt') == attempt_id and labels.get('sky-managed') == 'true':
                        self.gcloud(['run', 'services', 'delete', service, '--region', self.settings.region], timeout=120)
                        for _ in range(30):
                            remaining = json.loads(self.gcloud(['run', 'services', 'list', '--region', self.settings.region,
                                                                  '--filter', f'metadata.name={service}', '--format=json'], private=True))
                            if not any(item.get('metadata', {}).get('name') == service for item in remaining):
                                service_absent = True
                                break
                            time.sleep(2)
            except Exception:
                self.event('cleanup', 'Cloud Run 정리 확인 필요: ' + service)
        if self.pushed and service_absent:
            try:
                self.gcloud(['artifacts', 'docker', 'images', 'delete', self.image], timeout=120)
            except Exception:
                self.event('cleanup', '업로드 이미지 정리 확인 필요: ' + str(self.image))
        elif self.pushed:
            self.event('cleanup', 'Cloud Run 서비스 종료가 확인되지 않아 이미지를 보존했습니다: ' + str(self.image))
        if self.built:
            try:
                self.command(['docker', 'image', 'rm', self.image], timeout=30)
            except Exception:
                self.event('cleanup', '로컬 이미지 정리 확인 필요: ' + str(self.image))
