"""Verify remote build results against ECR's current tag and manifest digest."""
class EcrBuildVerifier:
    def __init__(self, settings, *, client=None):
        self.settings = settings
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client("ecr", region_name=settings.region,
                config=Config(connect_timeout=5, read_timeout=20, retries={"total_max_attempts": 1}))
        self.client = client

    def verify(self, request, result):
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            response = self.client.batch_get_image(registryId=self.settings.account_id,
                repositoryName="sky-managed", imageIds=[{"imageTag": "build-"+request["build_id"]}])
        except (BotoCoreError, ClientError):
            raise OSError("Built ECR image verification unavailable") from None
        images = response.get("images", [])
        if response.get("failures") or len(images) != 1 or images[0].get("imageId", {}).get("imageDigest") != result["image_digest"]:
            raise ValueError("Built ECR image no longer matches the result")
