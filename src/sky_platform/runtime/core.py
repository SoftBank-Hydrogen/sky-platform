from __future__ import annotations

import ast
import io
import json
import hashlib
from email import policy
from email.parser import BytesParser
import re
import shutil
import stat
import subprocess
import tempfile
import time
import tokenize
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath


MAX_UPLOAD = 20 * 1024 * 1024
MAX_EXTRACTED = 100 * 1024 * 1024
MAX_PACKAGE_BYTES = 1024 * 1024
RDS_CA_CONTAINER_PATH = "/app/.sky-rds-ca.pem"
IGNORED = {"node_modules", ".venv", "venv", "__pycache__", ".git", ".env", ".sky", "__MACOSX"}
SOURCE_SUFFIXES = {".js", ".cjs", ".mjs", ".ts", ".tsx", ".jsx", ".json",
                   ".py", ".rb", ".go", ".php", ".java", ".kt", ".cs", ".rs",
                   ".sh", ".html", ".css", ".toml", ".yaml", ".yml", ".ru"}
SOURCE_FILENAMES = {"Gemfile", "Pipfile", "Procfile", "go.mod", "requirements.txt"}
ANALYSIS_SUFFIXES = {".js", ".cjs", ".mjs", ".ts", ".tsx", ".jsx", ".py", ".rb",
                     ".go", ".php", ".java", ".kt", ".cs", ".rs", ".sh", ".ru", ".prisma"}


def validate_environment(values: dict | None, required: list[str]) -> dict[str, str]:
    values = {} if values is None else values
    if not isinstance(values, dict) or len(values) > 40:
        raise ValueError("Environment must be an object with at most 40 entries")
    for name, value in values.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,63}", name):
            raise ValueError("Environment names must use uppercase letters, digits and underscores")
        if name in {"PORT", "NODE_ENV"}:
            raise ValueError("PORT and NODE_ENV are managed by the deployment plan")
        if not isinstance(value, str) or len(value) > 8192 or any(c in value for c in "\r\n\x00"):
            raise ValueError("Environment values must be single-line strings up to 8192 characters")
    missing = [name for name in required if not values.get(name)]
    if missing:
        raise ValueError("Required environment values are not configured: " + ", ".join(missing))
    return dict(values)


def ignored_source_path(path: PurePosixPath) -> bool:
    return any(part in IGNORED or part.startswith('.env.') for part in path.parts)


def folder_upload_to_zip(body: bytes, content_type: str, archive: Path) -> None:
    """Convert paired browser folder paths/files into the existing bounded ZIP pipeline."""
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + content_type.encode('ascii', errors='strict')
        + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
    if message.get_content_type() != 'multipart/form-data' or not message.get_boundary() or not message.is_multipart():
        raise ValueError('Invalid folder upload')
    parts = message.get_payload()
    if not isinstance(parts, list) or not parts or len(parts) > 10000 or len(parts) % 2:
        raise ValueError('Invalid folder upload file count')
    seen = set()
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED) as bundle:
        for index in range(0, len(parts), 2):
            path_part, file_part = parts[index:index + 2]
            if (path_part.is_multipart() or file_part.is_multipart()
                    or path_part.get_param('name', header='content-disposition') != 'path'
                    or file_part.get_param('name', header='content-disposition') != 'file'
                    or path_part.get_content_disposition() != 'form-data'
                    or file_part.get_content_disposition() != 'form-data'):
                raise ValueError('Invalid folder upload fields')
            try:
                raw_path = path_part.get_payload(decode=True).decode('utf-8')
            except (UnicodeDecodeError, AttributeError):
                raise ValueError('Invalid folder upload path') from None
            path = PurePosixPath(raw_path)
            if (not raw_path or len(raw_path) > 1024 or any(ord(char) < 32 for char in raw_path)
                    or path.is_absolute() or path.as_posix() != raw_path
                    or '\\' in raw_path or '..' in path.parts or path.name in {'', '.'}
                    or raw_path in seen):
                raise ValueError('Unsafe or duplicate folder upload path')
            seen.add(raw_path)
            data = file_part.get_payload(decode=True)
            if data is None:
                raise ValueError('Invalid folder upload file')
            if not ignored_source_path(path):
                bundle.writestr(raw_path, data)


def extract_project(archive: Path, destination: Path) -> Path:
    """Extract a bounded archive without links, traversal or local secrets."""
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) > 5000 or sum(i.file_size for i in entries) > MAX_EXTRACTED:
            raise ValueError("ZIP exceeds the extracted size or file count limit")
        seen = set()
        files = set()
        directories = set()
        for item in entries:
            path = PurePosixPath(item.filename)
            if (not path.parts or path.is_absolute() or ".." in path.parts
                    or "\\" in item.filename):
                raise ValueError("Unsafe ZIP path")
            mode = item.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError("ZIP symbolic links are not supported")
            if ignored_source_path(path):
                continue
            name = path.as_posix()
            parents = {parent.as_posix() for parent in path.parents if parent.as_posix() != '.'}
            if name in seen or parents.intersection(files) or (not item.is_dir() and name in directories):
                raise ValueError("ZIP contains duplicate or conflicting paths")
            seen.add(name)
            directories.update(parents)
            if item.is_dir():
                directories.add(name)
            else:
                files.add(name)
        for item in entries:
            path = PurePosixPath(item.filename)
            if ignored_source_path(path):
                continue
            target = destination.joinpath(*path.parts)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(item) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    entrypoints = ("package.json", "Dockerfile", "server.py", "app.py", "main.py")
    if any((destination / name).is_file() for name in entrypoints):
        return destination
    candidates = {path.parent for name in entrypoints
                  for path in destination.glob(f"*/{name}")}
    if len(candidates) != 1:
        raise ValueError("Include package.json, Dockerfile, server.py, app.py or main.py at ZIP root or in one top-level folder")
    return candidates.pop()


@dataclass
class DeploymentPlan:
    runtime: str
    start_command: str
    build_command: str | None
    port: int
    dockerfile: str
    target: str = "local-docker"
    analyzer: str = "static"
    health_path: str = "/"
    framework: str = "nodejs"
    rationale: str = "package.json scripts를 사용한 정적 배포 계획입니다."
    warnings: list[str] = field(default_factory=list)
    required_env: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    source_digest: str = ""
    model: str | None = None
    dockerfile_source: str = "generated"


def source_digest(project: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(project.rglob("*")):
        if path.is_symlink():
            raise ValueError("Project symbolic links are not supported")
        if path.is_file():
            relative = path.relative_to(project).as_posix().encode()
            digest.update(len(relative).to_bytes(8, "big") + relative)
            size = path.stat().st_size
            digest.update(size.to_bytes(8, "big"))
            with path.open('rb') as source:
                remaining = size
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("Project source changed while hashing")
                    digest.update(chunk)
                    remaining -= len(chunk)
                if source.read(1):
                    raise ValueError("Project source changed while hashing")
    return digest.hexdigest()


def read_package(project: Path) -> dict:
    with (project / "package.json").open('rb') as source:
        content = source.read(MAX_PACKAGE_BYTES + 1)
    if len(content) > MAX_PACKAGE_BYTES:
        raise ValueError("package.json exceeds the 1 MiB analysis limit")
    package = json.loads(content.decode('utf-8'))
    if not isinstance(package, dict) or not isinstance(package.get("scripts", {}), dict):
        raise ValueError("package.json must contain a scripts object")
    return package


def read_requirements(project: Path) -> str | None:
    requirements = project / "requirements.txt"
    if not requirements.is_file():
        return None
    with requirements.open("rb") as source:
        content = source.read(MAX_PACKAGE_BYTES + 1)
    if len(content) > MAX_PACKAGE_BYTES:
        raise ValueError("requirements.txt exceeds the 1 MiB analysis limit")
    return content.decode('utf-8')


def has_python_requirement(project: Path, package: str) -> bool:
    content = read_requirements(project)
    if content is None:
        return False
    return bool(re.search(rf"(?im)^[ \t]*{re.escape(package)}(?:\[[a-z0-9_,.-]+\])?"
                          r"(?:[<>=!~][^\r\n]*)?[ \t]*(?:#.*)?$", content))


def python_app_constructor(path: Path) -> str | None:
    """Inspect module-level app assignment without executing uploaded Python code."""
    with path.open('rb') as source:
        content = source.read(MAX_PACKAGE_BYTES + 1)
    if len(content) > MAX_PACKAGE_BYTES:
        raise ValueError(f"{path.name} exceeds the 1 MiB static analysis limit")
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(content).readline)
        module = ast.parse(content.decode(encoding), filename=path.name)
    except (SyntaxError, UnicodeDecodeError, ValueError):
        raise ValueError(f"{path.name} has invalid Python syntax for static analysis") from None
    constructors = []
    for statement in module.body:
        value = None
        if isinstance(statement, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'app'
                                                       for target in statement.targets):
            value = statement.value
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name) \
                and statement.target.id == 'app':
            value = statement.value
        if value is None or isinstance(value, (ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Set)):
            continue
        constructor = value.func if isinstance(value, ast.Call) else None
        constructors.append(constructor.id if isinstance(constructor, ast.Name) else 'unknown')
    if len(constructors) > 1:
        raise ValueError(f"{path.name} defines app more than once; choose a start_script with AI analysis")
    return constructors[0] if constructors else None


def make_plan(project: Path, start_script: str, build_script: str | None,
              port: int = 3000, health_path: str = "/", **metadata) -> DeploymentPlan:
    existing_dockerfile = project / "Dockerfile"
    custom = existing_dockerfile.is_file()
    python = not custom and not (project / "package.json").is_file()
    asgi = python and isinstance(start_script, str) and start_script.startswith("asgi:")
    wsgi = python and isinstance(start_script, str) and start_script.startswith("wsgi:")
    if custom:
        if start_script != "dockerfile" or build_script is not None:
            raise ValueError("Existing Dockerfile requires start_script=dockerfile and build_script=null")
    elif python:
        entry = start_script[5:] if asgi or wsgi else start_script
        if (entry not in {"server.py", "app.py", "main.py"} or build_script is not None
                or not (project / entry).is_file()):
            raise ValueError("Python app requires an existing server.py, app.py or main.py and build_script=null")
        if asgi and not has_python_requirement(project, "uvicorn"):
            raise ValueError("ASGI app requires uvicorn in requirements.txt")
        if wsgi and not has_python_requirement(project, "gunicorn"):
            raise ValueError("WSGI app requires gunicorn in requirements.txt")
        if not asgi and not wsgi:
            read_requirements(project)
    else:
        scripts = read_package(project).get("scripts", {})
        if not isinstance(start_script, str):
            raise ValueError("A start script is required")
        for script in [start_script] + ([build_script] if build_script is not None else []):
            if (not isinstance(script, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:_-]{0,63}", script)
                    or not isinstance(scripts.get(script), str) or not scripts[script].strip()):
                raise ValueError("Plan must select an existing, valid npm script")
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Container port must be an integer between 1024 and 65535")
    if (not isinstance(health_path, str) or len(health_path) > 200
            or not re.fullmatch(r"/[A-Za-z0-9/_.-]*", health_path)
            or "//" in health_path or ".." in health_path):
        raise ValueError("Health path must be an absolute local HTTP path")
    if custom:
        with existing_dockerfile.open(encoding='utf-8') as source:
            dockerfile = source.read(40001)
        if not dockerfile.strip() or len(dockerfile) > 40000:
            raise ValueError("Existing Dockerfile must contain 1 to 40,000 characters")
        metadata.setdefault("framework", "container")
        metadata.setdefault("rationale", "업로드한 Dockerfile을 사용하고 실제 빌드·HTTP 응답으로 검증합니다.")
        return DeploymentPlan("custom-dockerfile", "Dockerfile CMD/ENTRYPOINT", None, port,
                              dockerfile, health_path=health_path,
                              source_digest=source_digest(project),
                              dockerfile_source="existing", **metadata)
    if python:
        install = (("RUN pip install --no-cache-dir -r requirements.txt\n")
                   if (project / "requirements.txt").is_file() else "")
        if asgi:
            start_args = ["python", "-m", "uvicorn", f"{entry[:-3]}:app",
                          "--host", "0.0.0.0", "--port", str(port)]
        elif wsgi:
            start_args = ["python", "-m", "gunicorn", "--bind", f"0.0.0.0:{port}",
                          "--workers", "1", "--access-logfile", "-", f"{entry[:-3]}:app"]
        else:
            start_args = ["python", entry]
        dockerfile = (
            "FROM python:3.13-slim-bookworm\nWORKDIR /app\n"
            "RUN useradd --uid 10001 --create-home app\n"
            "COPY --chown=app:app . .\n"
            + install
            + f"ENV PYTHONUNBUFFERED=1 PORT={port}\nEXPOSE {port}\n"
            + "USER app\n"
            + f"CMD {json.dumps(start_args)}\n"
        )
        runtime = "python-asgi" if asgi else "python-wsgi" if wsgi else "python"
        metadata.setdefault("framework", runtime)
        return DeploymentPlan(runtime, " ".join(start_args), None, port, dockerfile,
                              health_path=health_path, source_digest=source_digest(project), **metadata)
    install = "npm ci" if (project / "package-lock.json").exists() else "npm install"
    build = f"npm run {build_script}" if build_script else None
    start_args = ["npm", "start"] if start_script == "start" else ["npm", "run", start_script]
    dockerfile = (
        "FROM node:22-bookworm-slim\nWORKDIR /app\nRUN chown node:node /app\n"
        "COPY --chown=node:node . .\nUSER node\n"
        f"RUN {install}\n"
        + (f"RUN {build}\n" if build else "")
        + f"ENV NODE_ENV=production\nENV PORT={port}\nEXPOSE {port}\n"
        + f"CMD {json.dumps(start_args)}\n"
    )
    return DeploymentPlan("nodejs", " ".join(start_args), build, port, dockerfile,
                          health_path=health_path, source_digest=source_digest(project), **metadata)


def analyze(project: Path) -> DeploymentPlan:
    if (project / "Dockerfile").is_file():
        return make_plan(project, "dockerfile", None,
                         warnings=["정적 분석은 PORT=3000, HTTP 검사 경로=/를 가정합니다. 기존 Dockerfile의 실행 설정을 확인하세요."])
    if not (project / "package.json").is_file():
        entries = [name for name in ("server.py", "app.py", "main.py")
                   if (project / name).is_file()]
        if not entries:
            raise ValueError("Python app requires server.py, app.py or main.py at project root")
        uvicorn = has_python_requirement(project, "uvicorn")
        gunicorn = has_python_requirement(project, "gunicorn")
        server_candidates = []
        ambiguous = []
        missing_server = []
        for entry in entries:
            constructor = python_app_constructor(project / entry)
            if constructor == 'Flask':
                if gunicorn:
                    server_candidates.append('wsgi:' + entry)
                else:
                    missing_server.append('Flask app requires gunicorn in requirements.txt')
            elif constructor in {'FastAPI', 'Starlette'}:
                if uvicorn:
                    server_candidates.append('asgi:' + entry)
                else:
                    missing_server.append('ASGI app requires uvicorn in requirements.txt')
            elif constructor and uvicorn and not gunicorn:
                server_candidates.append('asgi:' + entry)
            elif constructor and gunicorn and not uvicorn:
                server_candidates.append('wsgi:' + entry)
            elif constructor and uvicorn and gunicorn:
                ambiguous.append(entry)
        if len(server_candidates) + len(ambiguous) + len(missing_server) > 1:
            raise ValueError("Multiple Python app objects found; choose a start_script with AI analysis")
        if ambiguous:
            raise ValueError("Python app server type is ambiguous; choose a start_script with AI analysis")
        if missing_server:
            raise ValueError(missing_server[0])
        start = server_candidates[0] if server_candidates else entries[0]
        return make_plan(project, start, None,
                         warnings=["정적 분석은 PORT=3000, HTTP 검사 경로=/를 가정합니다. Python 서버 실행 설정을 확인하세요."])
    package = read_package(project)
    scripts = package.get("scripts", {})
    if not isinstance(scripts, dict) or not isinstance(scripts.get("start"), str):
        raise ValueError("This prototype requires a package.json start script")
    return make_plan(project, "start", "build" if scripts.get("build") else None,
                     warnings=["정적 분석은 PORT=3000, HTTP 검사 경로=/를 가정합니다."])


class ImageBuilder:
    """Shared image preparation for local and cloud execution targets."""
    def __init__(self, command, event):
        self.command, self.event = command, event

    def build(self, project: Path, plan: DeploymentPlan, image: str,
              platform: str | None = None, extra_ca_bundle: Path | None = None):
        if not plan.source_digest or source_digest(project) != plan.source_digest:
            raise ValueError("Source changed after analysis; upload and analyze again")
        if plan.dockerfile_source == "existing":
            if (project / "Dockerfile").read_text() != plan.dockerfile:
                raise ValueError("Existing Dockerfile changed after analysis")
        with tempfile.TemporaryDirectory(prefix='sky-build-') as temporary:
            context = Path(temporary) / 'app'
            shutil.copytree(project, context, symlinks=True)
            if source_digest(context) != plan.source_digest:
                raise ValueError("Source changed while preparing image; upload and analyze again")
            if plan.dockerfile_source != "existing":
                (context / "Dockerfile").write_text(plan.dockerfile)
            if extra_ca_bundle is not None:
                ca_name = '.sky-rds-ca.pem'
                shutil.copyfile(extra_ca_bundle, context / ca_name)
                dockerfile = context / 'Dockerfile'
                content = dockerfile.read_text()
                directive = (f'\nCOPY .sky-rds-ca.pem {RDS_CA_CONTAINER_PATH}\n'
                             f'ENV NODE_EXTRA_CA_CERTS={RDS_CA_CONTAINER_PATH}\n')
                if plan.runtime in {'python', 'python-asgi', 'python-wsgi'}:
                    directive += f'ENV PGSSLROOTCERT={RDS_CA_CONTAINER_PATH}\n'
                if directive not in content:
                    dockerfile.write_text(content.rstrip('\n') + directive)
            ignore = context / ".dockerignore"
            current_ignore = ignore.read_text() if ignore.exists() else ""
            ignore.write_text(current_ignore.rstrip("\n") + "\n.git\nnode_modules\n.venv\nvenv\n__pycache__\n.env\n.env.*\n"
                              + ("!.sky-rds-ca.pem\n" if extra_ca_bundle is not None else ""))
            self.event("building", "Building application image")
            args = ["docker", "build", "--label", "app=sky"]
            if platform:
                args += ["--platform", platform]
            self.command(args + ["-t", image, str(context)])
            if platform:
                try:
                    try:
                        actual = self.command([
                            "docker", "image", "inspect", "--platform", platform,
                            "--format", "{{.Os}}/{{.Architecture}}", image]).strip().lower()
                    except Exception as exc:
                        raise ValueError(f"빌드 이미지의 {platform} 플랫폼을 Docker에서 확인할 수 없습니다.") from exc
                    if actual != platform:
                        raise ValueError(f"빌드 이미지 플랫폼 불일치: {platform}이 필요하지만 {actual or '불명'}입니다.")
                    self.event("building", f"이미지 플랫폼 확인 완료: {actual}")
                except Exception:
                    try:
                        self.command(["docker", "image", "rm", image], timeout=30)
                    except Exception:
                        self.event("cleanup", "플랫폼 확인 실패 후 로컬 이미지 정리 확인 필요: " + image)
                    raise
        return image


class LocalDockerAdapter:
    def __init__(self, event):
        self.event = event

    def command(self, args: list[str], timeout: int = 300) -> str:
        self.event("command", " ".join(args))
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if result.stdout:
            self.event("output", result.stdout[-12000:])
        if result.stderr:
            self.event("output", result.stderr[-12000:])
        if result.returncode:
            raise RuntimeError(f"{args[0]} {args[1]} failed (exit {result.returncode}); see logs")
        return result.stdout.strip()

    def deploy(self, project: Path, plan: DeploymentPlan, job_id: str,
               environment: dict | None = None) -> dict:
        if not plan.source_digest or source_digest(project) != plan.source_digest:
            raise ValueError("Source changed after analysis; upload and analyze again")
        environment = validate_environment(environment, plan.required_env)
        original_event = self.event
        def masked_event(stage, message):
            for value in sorted(set(environment.values()), key=len, reverse=True):
                if value:
                    message = message.replace(value, "[REDACTED]")
            original_event(stage, message)
        self.event = masked_event
        name = f"sky-{job_id}"
        image = f"sky/{job_id}:latest"
        ImageBuilder(self.command, self.event).build(project, plan, image)
        created = False
        try:
            self.event("starting", "Starting container on a loopback-only random port")
            run_args = [
                "docker", "run", "-d", "--name", name, "--label", "app=sky",
                "--label", f"sky-attempt={job_id}",
                "--memory", "256m", "--cpus", "1", "--pids-limit", "128",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "-e", f"PORT={plan.port}", "-p", f"127.0.0.1::{plan.port}",
            ]
            if environment:
                # Private temporary file outside the build context; never store values in job state.
                with tempfile.NamedTemporaryFile(mode="w", prefix="sky-env-", encoding="utf-8") as env_file:
                    for key, value in environment.items():
                        env_file.write(f"{key}={value}\n")
                    env_file.flush()
                    self.command(run_args + ["--env-file", env_file.name, image])
            else:
                self.command(run_args + [image])
            created = True
            binding = self.command(["docker", "port", name, f"{plan.port}/tcp"])
            url = "http://" + binding.splitlines()[0]
            self.event("verifying", f"Checking HTTP response: {url}{plan.health_path}")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(30):
                try:
                    with opener.open(url + plan.health_path, timeout=1) as response:
                        if response.status == 200:
                            return {"url": url, "health_url": url + plan.health_path,
                                    "container": name, "image": image}
                except (OSError, urllib.error.URLError):
                    pass
                time.sleep(1)
            self.command(["docker", "logs", "--tail", "80", name])
            raise RuntimeError(f"App did not return HTTP 200 at {plan.health_path} within the readiness window")
        except Exception:
            if created:
                self.command(["docker", "rm", "-f", name], timeout=30)
            raise

    def cleanup_failure(self, attempt_id):
        for args in (["docker", "rm", "-f", f"sky-{attempt_id}"],
                     ["docker", "image", "rm", f"sky/{attempt_id}:latest"]):
            try:
                self.command(args, timeout=30)
            except Exception:
                self.event("cleanup", "리소스 정리 결과를 확인하지 못했습니다: " + args[-1])

    @staticmethod
    def inspect_resource(kind: str, name: str) -> dict | None:
        result = subprocess.run(["docker", kind, "inspect", name], capture_output=True, text=True, timeout=15)
        if result.returncode:
            if "No such object" in result.stderr or "No such image" in result.stderr or "No such container" in result.stderr:
                return None
            raise RuntimeError("Docker 리소스 상태를 확인하지 못했습니다: " + result.stderr.strip()[-300:])
        try:
            items = json.loads(result.stdout)
        except ValueError:
            raise RuntimeError("Docker 상태 응답이 올바르지 않습니다.") from None
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise RuntimeError("Docker 리소스 하나를 확인할 수 없습니다.")
        return items[0]

    def retire(self, result: dict, job_id: str) -> None:
        if not re.fullmatch(r"[a-f0-9]{16}", job_id):
            raise ValueError("Invalid local deployment job ID")
        names = {f"sky-{job_id}", *(f"sky-{job_id}-a{i}" for i in range(1, 4))}
        name = result.get("container")
        if name not in names:
            raise ValueError("Stored container identity is invalid")
        suffix = name.removeprefix("sky-")
        image = f"sky/{suffix}:latest"
        if result.get("image") != image:
            raise ValueError("Stored image identity is invalid")
        container = self.inspect_resource("container", name)
        if container is not None:
            labels = container.get("Config", {}).get("Labels") or {}
            if (container.get("Name") != "/" + name or labels.get("app") != "sky"
                    or labels.get("sky-attempt") not in {None, suffix}
                    or container.get("Config", {}).get("Image") != image):
                raise ValueError("Docker container ownership changed; refusing deletion")
            self.event("retiring", "관리 컨테이너를 종료합니다: " + name)
            self.command(["docker", "rm", "-f", name], timeout=30)
            if self.inspect_resource("container", name) is not None:
                raise RuntimeError("컨테이너 종료를 확인하지 못했습니다.")
        inspected_image = self.inspect_resource("image", image)
        if inspected_image is not None:
            if (image not in (inspected_image.get("RepoTags") or [])
                    or (inspected_image.get("Config", {}).get("Labels") or {}).get("app") != "sky"):
                raise ValueError("Docker image ownership changed; refusing deletion")
            self.event("retiring", "관리 이미지 태그를 삭제합니다: " + image)
            self.command(["docker", "image", "rm", image], timeout=30)
            if self.inspect_resource("image", image) is not None:
                raise RuntimeError("이미지 태그 삭제를 확인하지 못했습니다.")
