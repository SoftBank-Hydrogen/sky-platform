"""Explicit live ECS Express update smoke; keeps existing demo services untouched."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import uuid
from dataclasses import replace
from pathlib import Path

from deployment.aws import AwsExpressAdapter, AwsSettings
from deployment.core import analyze


def assert_version(url, version):
    host = urllib.parse.urlsplit(url).hostname
    for _ in range(24):
        answer = subprocess.check_output(['dig', '@1.1.1.1', '+short', host, 'A'], text=True).splitlines()
        for address in (item for item in answer if item.count('.') == 3):
            response = subprocess.run(['curl', '--silent', '--show-error', '--fail', '--max-time', '8',
                '--connect-timeout', '3', '--resolve', f'{host}:443:{address}', url + '/'],
                capture_output=True, text=True)
            if response.returncode == 0:
                try:
                    if json.loads(response.stdout).get('version') == version:
                        return
                except ValueError:
                    pass
        time.sleep(5)
    raise AssertionError(f'The public URL did not serve the {version} response')


def main():
    parser = argparse.ArgumentParser(description='Create versioned ECS Express releases, optionally roll back, then clean up')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--rollback', action='store_true', help='Also redeploy the previous task definition')
    parser.add_argument('--rollback-oldest', action='store_true', help='Deploy v1, v2, v3 and restore v1')
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    args = parser.parse_args()
    settings = AwsSettings(args.region)
    settings.validate()
    identity = json.loads(subprocess.check_output(['aws', 'sts', 'get-caller-identity', '--region', args.region,
                                                    '--output', 'json', '--no-cli-pager'], text=True))
    if identity.get('Account') != args.account:
        raise SystemExit('AWS 계정이 예상과 다릅니다. 리소스를 생성하지 않았습니다.')
    if not args.apply:
        print('읽기 전용 계정 점검 완료. --apply를 지정해야 리소스를 생성합니다.')
        return
    first_attempt = uuid.uuid4().hex[:16] + '-a1'
    second_attempt = uuid.uuid4().hex[:16] + '-a1'
    third_attempt = uuid.uuid4().hex[:16] + '-a1'
    first_adapter = AwsExpressAdapter(lambda stage, message: print(f'[v1:{stage}] {message}', flush=True), settings)
    second_adapter = None
    third_adapter = None
    first_result = None
    second_result = None
    third_result = None
    with tempfile.TemporaryDirectory(prefix='sky-aws-update-smoke-') as directory:
        source = Path(__file__).resolve().parents[2] / 'tests' / 'fixtures' / 'apps' / 'hello-node'
        first_project = Path(directory) / 'v1'
        second_project = Path(directory) / 'v2'
        third_project = Path(directory) / 'v3'
        shutil.copytree(source, first_project)
        shutil.copytree(source, second_project)
        shutil.copytree(source, third_project)
        server_file = second_project / 'server.js'
        server_file.write_text(server_file.read_text().replace("version: 'v1'", "version: 'v2'")
                               .replace('Hello from Sky!', 'Hello from Sky v2!'))
        server_file = third_project / 'server.js'
        server_file.write_text(server_file.read_text().replace("version: 'v1'", "version: 'v3'")
                               .replace('Hello from Sky!', 'Hello from Sky v3!'))
        try:
            first_result = first_adapter.deploy(first_project,
                replace(analyze(first_project), target='aws-ecs-express'), first_attempt)
            print('v1 deployed:', first_result['url'], flush=True)
            for _ in range(180):
                deployments = json.loads(first_adapter.aws(['ecs', 'list-service-deployments',
                    '--cluster', 'default', '--service', first_result['service']], private=True))
                items = deployments.get('serviceDeployments', [])
                if items:
                    status = max(items, key=lambda item: item.get('createdAt', '')).get('status')
                    if status == 'SUCCESSFUL':
                        break
                    if status in {'STOPPED', 'ROLLBACK_FAILED', 'ROLLBACK_SUCCESSFUL'}:
                        raise RuntimeError('Initial service deployment failed: ' + status)
                time.sleep(5)
            else:
                raise RuntimeError('Initial service deployment did not finish')
            second_adapter = AwsExpressAdapter(
                lambda stage, message: print(f'[v2:{stage}] {message}', flush=True), settings,
                existing=first_result)
            second_result = second_adapter.deploy(second_project,
                replace(analyze(second_project), target='aws-ecs-express'), second_attempt)
            assert second_result['url'] == first_result['url']
            assert second_result['service_arn'] == first_result['service_arn']
            assert second_result['image'] != first_result['image']
            assert_version(second_result['url'], 'v2')
            print('PASS: ECS Express updated v1 -> v2 on the same HTTPS URL', flush=True)
            print(json.dumps({'url': second_result['url'], 'service': second_result['service'],
                              'images': second_result['images']}, ensure_ascii=False, indent=2), flush=True)
            if args.rollback_oldest:
                third_adapter = AwsExpressAdapter(
                    lambda stage, message: print(f'[v3:{stage}] {message}', flush=True), settings,
                    existing=second_result)
                third_result = third_adapter.deploy(third_project,
                    replace(analyze(third_project), target='aws-ecs-express'), third_attempt)
                assert third_result['url'] == first_result['url']
                assert third_result['service_arn'] == first_result['service_arn']
                assert_version(third_result['url'], 'v3')
                print('PASS: ECS Express updated v2 -> v3 on the same HTTPS URL', flush=True)
            if args.rollback or args.rollback_oldest:
                rollback_adapter = AwsExpressAdapter(
                    lambda stage, message: print(f'[rollback:{stage}] {message}', flush=True), settings)
                rollback_result = rollback_adapter.rollback_release(third_result or second_result, first_result, '/')
                assert rollback_result['url'] == first_result['url']
                assert_version(first_result['url'], 'v1')
                print('PASS: completed release restored v1 on the same HTTPS URL', flush=True)
        finally:
            try:
                if first_result:
                    results = [item for item in (first_result, second_result, third_result) if item]
                    images = [adapter.image for adapter in (first_adapter, second_adapter, third_adapter)
                              if adapter and adapter.image_pushed and adapter.image]
                    for _ in range(180):
                        described = json.loads(first_adapter.aws(['ecs', 'describe-express-gateway-service',
                            '--service-arn', first_result['service_arn']], private=True, quiet=True))['service']
                        configurations = described.get('activeConfigurations', [])
                        if not described.get('currentDeployment') and len(configurations) == 1:
                            active_image = configurations[0].get('primaryContainer', {}).get('image')
                            active_result = next((item for item in results if item['image'] == active_image), None)
                            if active_result:
                                break
                        time.sleep(5)
                    else:
                        raise RuntimeError('Smoke service did not settle; manual cleanup required')
                    first_adapter.retire({**active_result, 'images': list(dict.fromkeys(images))}, first_attempt)
                    print('Smoke service and ECR image tags deleted.', flush=True)
                else:
                    first_adapter.cleanup_failure(first_attempt)
            except Exception as exc:
                print('Smoke cleanup requires inspection:', exc, flush=True)


if __name__ == '__main__':
    main()
