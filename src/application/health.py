"""Read-only checks of a previously verified deployment."""
from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from adapters.aws.ecs import AwsExpressAdapter, AwsSettings
from adapters.gcp.cloud_run import CloudRunAdapter, CloudRunSettings


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def probe(url: str, token: str | None = None) -> bool:
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(urllib.request.Request(url, headers=headers), timeout=5) as response:
            return response.status == 200
    except (OSError, urllib.error.HTTPError):
        return False


def check_deployment(job: dict) -> dict:
    """Do not trust a stored success flag or an arbitrary stored URL as proof of health."""
    checked_at = datetime.now(timezone.utc).isoformat()
    if job.get('status') != 'succeeded' or not isinstance(job.get('result'), dict):
        raise ValueError('Only completed deployments can be checked')
    result = job['result']
    job_id = job['id']
    health_path = (job.get('plan') or {}).get('health_path', '/')
    target = job.get('target', 'local-docker')
    if job.get('deployment_state') == 'superseded':
        return {'healthy': False, 'checked_at': checked_at, 'reason': 'A newer release uses this service'}
    try:
        if target == 'local-docker':
            name = result.get('container', '')
            # Legacy deployments use the job ID; agent deployments use a bounded attempt ID.
            if name not in {f'sky-{job_id}', *(f'sky-{job_id}-a{i}' for i in range(1, 4))}:
                raise ValueError('Container identity is missing or unexpected')
            inspect = subprocess.run(['docker', 'inspect', name], capture_output=True, text=True, timeout=10)
            if inspect.returncode:
                return {'healthy': False, 'checked_at': checked_at, 'reason': 'Container is missing'}
            containers = json.loads(inspect.stdout)
            if (len(containers) != 1 or not containers[0].get('State', {}).get('Running')
                    or containers[0].get('Config', {}).get('Labels', {}).get('app') != 'sky'
                    or containers[0].get('Config', {}).get('Image') != result.get('image')):
                return {'healthy': False, 'checked_at': checked_at, 'reason': 'Container identity or state changed'}
            sqlite_binding = job.get('local_sqlite_binding')
            if isinstance(sqlite_binding, dict):
                mounts = containers[0].get('Mounts') or []
                if (result.get('sqlite_volume') != sqlite_binding.get('volume_name')
                        or result.get('sqlite_mount') != sqlite_binding.get('mount_path')
                        or not any(mount.get('Type') == 'volume'
                                   and mount.get('Name') == sqlite_binding.get('volume_name')
                                   and mount.get('Destination') == sqlite_binding.get('mount_path')
                                   for mount in mounts)):
                    return {'healthy': False, 'checked_at': checked_at,
                            'reason': 'SQLite volume mount identity changed'}
            url = result.get('url', '')
            parsed = urllib.parse.urlsplit(url)
            if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port
                    or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password):
                raise ValueError('Stored local URL is invalid')
            healthy = probe(url + health_path)
        elif target == 'cloud-run':
            service = result.get('service', '')
            if not re.fullmatch(r'sky-' + re.escape(job_id) + r'-a[1-3]', service):
                raise ValueError('Cloud Run service identity is missing or unexpected')
            settings = CloudRunSettings(**job['cloud'])
            settings.validate()
            if result.get('project') != settings.project or result.get('region') != settings.region:
                raise ValueError('Cloud Run project or region changed')
            url = result.get('url', '')
            CloudRunAdapter.validate_url(url)
            adapter = CloudRunAdapter(lambda *_: None, settings, public=result.get('public', False))
            service_data = json.loads(adapter.gcloud(['run', 'services', 'describe', service,
                                                      '--region', settings.region, '--format=json'],
                                                     private=True, timeout=30))
            labels = service_data.get('metadata', {}).get('labels', {})
            if (labels.get('sky-managed') != 'true'
                    or labels.get('sky-attempt') != service.removeprefix('sky-')
                    or service_data.get('status', {}).get('url') != url):
                return {'healthy': False, 'checked_at': checked_at, 'reason': 'Cloud Run service identity changed'}
            token = None
            if not result.get('public', False):
                token = adapter.gcloud(['auth', 'print-identity-token'], private=True, timeout=15)
                if not token:
                    raise RuntimeError('No identity token available')
            healthy = probe(url + health_path, token)
        elif target == 'aws-ecs-express':
            service = result.get('service', '')
            owner_attempt = result.get('owner_attempt') or service.removeprefix('sky-')
            if (not re.fullmatch(r'[a-f0-9]{16}-a[1-3]', owner_attempt)
                    or service != 'sky-' + owner_attempt
                    or not re.fullmatch(r'\d{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/sky-managed:'
                                        + re.escape(job_id) + r'-a[1-3]', result.get('image', ''))):
                raise ValueError('ECS Express service identity is missing or unexpected')
            settings = AwsSettings(**job['aws'])
            settings.validate()
            account = result.get('account', '')
            if not re.fullmatch(r'\d{12}', account) or result.get('region') != settings.region:
                raise ValueError('AWS account or region changed')
            arn = f'arn:aws:ecs:{settings.region}:{account}:service/default/{service}'
            if result.get('service_arn') != arn:
                raise ValueError('ECS Express service ARN changed')
            url = result.get('url', '')
            AwsExpressAdapter.validate_url(url, service, settings.region)
            adapter = AwsExpressAdapter(lambda *_: None, settings)
            service_data = json.loads(adapter.aws(['ecs', 'describe-express-gateway-service',
                                                   '--service-arn', arn, '--include', 'TAGS'],
                                                  private=True, timeout=30))['service']
            tags = {item['key']: item['value'] for item in service_data.get('tags', [])}
            configs = service_data.get('activeConfigurations', [])
            active = configs[0] if len(configs) == 1 else {}
            endpoints = [path.get('endpoint') for path in active.get('ingressPaths', [])
                         if path.get('accessType') == 'PUBLIC' and path.get('endpoint')]
            endpoint = endpoints[0].rstrip('/') if len(endpoints) == 1 and isinstance(endpoints[0], str) else ''
            public_url = endpoint if endpoint.startswith('https://') else 'https://' + endpoint
            if (service_data.get('serviceArn') != arn or service_data.get('status', {}).get('statusCode') != 'ACTIVE'
                    or service_data.get('currentDeployment') or len(configs) != 1
                    or tags.get('sky-managed') != 'true'
                    or tags.get('sky-attempt') != service.removeprefix('sky-')
                    or public_url != url.rstrip('/')
                    or active.get('primaryContainer', {}).get('image') != result.get('image')
                    or result.get('service_security_group', '') != settings.service_security_group
                    or (settings.service_security_group and settings.service_security_group not in
                        active.get('networkConfiguration', {}).get('securityGroups', []))
                    or (result.get('task_definition_arn')
                        and active.get('taskDefinitionArn') != result['task_definition_arn'])):
                return {'healthy': False, 'checked_at': checked_at, 'reason': 'ECS Express service identity or state changed'}
            healthy = probe(url + health_path)
            if not healthy:
                healthy = adapter.probe_with_dns_fallback(url + health_path) == 200
        else:
            raise ValueError('Unsupported deployment target')
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired, RuntimeError) as exc:
        return {'healthy': False, 'checked_at': checked_at, 'reason': str(exc)[:300]}
    return {'healthy': healthy, 'checked_at': checked_at,
            'reason': 'HTTP 200 confirmed' if healthy else 'Service did not return HTTP 200'}
