import json
import unittest
from unittest.mock import patch

from deployment.aws import AwsConfigurationError, AwsExpressAdapter, AwsSettings
from deployment.aws_pricing import estimate_postgres_base_capacity


REGION = 'ap-northeast-2'


def price_item(family, attributes, sku, unit, usd):
    return json.dumps({'product': {'productFamily': family, 'sku': sku,
        'attributes': {'regionCode': REGION, 'databaseEngine': 'PostgreSQL',
            'deploymentOption': 'Single-AZ', 'locationType': 'AWS Region', **attributes}},
        'terms': {'OnDemand': {'term': {'priceDimensions': {'rate': {
            'unit': unit, 'beginRange': '0', 'endRange': 'Inf',
            'pricePerUnit': {'USD': usd}}}}}}})


INSTANCE = price_item('Database Instance', {
    'instanceType': 'db.t4g.micro', 'licenseModel': 'No license required',
    'usagetype': 'APN2-InstanceUsage:db.t4g.micro'}, 'ZBMXF2F4CYQ2FT96', 'Hrs', '0.0250000000')
STORAGE = price_item('Database Storage', {
    'volumeType': 'General Purpose-GP3', 'usagetype': 'APN2-RDS:GP3-Storage'},
    '8TFTRRBWJSP95DQP', 'GB-Mo', '0.1310000000')


class AwsPricingTests(unittest.TestCase):
    def setUp(self):
        self.adapter = AwsExpressAdapter(lambda *_: None, AwsSettings(REGION))

    def test_exact_single_az_products_yield_base_capacity_only(self):
        calls = []
        def command(args, **_kwargs):
            calls.append(args)
            item = INSTANCE if 'Type=TERM_MATCH,Field=instanceType,Value=db.t4g.micro' in args else STORAGE
            return json.dumps({'FormatVersion': 'aws_v1', 'PriceList': [item]})
        with patch.object(self.adapter, 'command', side_effect=command):
            estimate = estimate_postgres_base_capacity(self.adapter, REGION)
        self.assertEqual(estimate['baseline_730h_usd'], '20.87')
        self.assertEqual(estimate['scope'], 'instance_and_provisioned_storage_only')
        self.assertIn('backup_overage', estimate['excluded'])
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(args[:3] == ['aws', 'pricing', 'get-products'] for args in calls))
        self.assertTrue(all(args[args.index('--region') + 1] == 'us-east-1' for args in calls))

    def test_rejects_duplicate_or_wrong_unit_or_truncated_prices(self):
        for response in [
                {'FormatVersion': 'aws_v1', 'PriceList': [INSTANCE, INSTANCE]},
                {'FormatVersion': 'aws_v1', 'PriceList': [INSTANCE], 'NextToken': 'more'},
                {'FormatVersion': 'aws_v1', 'PriceList': [INSTANCE.replace('"Hrs"', '"GB-Mo"')]}]:
            with self.subTest(response=response):
                with patch.object(self.adapter, 'command', return_value=json.dumps(response)):
                    with self.assertRaises(AwsConfigurationError):
                        estimate_postgres_base_capacity(self.adapter, REGION)

    def test_rejects_a_product_for_a_different_region(self):
        wrong = INSTANCE.replace(REGION, 'us-east-1')
        with patch.object(self.adapter, 'command', return_value=json.dumps({
                'FormatVersion': 'aws_v1', 'PriceList': [wrong]})):
            with self.assertRaisesRegex(AwsConfigurationError, 'SKU'):
                estimate_postgres_base_capacity(self.adapter, REGION)


if __name__ == '__main__':
    unittest.main()
