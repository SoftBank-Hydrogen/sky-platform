"""AWS requests are stubbed; no real bucket or credentials are used."""

import hashlib
import io
from dataclasses import replace
from unittest.mock import Mock

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from botocore.response import StreamingBody
from botocore.stub import Stubber

from adapters.aws.source_artifacts import S3ArtifactSettings, S3SourceArtifactStore
from ports.artifacts import SourceArtifact


def settings():
    return S3ArtifactSettings("sky-test-artifacts", "ap-northeast-2", "977889523182")


def artifact(data=b"test-zip-bytes"):
    return SourceArtifact(
        "org1", "game1", "a" * 32, "original", hashlib.sha256(data).hexdigest(), len(data), "b" * 64
    )


def response(ref, data=b"test-zip-bytes", **changes):
    values = {
        "Body": io.BytesIO(data),
        "ContentLength": ref.size,
        "Metadata": S3SourceArtifactStore._metadata(ref),
        "ServerSideEncryption": "AES256",
    }
    values.update(changes)
    return values


def test_real_sdk_accepts_conditional_checksum_and_expected_owner_request():
    client = boto3.client(
        "s3", region_name="ap-northeast-2", aws_access_key_id="test", aws_secret_access_key="test"
    )
    ref = artifact()
    store = S3SourceArtifactStore(settings(), client=client)
    import base64

    expected = {
        "Bucket": settings().bucket,
        "Key": ref.key,
        "Body": b"test-zip-bytes",
        "ContentLength": ref.size,
        "ContentType": "application/zip",
        "ServerSideEncryption": "AES256",
        "ExpectedBucketOwner": settings().account_id,
        "IfNoneMatch": "*",
        "ChecksumSHA256": base64.b64encode(bytes.fromhex(ref.sha256)).decode(),
        "Metadata": store._metadata(ref),
    }
    with Stubber(client) as stub:
        stub.add_response("put_object", {"ETag": '"test"'}, expected)
        store.put(ref, b"test-zip-bytes")
        stub.assert_no_pending_responses()


def test_get_stream_verifies_bytes_and_closes_body():
    ref = artifact()
    stream = io.BytesIO(b"test-zip-bytes")
    body = StreamingBody(stream, ref.size)
    client = Mock(get_object=Mock(return_value=response(ref, Body=body)))
    assert S3SourceArtifactStore(settings(), client=client).get(ref) == b"test-zip-bytes"
    assert stream.closed
    client.get_object.assert_called_once_with(
        Bucket=settings().bucket, Key=ref.key, ExpectedBucketOwner=settings().account_id
    )


def test_duplicate_conditional_put_only_succeeds_after_full_verification():
    ref = artifact()
    duplicate = ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
    client = Mock(put_object=Mock(side_effect=duplicate), get_object=Mock(return_value=response(ref)))
    S3SourceArtifactStore(settings(), client=client).put(ref, b"test-zip-bytes")
    assert client.put_object.call_count == 1 and client.get_object.call_count == 1
    client.get_object.return_value = response(ref, data=b"changed-bytes!")
    with pytest.raises(ValueError):
        S3SourceArtifactStore(settings(), client=client).put(ref, b"test-zip-bytes")


@pytest.mark.parametrize(
    "changes",
    [{"ContentLength": 0}, {"ContentLength": True}, {"Metadata": {}}, {"ServerSideEncryption": None}],
)
def test_invalid_response_is_rejected_and_closed_before_read(changes):
    ref = artifact()
    body = io.BytesIO(b"test-zip-bytes")
    client = Mock(get_object=Mock(return_value=response(ref, Body=body, **changes)))
    with pytest.raises(ValueError):
        S3SourceArtifactStore(settings(), client=client).get(ref)
    assert body.closed


@pytest.mark.parametrize("data", [b"short", b"test-zip-bytes-extra", b"wrong-zip-body"])
def test_length_or_checksum_mismatch_rejected(data):
    ref = artifact()
    body = io.BytesIO(data)
    client = Mock(get_object=Mock(return_value=response(ref, data, Body=body)))
    with pytest.raises(ValueError):
        S3SourceArtifactStore(settings(), client=client).get(ref)
    assert body.closed


def test_download_reads_at_most_declared_size_plus_one():
    ref = artifact()

    class Body:
        def __init__(self):
            self.requested = []
            self.closed = False

        def read(self, size):
            self.requested.append(size)
            return b"x" * size

        def close(self):
            self.closed = True

    body = Body()
    client = Mock(get_object=Mock(return_value=response(ref, Body=body)))
    with pytest.raises(ValueError):
        S3SourceArtifactStore(settings(), client=client).get(ref)
    assert sum(body.requested) == ref.size + 1 and body.closed


def test_missing_object_maps_to_not_found_and_iam_failure_is_redacted():
    ref = artifact()
    client = Mock(get_object=Mock(side_effect=ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")))
    store = S3SourceArtifactStore(settings(), client=client)
    with pytest.raises(FileNotFoundError):
        store.get(ref)
    client.get_object.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "private"}}, "GetObject"
    )
    with pytest.raises(OSError) as error:
        store.get(ref)
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    "error",
    [
        EndpointConnectionError(endpoint_url="private-endpoint"),
        ClientError({"Error": {"Code": "ConditionalRequestConflict", "Message": "private"}}, "PutObject"),
    ],
)
def test_ambiguous_upload_never_blindly_retries(error):
    client = Mock(put_object=Mock(side_effect=error))
    with pytest.raises(OSError, match="uncertain") as failure:
        S3SourceArtifactStore(settings(), client=client).put(artifact(), b"test-zip-bytes")
    assert client.put_object.call_count == 1
    assert "private" not in str(failure.value)
    client.get_object.assert_not_called()


def test_invalid_put_bytes_do_not_call_aws():
    client = Mock()
    with pytest.raises(ValueError):
        S3SourceArtifactStore(settings(), client=client).put(artifact(), b"bad")
    client.put_object.assert_not_called()


def test_metadata_mismatch_including_tree_hash_is_rejected():
    ref = artifact()
    wrong = replace(ref, source_digest="c" * 64)
    client = Mock(get_object=Mock(return_value=response(ref)))
    with pytest.raises(ValueError):
        S3SourceArtifactStore(settings(), client=client).get(wrong)


@pytest.mark.parametrize(
    "bucket", ["", "a", "https://evil.example", "127.0.0.1", "A-bucket", "a..bucket", "a.-bucket"]
)
def test_invalid_bucket_configuration(bucket):
    with pytest.raises(ValueError):
        S3ArtifactSettings(bucket, "ap-northeast-2", "977889523182")


def test_settings_read_only_explicit_infra_variables():
    assert (
        S3ArtifactSettings.from_environment(
            {
                "SKY_ARTIFACTS_BUCKET": "sky-test-artifacts",
                "SKY_AWS_REGION": "ap-northeast-2",
                "SKY_AWS_ACCOUNT_ID": "977889523182",
            }
        )
        == settings()
    )
    with pytest.raises(ValueError):
        S3ArtifactSettings.from_environment({})


def test_malformed_body_has_a_stable_integrity_error():
    ref = artifact()
    client = Mock(get_object=Mock(return_value=response(ref, Body=1)))
    with pytest.raises(ValueError, match="stored source"):
        S3SourceArtifactStore(settings(), client=client).get(ref)
