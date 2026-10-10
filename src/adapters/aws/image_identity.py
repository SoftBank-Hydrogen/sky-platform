"""Read-only linkage from a rehearsed OCI config to running ECS tasks."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime


def verify_runtime_image(aws, *, account, region, service, task_definition, image,
                         manifest_digest, local_image_id):
    """Resolve one linux/amd64 manifest, including OCI indexes with attestations."""
    digest_pattern = r'sha256:[a-f0-9]{64}'
    if not all(isinstance(value, str) and re.fullmatch(digest_pattern, value)
               for value in (manifest_digest, local_image_id)):
        raise ValueError('이미지 식별 다이제스트가 올바르지 않습니다.')
    def read_manifest(digest):
        response = json.loads(aws([
            'ecr', 'batch-get-image', '--repository-name', 'sky-managed',
            '--image-ids', 'imageDigest=' + digest], private=True, quiet=True))
        images = response.get('images', [])
        if response.get('failures') or len(images) != 1:
            raise ValueError('배포 이미지 매니페스트를 하나로 확인할 수 없습니다.')
        raw = images[0].get('imageManifest')
        if (not isinstance(raw, str) or images[0].get('imageId', {}).get('imageDigest') != digest
                or 'sha256:' + hashlib.sha256(raw.encode()).hexdigest() != digest):
            raise ValueError('배포 이미지 매니페스트 해시가 일치하지 않습니다.')
        return json.loads(raw)

    manifest = read_manifest(manifest_digest)
    platform_digest = manifest_digest
    if 'manifests' in manifest:
        candidates = [item for item in manifest['manifests']
                      if item.get('platform', {}).get('os') == 'linux'
                      and item.get('platform', {}).get('architecture') == 'amd64']
        if manifest.get('schemaVersion') != 2 or len(candidates) != 1:
            raise ValueError('linux/amd64 이미지 매니페스트를 하나로 확인할 수 없습니다.')
        platform_digest = candidates[0].get('digest')
        if not isinstance(platform_digest, str) or not re.fullmatch(digest_pattern, platform_digest):
            raise ValueError('플랫폼 이미지 다이제스트가 올바르지 않습니다.')
        manifest = read_manifest(platform_digest)
    config_digest = manifest.get('config', {}).get('digest')
    if (manifest.get('schemaVersion') != 2 or 'manifests' in manifest
            or not isinstance(config_digest, str) or not re.fullmatch(digest_pattern, config_digest)
            or local_image_id not in {manifest_digest, platform_digest, config_digest}):
        raise ValueError('리허설 이미지와 레지스트리 이미지의 다이제스트 연결이 다릅니다.')
    listed = json.loads(aws(['ecs', 'list-tasks', '--cluster', 'default', '--service-name', service,
                             '--desired-status', 'RUNNING'], private=True, quiet=True))
    arns = listed.get('taskArns', [])
    prefix = f'arn:aws:ecs:{region}:{account}:task/default/'
    if (listed.get('nextToken') or not isinstance(arns, list) or not 1 <= len(arns) <= 100
            or len(set(arns)) != len(arns)
            or any(not isinstance(arn, str) or not arn.startswith(prefix)
                   or not re.fullmatch(r'[a-f0-9]{32}', arn.removeprefix(prefix)) for arn in arns)):
        raise ValueError('실행 중인 소유 ECS 태스크 목록을 확인할 수 없습니다.')
    described = json.loads(aws(['ecs', 'describe-tasks', '--cluster', 'default', '--tasks', *arns],
                              private=True, quiet=True))
    tasks = described.get('tasks', [])
    if (described.get('failures') or len(tasks) != len(arns)
            or {task.get('taskArn') for task in tasks} != set(arns)):
        raise ValueError('실행 중인 ECS 태스크 응답이 목록과 다릅니다.')
    for task in tasks:
        containers = [container for container in task.get('containers', [])
                      if container.get('image') == image]
        if (task.get('group') != 'service:' + service
                or task.get('clusterArn') != f'arn:aws:ecs:{region}:{account}:cluster/default'
                or task.get('taskDefinitionArn') != task_definition
                or task.get('lastStatus') != 'RUNNING'
                or task.get('desiredStatus') != 'RUNNING' or len(containers) != 1
                or containers[0].get('lastStatus') != 'RUNNING'
                or containers[0].get('imageDigest') not in {manifest_digest, platform_digest}):
            raise ValueError('실행 ECS 태스크가 검증한 이미지 또는 서비스 리비전과 다릅니다.')
    return {'protocol': 'ecs-image-identity-v1', 'status': 'passed',
            'checked_at': datetime.now(UTC).isoformat(), 'local_image_id': local_image_id,
            'manifest_digest': manifest_digest, 'platform_manifest_digest': platform_digest,
            'config_digest': config_digest,
            'runtime_digests': sorted({container['imageDigest'] for task in tasks
                                      for container in task['containers'] if container.get('image') == image}),
            'image': image,
            'service_arn': f'arn:aws:ecs:{region}:{account}:service/default/{service}',
            'task_definition_arn': task_definition, 'task_arns': sorted(arns)}
