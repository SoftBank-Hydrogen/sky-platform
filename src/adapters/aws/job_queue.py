"""SQS FIFO publishing aligned with the sky-infra queue contract."""

import json
import re
from urllib.parse import urlsplit

from ports.operations import OutboxDelivery


class SqsOperationQueue:
    def __init__(self, url: str, *, region: str, account_id: str, client=None):
        if not all(isinstance(value, str) for value in (url, region, account_id)) or not re.fullmatch(
            r"[a-z]{2}(?:-[a-z]+)+-\d+", region
        ):
            raise ValueError("Invalid SQS account, region or URL")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != f"sqs.{region}.amazonaws.com"
            or not re.fullmatch(r"\d{12}", account_id)
            or not re.fullmatch(rf"/{account_id}/[A-Za-z0-9_-]+\.fifo", parsed.path)
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Queue must be the configured account's regional SQS FIFO URL")
        if client is None:
            import boto3

            from botocore.config import Config

            client = boto3.client(
                "sqs",
                region_name=region,
                config=Config(
                    connect_timeout=5, read_timeout=20, retries={"mode": "standard", "total_max_attempts": 1}
                ),
            )
        self.client = client
        self.url = url

    def publish(self, delivery: OutboxDelivery):
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            self.client.send_message(
                QueueUrl=self.url,
                MessageBody=json.dumps(delivery.message(), sort_keys=True, separators=(",", ":")),
                MessageGroupId=delivery.application_id,
                MessageDeduplicationId=delivery.attempt_id,
            )
        except (BotoCoreError, ClientError):
            raise OSError("SQS publish failed; delivery may be uncertain") from None
