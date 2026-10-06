import unittest
from unittest.mock import patch

from tests.live.smoke_aws_network_browser import main


class AwsNetworkBrowserSmokeTests(unittest.TestCase):
    def test_default_mode_only_preflights_without_starting_browser(self):
        arguments = ['--application', 'netprobe-12345678',
                     '--account', '123456789012', '--region', 'ap-northeast-2']
        with patch('tests.live.smoke_aws_network_browser.discover_default_network',
                   return_value={'vpc_id': 'vpc-12345678'}), \
                patch('tests.live.smoke_aws_network_browser.AwsServiceNetworkProvisioner.preflight',
                      return_value={'stack_name': 'sky-network-netprobe-12345678'}), \
                patch('tests.live.smoke_aws_network_browser.subprocess.Popen') as chrome:
            main(arguments)
        chrome.assert_not_called()


if __name__ == '__main__':
    unittest.main()
