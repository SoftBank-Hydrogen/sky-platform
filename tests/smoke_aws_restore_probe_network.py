"""Opt-in real AWS smoke for temporary restore verifier network access."""
from __future__ import annotations

import argparse
import secrets

from sky_platform.database.postgres_restore_network import RestoreNetworkRequest, RestoreSecurityGroup
from sky_platform.database.postgres_restore_probe_network import RestoreProbeNetwork


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description='Verify temporary restore probe network')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--application', default='demo-app')
    parser.add_argument('--account', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--vpc-id', required=True)
    args = parser.parse_args(argv)
    target = 'sky-restore-' + args.application + '-probe-' + secrets.token_hex(4)
    request = RestoreNetworkRequest(args.application, target, args.account,
                                    args.region, args.vpc_id)
    database = RestoreSecurityGroup(request)
    database.preflight()
    print('Read-only probe network plan:', target, flush=True)
    if not args.apply:
        return
    db_id = None
    probe_id = None
    try:
        db_id = database.create()['group_id']
        probe = RestoreProbeNetwork(request, db_id)
        probe_id = probe.open()['probe_group_id']
        print('PASS: probe link opened:', probe_id, flush=True)
        probe.close(probe_id)
        probe_id = None
        print('PASS: probe link closed', flush=True)
        database.delete(db_id)
        db_id = None
        print('PASS: temporary database group deleted', flush=True)
    finally:
        if db_id or probe_id:
            print('AWS 상태를 직접 확인하세요:', request.group_name,
                  request.target_id + '-probe', db_id, probe_id, flush=True)


if __name__ == '__main__':
    main()
