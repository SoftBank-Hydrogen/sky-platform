import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.smoke_aws_restore_marker_source import run


class RestoreMarkerSourceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name) / 'state' / 'marker.json'
        self.args = argparse.Namespace(
            application='demo-app', account='123456789012', region='ap-northeast-2',
            vpc_id='vpc-12345678', subnet_id=['subnet-12345678', 'subnet-87654321'],
            service_security_group='sg-12345678',
            snapshot_id='sky-demo-app-marker-test',
            state_file=self.state, apply=True)

    def test_snapshot_follows_source_write_and_precedes_marker_removal(self):
        events = []
        result = {'account': self.args.account, 'region': self.args.region,
                  'target': 'aws-ecs-express', 'service': 'sky-aaaaaaaaaaaaaaaa-a1',
                  'service_arn': 'arn:aws:ecs:ap-northeast-2:123456789012:'
                                 'service/default/sky-aaaaaaaaaaaaaaaa-a1',
                  'image': 'image', 'owner_attempt': 'aaaaaaaaaaaaaaaa-a1',
                  'url': 'https://service.ecs.ap-northeast-2.on.aws'}

        def probe(_url, _key, _marker, method):
            events.append(method)

        def snapshot(*_args):
            events.append('SNAPSHOT')
            return {'snapshot_arn': 'snapshot-arn'}

        def retire(*_args):
            events.append('RETIRE')

        with patch('tests.smoke_aws_restore_marker_source.AwsPostgresProvisioner.inspect_current',
                   return_value={'database_id': 'sky-demo-app'}), \
                patch('tests.smoke_aws_restore_marker_source.plan_snapshot',
                      return_value={'database_id': 'sky-demo-app'}), \
                patch('tests.smoke_aws_restore_marker_source.AwsExpressAdapter.deploy',
                      return_value=result), \
                patch('tests.smoke_aws_restore_marker_source.AwsExpressAdapter.retire',
                      side_effect=retire), \
                patch('tests.smoke_aws_restore_marker_source.probe', side_effect=probe), \
                patch('tests.smoke_aws_restore_marker_source.create_snapshot',
                      side_effect=snapshot), \
                patch('tests.smoke_aws_restore_marker_source.inspect_snapshot',
                      return_value={'status': 'available'}), \
                patch('tests.smoke_aws_restore_marker_source.uuid.uuid4') as uuid4:
            uuid4.return_value.hex = 'a' * 32
            outcome = run(self.args)

        self.assertEqual(events, ['POST', 'GET', 'SNAPSHOT', 'DELETE', 'RETIRE'])
        self.assertEqual(outcome['status'], 'succeeded')
        stored = json.loads(self.state.read_text())
        self.assertEqual(stored['stage'], 'source_service_retired')
        self.assertNotIn('probe_key', stored)
        self.assertEqual(os.stat(self.state).st_mode & 0o777, 0o600)

    def test_read_only_plan_does_not_create_state_or_deploy(self):
        self.args.apply = False
        with patch('tests.smoke_aws_restore_marker_source.AwsPostgresProvisioner.inspect_current',
                   return_value={'database_id': 'sky-demo-app'}), \
                patch('tests.smoke_aws_restore_marker_source.plan_snapshot',
                      return_value={'database_id': 'sky-demo-app'}), \
                patch('tests.smoke_aws_restore_marker_source.AwsExpressAdapter.deploy') as deploy:
            outcome = run(self.args)
        self.assertEqual(outcome['mode'], 'read_only')
        self.assertFalse(self.state.exists())
        deploy.assert_not_called()


if __name__ == '__main__':
    unittest.main()
