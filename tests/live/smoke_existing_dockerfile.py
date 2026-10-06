"""Build and serve a non-Node ZIP app with its own Dockerfile; needs local Docker."""
import tempfile
import uuid
import urllib.request
import zipfile
from pathlib import Path

from application.deployment_core import LocalDockerAdapter, analyze, extract_project


def main():
    suffix = uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix="sky-custom-") as temporary:
        root = Path(temporary)
        archive = root / "app.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("server.py", "from http.server import BaseHTTPRequestHandler, HTTPServer\nimport os\nclass Handler(BaseHTTPRequestHandler):\n    def do_GET(self):\n        self.send_response(200)\n        self.send_header('Content-Type', 'application/json')\n        self.end_headers()\n        self.wfile.write(b'{\"custom\":true}')\nHTTPServer(('0.0.0.0', int(os.environ['PORT'])), Handler).serve_forever()\n")
            bundle.writestr("Dockerfile", "FROM python:3.13-alpine\nWORKDIR /app\nCOPY server.py ./\nCMD [\"python\", \"server.py\"]\n")
            bundle.writestr(".dockerignore", "coverage\n")
        project = extract_project(archive, root / "source")
        plan = analyze(project)
        assert plan.dockerfile_source == "existing"
        adapter = LocalDockerAdapter(lambda stage, message: print(f"[{stage}] {message}", flush=True))
        try:
            result = adapter.deploy(project, plan, suffix)
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(result["url"], timeout=5) as response:
                assert response.status == 200
                assert response.read() == b'{"custom":true}'
            assert (project / "Dockerfile").read_text() == plan.dockerfile
            assert "coverage\n" in (project / ".dockerignore").read_text()
            print("PASS: Python ZIP without package.json -> Docker build -> HTTP 200", flush=True)
        finally:
            adapter.cleanup_failure(suffix)


if __name__ == "__main__":
    main()
