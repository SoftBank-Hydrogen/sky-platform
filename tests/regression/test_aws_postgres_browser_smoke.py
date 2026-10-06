import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from tests.live.smoke_aws_postgres_browser import main


class AwsPostgresBrowserSmokeTests(unittest.TestCase):
    def test_update_mode_requires_explicit_apply_before_aws_lookup(self):
        arguments = ['--account', '123456789012', '--region', 'ap-northeast-2',
                     '--service-security-group', 'sg-33333333', '--verify-update']
        with patch('tests.live.smoke_aws_postgres_browser.discover_existing_postgres') as discover, \
                redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(arguments)
        discover.assert_not_called()

    def test_default_mode_only_discovers_existing_database(self):
        arguments = ['--account', '123456789012', '--region', 'ap-northeast-2',
                     '--service-security-group', 'sg-33333333']
        for runtime in ('node', 'python'):
            with self.subTest(runtime=runtime), \
                    patch('tests.live.smoke_aws_postgres_browser.discover_existing_postgres',
                          return_value={'database_id': 'sky-demo-app',
                                        'status': 'available'}) as discover, \
                    patch('tests.live.smoke_aws_postgres_browser.subprocess.Popen') as chrome:
                main(arguments + ['--probe-runtime', runtime])
                discover.assert_called_once()
                chrome.assert_not_called()


if __name__ == '__main__':
    unittest.main()
