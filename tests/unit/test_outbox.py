import json
from unittest.mock import Mock

import pytest

pytest.importorskip("botocore")

from adapters.aws.job_queue import SqsOperationQueue
from application.outbox import OutboxPublisher
from ports.operations import OutboxDelivery

URL = "https://sqs.ap-northeast-2.amazonaws.com/123456789012/sky-dev-jobs.fifo"


def delivery():
    return OutboxDelivery("event1", "publisher", 1, "operation1", "attempt1", "game", "team")


def test_fifo_send_contains_only_db_identities_and_infra_grouping():
    client = Mock()
    queue = SqsOperationQueue(URL, region="ap-northeast-2", account_id="123456789012", client=client)
    message = delivery()
    queue.publish(message)
    kwargs = client.send_message.call_args.kwargs
    assert json.loads(kwargs["MessageBody"]) == message.message()
    assert kwargs["MessageGroupId"] == "game"
    assert kwargs["MessageDeduplicationId"] == "attempt1"
    assert kwargs["QueueUrl"] == URL


@pytest.mark.parametrize(
    "url",
    [
        URL.replace("123456789012", "999999999999"),
        URL.replace("ap-northeast-2", "us-east-1"),
        URL.replace("https:", "http:"),
        URL.replace(".fifo", ""),
        URL + "?option=1",
        URL.replace("sqs.ap-northeast-2.amazonaws.com", "sqs.ap-northeast-2.amazonaws.com.evil.example"),
    ],
)
def test_queue_cannot_publish_to_another_account_or_endpoint(url):
    with pytest.raises(ValueError):
        SqsOperationQueue(url, region="ap-northeast-2", account_id="123456789012", client=Mock())


def test_aws_publish_error_is_redacted_and_is_not_retried_by_adapter():
    from botocore.exceptions import ClientError

    client = Mock()
    client.send_message.side_effect = ClientError(
        {"Error": {"Code": "Denied", "Message": "private-token"}}, "SendMessage"
    )
    queue = SqsOperationQueue(URL, region="ap-northeast-2", account_id="123456789012", client=client)
    with pytest.raises(OSError) as error:
        queue.publish(delivery())
    assert "private-token" not in str(error.value)
    assert client.send_message.call_count == 1


def test_failed_send_releases_outbox_without_confirming_it():
    store, queue = Mock(), Mock()
    message = delivery()
    store.claim_outbox.return_value = (message,)
    queue.publish.side_effect = OSError("uncertain send")
    result = OutboxPublisher(store, queue, "publisher").dispatch_once()
    assert result.confirmed == 0 and result.deferred == 1
    store.release_outbox.assert_called_once_with(message)
    store.confirm_outbox.assert_not_called()


def test_database_confirm_failure_does_not_immediately_repeat_the_remote_send():
    store, queue = Mock(), Mock()
    store.claim_outbox.return_value = (delivery(),)
    store.confirm_outbox.side_effect = OSError("uncertain commit")
    with pytest.raises(OSError):
        OutboxPublisher(store, queue, "publisher").dispatch_once()
    assert queue.publish.call_count == 1
    store.release_outbox.assert_not_called()


def test_default_sqs_client_disables_sdk_retry_layer():
    from unittest.mock import patch

    with patch("boto3.client") as factory:
        SqsOperationQueue(URL, region="ap-northeast-2", account_id="123456789012")
    config = factory.call_args.kwargs["config"]
    assert config.retries["total_max_attempts"] == 1
    assert config.connect_timeout == 5 and config.read_timeout == 20
