"""Reject untrusted SQS commands and use only bounded identity envelopes."""

import json
from unittest.mock import Mock
from uuid import uuid4

import pytest

from adapters.aws.job_queue import SqsOperationQueue


def envelope():
    return {
        "version": 1,
        "workspace": "team",
        "application_id": "app",
        "operation_id": str(uuid4()),
        "attempt_id": str(uuid4()),
    }


def queue(body):
    client = Mock(
        receive_message=Mock(return_value={"Messages": [{"ReceiptHandle": "opaque", "Body": body}]})
    )
    return SqsOperationQueue(
        "https://sqs.ap-northeast-2.amazonaws.com/111111111111/allocation.fifo",
        region="ap-northeast-2",
        account_id="111111111111",
        client=client,
    ), client


def test_receives_identity_only_and_acks_exact_receipt():
    message = envelope()
    adapter, client = queue(json.dumps(message))
    delivery = adapter.receive()
    assert delivery.message == message
    adapter.extend(delivery, seconds=300)
    adapter.delete(delivery)
    assert client.delete_message.call_args.kwargs["ReceiptHandle"] == "opaque"
    assert client.change_message_visibility.call_args.kwargs["VisibilityTimeout"] == 300
    assert client.receive_message.call_args.kwargs["MaxNumberOfMessages"] == 1


@pytest.mark.parametrize("change", ["command", "version", "identity", "scope", "duplicate", "oversize"])
def test_invalid_queue_input_never_exposes_a_command(change):
    value = envelope()
    if change == "command":
        value["command"] = {"create_database": "arbitrary"}
    elif change == "version":
        value["version"] = True
    elif change == "identity":
        value["operation_id"] = "not-a-uuid"
    elif change == "scope":
        value["workspace"] = "../another"
    body = json.dumps(value)
    if change == "duplicate":
        body = body[:-1] + ', "workspace":"other"}'
    elif change == "oversize":
        body = " " * 4097
    adapter, client = queue(body)
    assert adapter.receive().message is None
    client.delete_message.assert_not_called()
