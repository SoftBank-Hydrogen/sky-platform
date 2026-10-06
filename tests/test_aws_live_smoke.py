"""Validate the live smoke's guards and evidence checks without calling paid services."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sky_platform.planning.analysis import AISettings
from smoke_aws_live_ai import main, preflight, verify_job


class LiveAwsSmokeTests(unittest.TestCase):
    def test_dry_run_never_starts_deployment(self):
        with patch('smoke_aws_live_ai.AISettings.from_environment',
                   return_value=AISettings('fixture-key', 'fixture-model')), \
                patch('smoke_aws_live_ai.preflight'), patch('smoke_aws_live_ai.run_live') as run_live:
            main(['--account', '123456789012', '--region', 'ap-northeast-2'])
            run_live.assert_not_called()

    def test_preflight_requires_key_and_expected_account_before_mutation(self):
        with patch('smoke_aws_live_ai.subprocess.check_output') as identity:
            with self.assertRaisesRegex(ValueError, 'OPENAI_API_KEY'):
                preflight('123456789012', 'ap-northeast-2', AISettings())
            with self.assertRaisesRegex(ValueError, '12자리'):
                preflight('123', 'ap-northeast-2', AISettings('fixture-key', 'fixture-model'))
            identity.assert_not_called()

    def test_preflight_rejects_wrong_aws_account(self):
        with patch('smoke_aws_live_ai.shutil.which', return_value='/usr/bin/tool'), \
                patch('smoke_aws_live_ai.subprocess.run'), \
                patch('smoke_aws_live_ai.subprocess.check_output',
                      return_value=json.dumps({'Account': '999999999999'})):
            with self.assertRaisesRegex(ValueError, '리소스를 생성하지 않았습니다'):
                preflight('123456789012', 'ap-northeast-2', AISettings('fixture-key', 'fixture-model'))

    def test_preflight_rejects_stopped_docker_before_sts(self):
        with patch('smoke_aws_live_ai.shutil.which', return_value='/usr/bin/tool'), \
                patch('smoke_aws_live_ai.subprocess.run', side_effect=FileNotFoundError), \
                patch('smoke_aws_live_ai.subprocess.check_output') as identity:
            with self.assertRaisesRegex(ValueError, 'Docker 데몬'):
                preflight('123456789012', 'ap-northeast-2', AISettings('fixture-key', 'fixture-model'))
            identity.assert_not_called()

    def test_success_requires_real_source_edits_and_unchanged_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'source'
            work = root / 'work'
            project.mkdir()
            work.mkdir()
            (project / 'package.json').write_text('{"scripts":{}}')
            (project / 'server.js').write_text(".listen(4321, '127.0.0.1')")
            (work / 'package.json').write_text('{"scripts":{"start":"node server.js"}}')
            (work / 'server.js').write_text(".listen(Number(process.env.PORT), '0.0.0.0')")
            job_id = 'a' * 16
            job = {'status': 'succeeded', 'target': 'aws-ecs-express', 'project': str(project),
                   'changes': [{'path': 'package.json', 'diff': '+start'},
                               {'path': 'server.js', 'diff': '+binding'}],
                   'result': {'target': 'aws-ecs-express', 'service': 'sky-' + job_id + '-a1',
                              'owner_attempt': job_id + '-a1', 'url': 'https://service.example'}}
            verify_job(job, job_id)
            with self.assertRaisesRegex(AssertionError, '모두 수정'):
                verify_job({**job, 'changes': [{'path': 'server.js', 'diff': '+binding'}]}, job_id)


if __name__ == '__main__':
    unittest.main()
