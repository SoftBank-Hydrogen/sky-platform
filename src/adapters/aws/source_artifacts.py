"""Bounded S3 source objects with conditional creation and content verification."""

import base64
import hashlib
import ipaddress
import os
import re
from dataclasses import dataclass

from ports.artifacts import SourceArtifact


@dataclass(frozen=True)
class S3ArtifactSettings:
    bucket: str
    region: str
    account_id: str

    def __post_init__(self):
        if (not isinstance(self.bucket, str) or not 3 <= len(self.bucket) <= 63
                or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*[a-z0-9]", self.bucket)
                or any(x in self.bucket for x in ("..", ".-", "-."))):
            raise ValueError("Invalid artifacts bucket")
        try:
            ipaddress.ip_address(self.bucket)
        except ValueError:
            pass
        else:
            raise ValueError("Artifacts bucket cannot be an IP address")
        if not isinstance(self.region, str) or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", self.region):
            raise ValueError("Invalid artifacts region")
        if not isinstance(self.account_id, str) or not re.fullmatch(r"\d{12}", self.account_id):
            raise ValueError("Invalid artifacts account")

    @classmethod
    def from_environment(cls, environment=None):
        values = os.environ if environment is None else environment
        return cls(values.get("SKY_ARTIFACTS_BUCKET", ""), values.get("SKY_AWS_REGION", ""),
                   values.get("SKY_AWS_ACCOUNT_ID", ""))


class S3SourceArtifactStore:
    def __init__(self, settings: S3ArtifactSettings, *, client=None):
        self.settings = settings
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client("s3", region_name=settings.region, config=Config(
                connect_timeout=5, read_timeout=20,
                retries={"mode": "standard", "total_max_attempts": 1}))
        self.client = client

    @staticmethod
    def _metadata(artifact):
        return {"sha256": artifact.sha256, "source-digest": artifact.source_digest,
                "organization-id": artifact.organization_id, "application-id": artifact.application_id,
                "upload-id": artifact.upload_id, "artifact-kind": artifact.kind}

    @staticmethod
    def _validate(artifact, data):
        if (not isinstance(artifact, SourceArtifact) or not isinstance(data, bytes)
                or len(data) != artifact.size or hashlib.sha256(data).hexdigest() != artifact.sha256):
            raise ValueError("Source object size or digest mismatch")

    def put(self, artifact, data):
        from botocore.exceptions import BotoCoreError, ClientError

        self._validate(artifact, data)
        try:
            self.client.put_object(Bucket=self.settings.bucket, Key=artifact.key, Body=data,
                                   ContentLength=artifact.size, ContentType="application/zip",
                                   ServerSideEncryption="AES256", ExpectedBucketOwner=self.settings.account_id,
                                   IfNoneMatch="*", ChecksumSHA256=base64.b64encode(
                                       bytes.fromhex(artifact.sha256)).decode(),
                                   Metadata=self._metadata(artifact))
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code in {"PreconditionFailed", "412"}:
                # Another request may have stored it, including an earlier timed-out send.
                # Only verified identical bytes/metadata make this idempotent success.
                self.get(artifact)
                return
            raise OSError("Source upload failed; outcome may be uncertain") from None
        except BotoCoreError:
            raise OSError("Source upload failed; outcome may be uncertain") from None

    def get(self, artifact):
        from botocore.exceptions import BotoCoreError, ClientError

        if not isinstance(artifact, SourceArtifact):
            raise ValueError("Invalid source artifact")
        body = None
        try:
            response = self.client.get_object(Bucket=self.settings.bucket, Key=artifact.key,
                                              ExpectedBucketOwner=self.settings.account_id)
            body = response.get("Body")
            length = response.get("ContentLength")
            if (type(length) is not int or length != artifact.size
                    or response.get("Metadata") != self._metadata(artifact)
                    or response.get("ServerSideEncryption") != "AES256"
                    or not callable(getattr(body, "read", None))):
                raise ValueError("Invalid stored source object")
            chunks = bytearray()
            while len(chunks) <= artifact.size:
                requested = min(1024 * 1024, artifact.size + 1 - len(chunks))
                part = body.read(requested)
                if not isinstance(part, bytes) or len(part) > requested:
                    raise ValueError("Invalid stored source body")
                if not part:
                    break
                chunks.extend(part)
            data = bytes(chunks)
            self._validate(artifact, data)
            return data
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                raise FileNotFoundError("Source object is unavailable") from None
            raise OSError("Source download failed") from None
        except (BotoCoreError, OSError):
            raise OSError("Source download failed") from None
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                try:
                    close()
                except OSError:
                    pass
