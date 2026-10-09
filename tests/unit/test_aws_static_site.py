"""The static target must reject mixed apps and foreign AWS ownership."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adapters.aws.ecs import AwsConfigurationError, AwsSettings
from adapters.aws.static_site import AwsStaticSiteAdapter

ACCOUNT = "123456789012"
ATTEMPT = "0123456789abcdef-a1"


class StaticSiteAdapterTests(unittest.TestCase):
    def make_adapter(self, account=ACCOUNT):
        calls = []

        def command(args, timeout=300):
            calls.append(args)
            if args[:2] == ["sts", "get-caller-identity"]:
                return {"Account": account}
            raise AssertionError(f"Unexpected AWS request: {args}")

        return AwsStaticSiteAdapter(AwsSettings("ap-northeast-2", expected_account=ACCOUNT,
                                               account_pin_required=True), command=command), calls

    def test_static_preflight_is_read_only_and_source_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "index.html").write_text("<h1>Hello</h1>")
            adapter, calls = self.make_adapter()
            result = adapter.preflight(project, "hello-site", ATTEMPT)
        self.assertEqual(result["target"], "aws-s3-cloudfront")
        self.assertEqual(result["file_count"], 1)
        self.assertEqual(len(result["source_digest"]), 64)
        self.assertEqual(calls, [["sts", "get-caller-identity"]])

    def test_mixed_backend_is_rejected_before_aws_call(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "index.html").write_text("<h1>Hello</h1>")
            (project / "server.js").write_text("const http = require('node:http');")
            adapter, calls = self.make_adapter()
            with self.assertRaisesRegex(ValueError, "정적 파일만으로"):
                adapter.preflight(project, "hello-site", ATTEMPT)
        self.assertEqual(calls, [])

    def test_wrong_account_is_rejected_before_create(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "index.html").write_text("<h1>Hello</h1>")
            adapter, calls = self.make_adapter("999999999999")
            with self.assertRaises(AwsConfigurationError):
                adapter.deploy(project, "hello-site", ATTEMPT)
        self.assertEqual(calls, [["sts", "get-caller-identity"]])

    def test_foreign_stack_arn_is_rejected_before_aws_call(self):
        adapter, calls = self.make_adapter()
        with self.assertRaises(AwsConfigurationError):
            adapter.retire("hello-site", ATTEMPT, "arn:aws:cloudformation:ap-northeast-2:999999999999:stack/other/x")
        self.assertEqual(calls, [])

    def test_owned_stack_upload_requires_exact_public_index(self):
        stack = ("arn:aws:cloudformation:ap-northeast-2:123456789012:"
                 f"stack/sky-static-{ATTEMPT}/generated")
        bucket = f"sky-static-{ACCOUNT}-ap-northeast-2-{ATTEMPT}"
        calls = []

        def command(args, timeout=300):
            calls.append(args)
            if args[:2] == ["sts", "get-caller-identity"]:
                return {"Account": ACCOUNT}
            if args[:2] == ["cloudformation", "create-stack"]:
                return {"StackId": stack}
            if args[:2] == ["cloudformation", "wait"]:
                return {}
            if args[:2] == ["cloudformation", "describe-stacks"]:
                return {"Stacks": [{"StackId": stack, "StackStatus": "CREATE_COMPLETE",
                                    "Tags": [{"Key": "sky-managed", "Value": "true"},
                                             {"Key": "sky-app", "Value": "hello-site"},
                                             {"Key": "sky-attempt", "Value": ATTEMPT}],
                                    "Outputs": [{"OutputKey": "BucketName", "OutputValue": bucket},
                                                {"OutputKey": "DistributionId", "OutputValue": "E12345678"},
                                                {"OutputKey": "DomainName", "OutputValue": "d123.cloudfront.net"}]}]}
            if args[:2] == ["s3api", "put-object"]:
                return {"ETag": '"abc"'}
            raise AssertionError(f"Unexpected AWS request: {args}")

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self, _limit):
                return b"<h1>Hello</h1>"

        class Opener:
            def open(self, request, timeout):
                self.request = request
                return Response()

        opener = Opener()
        checkpoints = []
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "index.html").write_text("<h1>Hello</h1>")
            adapter = AwsStaticSiteAdapter(AwsSettings("ap-northeast-2", expected_account=ACCOUNT,
                                                       account_pin_required=True), command=command,
                                           checkpoint=lambda **updates: checkpoints.append(updates))
            with patch("adapters.aws.static_site.urllib.request.build_opener", return_value=opener):
                result = adapter.deploy(project, "hello-site", ATTEMPT)
        self.assertEqual(result["url"], "https://d123.cloudfront.net")
        self.assertEqual(result["stack_id"], stack)
        self.assertEqual(opener.request.full_url, "https://d123.cloudfront.net/")
        self.assertEqual([args[:2] for args in calls].count(["s3api", "put-object"]), 1)
        self.assertEqual(checkpoints, [{"static_stack_id": stack}])


if __name__ == "__main__":
    unittest.main()
