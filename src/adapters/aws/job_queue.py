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


    def receive(self):
        from botocore.exceptions import BotoCoreError, ClientError
        from ports.queue import ReceivedOperation
        from uuid import UUID
        try:
            response = self.client.receive_message(QueueUrl=self.url, MaxNumberOfMessages=1,
                WaitTimeSeconds=10, VisibilityTimeout=300)
        except (BotoCoreError, ClientError):
            raise OSError("SQS receive failed") from None
        messages = response.get("Messages", [])
        if not messages:
            return None
        raw = messages[0]
        receipt = raw.get("ReceiptHandle")
        if not isinstance(receipt, str) or not 0 < len(receipt) <= 4096:
            raise OSError("SQS receipt missing")
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate queue field")
                result[key] = value
            return result
        try:
            body = raw["Body"]
            if not isinstance(body, str) or len(body.encode()) > 4096:
                raise ValueError("Queue body exceeds limit")
            value = json.loads(body, object_pairs_hook=unique)
            if not isinstance(value, dict) or set(value) != {"version", "workspace", "operation_id", "attempt_id", "application_id"} or type(value["version"]) is not int or value["version"] != 1:
                raise ValueError("Unsupported queue message")
            for field in ("operation_id", "attempt_id"):
                if not isinstance(value[field], str) or str(UUID(value[field])) != value[field]:
                    raise ValueError("Invalid queue identity")
            for field in ("workspace", "application_id"):
                if not isinstance(value[field], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value[field]):
                    raise ValueError("Invalid queue scope")
        except (KeyError, TypeError, ValueError, UnicodeError):
            value = None
        return ReceivedOperation(receipt, value)

    def extend(self, delivery, *, seconds=300):
        from botocore.exceptions import BotoCoreError, ClientError
        if type(seconds) is not int or not 0 <= seconds <= 43200:
            raise ValueError("Invalid visibility timeout")
        try:
            self.client.change_message_visibility(QueueUrl=self.url, ReceiptHandle=delivery.receipt, VisibilityTimeout=seconds)
        except (BotoCoreError, ClientError):
            raise OSError("SQS visibility update failed") from None

    def delete(self, delivery):
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            self.client.delete_message(QueueUrl=self.url, ReceiptHandle=delivery.receipt)
        except (BotoCoreError, ClientError):
            raise OSError("SQS acknowledgement failed") from None
