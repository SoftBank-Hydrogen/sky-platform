"""Opt-in real AWS lifecycle smoke for an isolated RDS restore security group."""
from __future__ import annotations

import argparse
import secrets

from sky_platform.database.postgres_restore_network import RestoreNetworkRequest, RestoreSecurityGroup


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Verify an isolated restore security group')
    parser.add_argument('--apply', action='store_true',
                        help='Create and retire a temporary security group')
    parser.add_argument('--application', default='demo-app')
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--vpc-id', required=True)
    args = parser.parse_args(argv)
    target = 'sky-restore-' + args.application + '-netprobe-' + secrets.token_hex(4)
    request = RestoreNetworkRequest(args.application, target, args.account,
                                    args.region, args.vpc_id)
    manager = RestoreSecurityGroup(request)
    preview = manager.preflight()
    print('Read-only restore network plan:', preview['group_name'], flush=True)
    if not args.apply:
        print('읽기 전용 검증 완료. --apply 없이는 그룹을 생성하지 않습니다.', flush=True)
        return
    created = None
    try:
        created = manager.create()
        print('PASS: isolated restore group created:', created['group_id'], flush=True)
    finally:
        if created is None:
            print('생성 결과가 불확실합니다. 이름으로 소유 그룹을 확인하세요:',
                  request.group_name, flush=True)
        else:
            deleted = manager.delete(created['group_id'])
            print('PASS: unused restore group deleted:', deleted['group_id'], flush=True)


if __name__ == '__main__':
    main()
