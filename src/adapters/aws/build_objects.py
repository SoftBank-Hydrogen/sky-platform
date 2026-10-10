"""Bounded immutable S3 build inputs/results, confined to infra prefix contracts."""
import json
from ports.remote_builds import canonical, digest, request_key


class S3BuildObjects:
    def __init__(self, settings, *, client=None):
        from adapters.aws.source_artifacts import S3SourceArtifactStore
        self.settings = settings
        self.client = client if client is not None else S3SourceArtifactStore(settings).client

    def read(self, key):
        from botocore.exceptions import BotoCoreError, ClientError
        stream = None
        try:
            result = self.client.get_object(Bucket=self.settings.bucket, Key=key, ExpectedBucketOwner=self.settings.account_id)
            stream = result["Body"]
            if result.get("ContentLength", 0) > 65536:
                raise ValueError("Build document exceeds limit")
            body = stream.read(65537)
            if len(body) > 65536:
                raise ValueError("Build document exceeds limit")
            def unique(pairs):
                value = {}
                for name, item in pairs:
                    if name in value:
                        raise ValueError("Duplicate build field")
                    value[name] = item
                return value
            value = json.loads(body, object_pairs_hook=unique)
            canonical(value)
            return value
        except (BotoCoreError, ClientError):
            raise OSError("Build object is unavailable") from None
        finally:
            if stream is not None:
                stream.close()

    def put(self, key, value):
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            self.client.put_object(Bucket=self.settings.bucket, Key=key, Body=canonical(value),
                ContentType="application/json", ServerSideEncryption="AES256", IfNoneMatch="*",
                ExpectedBucketOwner=self.settings.account_id)
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
                raise OSError("Build object write outcome is uncertain") from None
            if digest(self.read(key)) != digest(value):
                raise ValueError("Build object identity already belongs to another document") from None
        except BotoCoreError:
            raise OSError("Build object write outcome is uncertain") from None

    def put_request(self, request):
        self.put(request_key(request), request)

    def result(self, request):
        return self.read(f"builds/{request['build_id']}/result.json")
