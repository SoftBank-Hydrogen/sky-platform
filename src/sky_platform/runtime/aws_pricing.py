"""Read-only RDS base-capacity estimate from exact AWS Price List products."""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation, ROUND_UP

from sky_platform.runtime.aws import AwsConfigurationError, AwsExpressAdapter


HOURS_PER_MONTH = 730
STORAGE_GIB = 20


def _rate(adapter: AwsExpressAdapter, target_region: str, family: str,
          attributes: dict[str, str], usage_suffix: str, unit: str) -> dict:
    filters = {'regionCode': target_region, 'databaseEngine': 'PostgreSQL',
               'deploymentOption': 'Single-AZ', **attributes}
    args = ['aws', 'pricing', 'get-products', '--service-code', 'AmazonRDS',
            '--filters', *[f'Type=TERM_MATCH,Field={key},Value={value}'
                           for key, value in filters.items()],
            '--max-items', '20', '--region', 'us-east-1', '--no-cli-pager', '--output', 'json']
    response = json.loads(adapter.command(args, private=True, quiet=True))
    if response.get('FormatVersion') != 'aws_v1' or response.get('NextToken'):
        raise AwsConfigurationError('RDS 공개 가격 결과를 완전히 확인하지 못했습니다.')
    matches = []
    for raw in response.get('PriceList', []):
        item = json.loads(raw)
        product = item.get('product', {})
        values = product.get('attributes', {})
        if (product.get('productFamily') != family
                or any(values.get(key) != value for key, value in filters.items())
                or not values.get('usagetype', '').endswith(usage_suffix)
                or values.get('locationType') != 'AWS Region'):
            continue
        terms = item.get('terms', {}).get('OnDemand', {})
        dimensions = [dimension for term in terms.values()
                      for dimension in term.get('priceDimensions', {}).values()]
        if len(terms) != 1 or len(dimensions) != 1:
            raise AwsConfigurationError('RDS 공개 가격의 온디맨드 단위를 하나로 확인하지 못했습니다.')
        dimension = dimensions[0]
        if (dimension.get('unit') != unit or dimension.get('beginRange') != '0'
                or dimension.get('endRange') != 'Inf'):
            raise AwsConfigurationError('RDS 공개 가격의 과금 단위가 예상과 다릅니다.')
        try:
            price = Decimal(dimension['pricePerUnit']['USD'])
        except (KeyError, TypeError, InvalidOperation):
            raise AwsConfigurationError('RDS 공개 가격의 USD 요금을 읽지 못했습니다.') from None
        if not price.is_finite() or price <= 0:
            raise AwsConfigurationError('RDS 공개 가격의 USD 요금이 올바르지 않습니다.')
        matches.append({'sku': product.get('sku'), 'usd': price,
                        'published_at': item.get('publicationDate')})
    if len(matches) != 1 or not re.fullmatch(r'[A-Z0-9]{8,32}', matches[0].get('sku', '')):
        raise AwsConfigurationError('RDS 공개 가격 SKU를 정확히 하나로 확인하지 못했습니다.')
    return matches[0]


def estimate_postgres_base_capacity(adapter: AwsExpressAdapter, target_region: str) -> dict:
    """Estimate only 730 instance-hours plus 20 GiB-month of Single-AZ gp3."""
    instance = _rate(adapter, target_region, 'Database Instance',
                     {'instanceType': 'db.t4g.micro', 'licenseModel': 'No license required'},
                     'InstanceUsage:db.t4g.micro', 'Hrs')
    storage = _rate(adapter, target_region, 'Database Storage',
                    {'volumeType': 'General Purpose-GP3'}, 'RDS:GP3-Storage', 'GB-Mo')
    baseline = (instance['usd'] * HOURS_PER_MONTH + storage['usd'] * STORAGE_GIB).quantize(
        Decimal('0.01'), rounding=ROUND_UP)
    return {'scope': 'instance_and_provisioned_storage_only', 'currency': 'USD',
            'hours': HOURS_PER_MONTH, 'storage_gib': STORAGE_GIB,
            'instance_hourly_usd': str(instance['usd']),
            'storage_gib_monthly_usd': str(storage['usd']),
            'baseline_730h_usd': str(baseline),
            'instance_sku': instance['sku'], 'storage_sku': storage['sku'],
            'excluded': ['backup_overage', 'additional_iops_or_throughput',
                         'data_transfer', 'secrets', 'logs',
                         'ecs', 'taxes', 'discounts']}
