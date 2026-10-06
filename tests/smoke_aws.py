"""Explicit live AWS smoke. Creates persistent, billable resources with --apply."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path

from sky_platform.runtime.aws import AwsExpressAdapter, AwsSettings
from sky_platform.runtime.core import analyze


def main():
    parser = argparse.ArgumentParser(description='Deploy the real hello-node sample to AWS ECS Express Mode')
    parser.add_argument('--apply', action='store_true', help='Create real AWS resources')
    parser.add_argument('--account', required=True, help='Expected 12-digit AWS account ID')
    parser.add_argument('--region', required=True)
    args = parser.parse_args()
    settings = AwsSettings(args.region)
    settings.validate()
    identity = json.loads(subprocess.check_output(['aws', 'sts', 'get-caller-identity', '--region', args.region,
                                                    '--output', 'json', '--no-cli-pager'], text=True))
    if identity.get('Account') != args.account:
        raise SystemExit('AWS 계정이 요청한 계정과 다릅니다. 리소스를 생성하지 않았습니다.')
    if not args.apply:
        print('읽기 전용 사전 점검 완료. --apply를 지정해야 AWS 리소스를 생성합니다.')
        return
    attempt = uuid.uuid4().hex[:16] + '-a1'
    with tempfile.TemporaryDirectory(prefix='sky-aws-smoke-') as directory:
        project = Path(directory) / 'app'
        shutil.copytree(Path(__file__).resolve().parents[1] / 'tests' / 'fixtures' / 'apps' / 'hello-node', project)
        plan = replace(analyze(project), target='aws-ecs-express')
        adapter = AwsExpressAdapter(lambda stage, message: print(f'[{stage}] {message}', flush=True), settings)
        try:
            result = adapter.deploy(project, plan, attempt)
        except Exception:
            adapter.cleanup_failure(attempt)
            raise
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print('성공한 ECS 서비스·ECR 이미지·공유 CloudFormation 스택은 자동 삭제하지 않습니다.')


if __name__ == '__main__':
    main()
