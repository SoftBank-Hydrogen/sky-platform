"""Smoke the A-phase service image without AWS credentials or OpenAI calls.

--full runs on Linux with host networking and a host Docker socket. The shared
temporary path must have the same name inside Sky and on the Docker host.
"""

import argparse
import io
import json
import os
import re
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path


def docker(*args, check=True):
    return subprocess.run(
        ["docker", *args], check=check, capture_output=True, text=True, timeout=300
    ).stdout.strip()


def restore_temp_ownership(image, shared):
    if os.name != "posix":
        return
    # Sky runs as root for Docker access and can create private root-owned state.
    # Only this smoke's temporary tree is mounted; no socket or host networking.
    docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--user",
        "0:0",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "CHOWN",
        "--cap-add",
        "DAC_OVERRIDE",
        "--security-opt",
        "no-new-privileges",
        "--mount",
        f"type=bind,source={shared},target=/smoke",
        "--entrypoint",
        "chown",
        image,
        "--recursive",
        "--no-dereference",
        "--",
        f"{os.getuid()}:{os.getgid()}",
        "/smoke",
    )


def expect_state_rejection(*args):
    try:
        docker(*args)
    except subprocess.CalledProcessError as error:
        assert error.returncode == 2 and "Sky service state:" in error.stderr, error.stderr
    else:
        raise AssertionError("Service started without initialized persistent state")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="sky-platform:smoke")
    parser.add_argument("--full", action="store_true")
    options = parser.parse_args()
    name = "sky-service-smoke-" + uuid.uuid4().hex[:12]
    job_id = None
    with tempfile.TemporaryDirectory(prefix=name + "-") as directory:
        shared = Path(directory).resolve()
        state = shared / "state"
        state.mkdir()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        args = [
            "run",
            "-d",
            "--name",
            name,
            "--label",
            "sky-service-smoke=" + name,
            "--mount",
            f"type=bind,source={state},target=/.sky",
        ]
        if options.full:
            args += [
                "--network",
                "host",
                "--mount",
                "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
                "--mount",
                f"type=bind,source={shared},target={shared}",
                "-e",
                f"TMPDIR={shared}",
            ]
        else:
            args += ["-p", f"127.0.0.1:{port}:8080"]
        args += [
            options.image,
            "--host",
            "0.0.0.0",
            "--port",
            str(port if options.full else 8080),
            "--state-dir",
            "/.sky",
            "--monitor-interval",
            "0",
            "--github-poll-interval",
            "0",
            # Forwarded server flags must not override the guarded state path.
            "--state-d",
            "/tmp/sky-smoke-unverified-state",
        ]
        base = f"http://127.0.0.1:{port}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        token = ""

        def request(path, data=None):
            req = urllib.request.Request(base + path, data=data, headers={"X-Sky-Token": token})
            with opener.open(req, timeout=30) as response:
                return json.load(response)

        def ready():
            nonlocal token
            for _ in range(60):
                try:
                    assert request("/health") == {"status": "ok"}
                    with opener.open(base, timeout=3) as response:
                        html = response.read().decode()
                    match = re.search(r"const token='([^']+)'", html)
                    assert match and "Sky" in html
                    token = match.group(1)
                    return
                except (OSError, urllib.error.HTTPError):
                    time.sleep(1)
            raise AssertionError("Sky service did not become ready")

        try:
            probe = [
                "run",
                "--rm",
                "--network",
                "none",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
            ]
            expect_state_rejection(*probe, options.image, "--state-dir", "/.sky")
            state_mount = f"type=bind,source={state},target=/.sky"
            expect_state_rejection(*probe, "--mount", state_mount, options.image, "--state-dir", "/.sky")
            assert list(state.iterdir()) == [], "Rejected startup changed the uninitialized state"
            # Initialization only mounts the fresh state directory; no Docker socket.
            docker(
                *probe,
                "--cap-add",
                "DAC_OVERRIDE",
                "--mount",
                state_mount,
                options.image,
                "--initialize-state",
                "--state-dir",
                "/.sky",
            )
            expect_state_rejection(
                *probe, "--mount", state_mount, options.image, "--initialize-state", "--state-dir", "/.sky"
            )
            docker(*args)
            ready()
            docker(
                "exec",
                name,
                "python",
                "-c",
                "from pathlib import Path; assert not Path('/tmp/sky-smoke-unverified-state').exists()",
            )
            assert not request("/api/config")["ai_available"]
            docker("exec", name, "aws", "--version")
            docker("exec", name, "docker", "--version")
            docker("exec", name, "docker", "compose", "version")
            # The health route is public, but it must not weaken API authentication.
            try:
                opener.open(base + "/api/jobs", timeout=3)
                raise AssertionError("Unauthenticated API access was accepted")
            except urllib.error.HTTPError as error:
                assert error.code == 403
                error.close()
            if options.full:
                archive = io.BytesIO()
                with zipfile.ZipFile(archive, "w") as output:
                    for path in Path("tests/fixtures/apps/hello-node").iterdir():
                        output.write(path, path.name)
                planned = request("/api/analyze", archive.getvalue())
                job_id = planned["id"]
                request("/api/deploy/" + job_id, b"{}")
                for _ in range(180):
                    job = request("/api/jobs/" + job_id)
                    if job["status"] != "running":
                        break
                    time.sleep(1)
                assert job["status"] == "succeeded", "Fixture deployment failed"
                with opener.open(job["result"]["url"], timeout=5) as response:
                    assert json.load(response)["message"] == "Hello from Sky!"
                docker("restart", name)
                ready()
                assert request("/api/jobs/" + job_id)["status"] == "succeeded"
        except Exception:
            print(docker("logs", "--tail", "60", name, check=False))
            raise
        finally:
            docker("rm", "-f", name, check=False)
            if job_id:
                # Only resources produced by this smoke job, never a global prune.
                docker("rm", "-f", "sky-" + job_id, check=False)
                docker("image", "rm", "sky/" + job_id + ":latest", check=False)
            restore_temp_ownership(options.image, shared)

    print(
        "PASS: state volume guard, Sky HTTP/UI, API auth, AWS/Docker/Compose tools"
        + (", ZIP build/deploy and restart recovery (offline)" if options.full else " (UI smoke only)")
        + "; temporary resources cleaned"
    )


if __name__ == "__main__":
    main()
