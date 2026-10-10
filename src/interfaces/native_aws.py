"""Explicit native AWS deployment command with durable private receipts."""

import argparse
import json
import os
from pathlib import Path

from adapters.aws.ecs import AwsSettings
from adapters.aws.native import AwsEc2Adapter, AwsLambdaAdapter


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Deploy a scoped Lambda ZIP or immutable EC2 image",
        epilog="Requires AWS account/region pin and SKY_AWS_ROLE_BOUNDARY_ARN. "
        "Install sky-platform[aws-native]. EC2 requires an existing public subnet; "
        "neither backend provisions databases or changes your original source.",
    )
    parser.add_argument("backend", choices=["lambda", "ec2"])
    parser.add_argument("action", choices=["deploy", "verify", "destroy"])
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--application-id")
    parser.add_argument("--attempt-id")
    parser.add_argument("--project", type=Path)
    parser.add_argument("--handler", default="handler.handler")
    parser.add_argument("--image")
    parser.add_argument("--subnet-id")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--public-access", action="store_true")
    parser.add_argument("--stateless", action="store_true")
    options = parser.parse_args(argv)
    path = options.receipt.absolute()
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        parser.error("Receipt must not use symbolic links")
    if options.action == "deploy":
        if path.exists():
            parser.error("Receipt already exists; verify or destroy that attempt before a new deployment")
        if not options.application_id or not options.attempt_id:
            parser.error("Deploy requires --application-id and --attempt-id")
        if options.backend == "lambda" and not options.project:
            parser.error("Lambda requires --project")
        if options.backend == "ec2" and (not options.image or not options.subnet_id):
            parser.error("EC2 requires --image and --subnet-id")
    path.parent.mkdir(parents=True, exist_ok=True)

    def checkpoint(receipt):
        temporary = path.with_name(path.name + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(receipt, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    adapter_type = AwsLambdaAdapter if options.backend == "lambda" else AwsEc2Adapter
    try:
        adapter = adapter_type(AwsSettings.from_environment(), checkpoint=checkpoint)
        if options.action == "deploy":
            # Reserve the attempt once, before mutations; concurrent deploys cannot overwrite it.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump({"target": "aws-" + options.backend, "status": "reserved"}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            if options.backend == "lambda":
                result = adapter.deploy(
                    options.project,
                    options.application_id,
                    options.attempt_id,
                    handler=options.handler,
                    public_access=options.public_access,
                )
            else:
                result = adapter.deploy(
                    options.image,
                    options.application_id,
                    options.attempt_id,
                    subnet_id=options.subnet_id,
                    port=options.port,
                    public_access=options.public_access,
                    stateless=options.stateless,
                )
        else:
            receipt = json.loads(path.read_text())
            if receipt.get("target") != "aws-" + options.backend:
                raise ValueError("Receipt backend differs from requested backend")
            result = getattr(adapter, options.action)(receipt)
    except Exception as error:  # noqa: BLE001 - CLI boundary masks credential-bearing SDK errors
        # Keep receipt for recovery; never auto-delete resources after an uncertain write.
        reason = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
        parser.exit(1, f"Native AWS operation failed: {reason}; retained receipt: {path}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
