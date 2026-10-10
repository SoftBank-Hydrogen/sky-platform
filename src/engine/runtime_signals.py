"""Bounded source observations, never proof of duration, statelessness or host access."""

from __future__ import annotations

import ast
import re
from pathlib import PurePosixPath


def runtime_signals(path: str, content: str) -> set[str]:
    """Inspect code without importing or executing any uploaded module.

    Python uses AST nodes so comments and quoted examples cannot become runtime
    evidence. JS matches remain hypotheses; unsupported syntax stays unresolved.
    Host commands are recognized only as literal argv to subprocess APIs.
    """
    signals: set[str] = set()
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == ".py":
        try:
            tree = ast.parse(content)
        except (SyntaxError, ValueError, RecursionError):
            return {"runtime-syntax-unresolved"}
        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for item in node.names:
                    aliases[item.asname or item.name] = item.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                for item in node.names:
                    aliases[item.asname or item.name] = node.module + "." + item.name

        def qualified(node: ast.AST) -> str:
            if isinstance(node, ast.Name):
                return aliases.get(node.id, node.id)
            if isinstance(node, ast.Attribute):
                return qualified(node.value) + "." + node.attr
            return ""

        flask_apps = {
            name.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and qualified(node.value.func) == "flask.Flask"
            for name in node.targets
            if isinstance(name, ast.Name)
        }
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                arguments = [item.arg for item in (*node.args.posonlyargs, *node.args.args)]
                if node.name in {"handler", "lambda_handler"} and arguments[:2] == ["event", "context"]:
                    signals.add("function-handler")
            if not isinstance(node, ast.Call):
                continue
            name = qualified(node.func)
            if name in {"uvicorn.run", "aiohttp.web.run_app", "http.server.HTTPServer"} or (
                name in {app + ".run" for app in flask_apps}
            ):
                signals.add("persistent-http-server")
            if name in {"subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.Popen"}:
                argv = (
                    node.args[0]
                    if node.args
                    else next((item.value for item in node.keywords if item.arg == "args"), None)
                )
                if isinstance(argv, (ast.List, ast.Tuple)) and argv.elts:
                    command = argv.elts[0]
                    if (
                        isinstance(command, ast.Constant)
                        and isinstance(command.value, str)
                        and PurePosixPath(command.value).name in {"modprobe", "insmod", "rmmod"}
                    ):
                        signals.add("host-kernel-control")
            if name in {"open", "os.open"} and node.args:
                argument = node.args[0]
                if (
                    isinstance(argument, ast.Constant)
                    and isinstance(argument.value, str)
                    and (
                        argument.value == "/dev/kvm"
                        or argument.value.startswith(("/dev/nvidia", "/dev/dri/"))
                    )
                ):
                    signals.add("host-device-access")
    elif suffix in {".js", ".cjs", ".mjs", ".ts", ".tsx", ".jsx"}:
        # Mask ordinary quoted literals/comments; this is not a full JS parser.
        content = re.sub(
            r"//[^\n]*|/\*[\s\S]*?\*/|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`",
            lambda match: "".join("\n" if char == "\n" else " " for char in match.group()),
            content,
        )
        # Anchors exclude normal comment lines. These are still inferred signals,
        # not a JS parser or proof that a detected entrypoint is reachable.
        if re.search(
            r"(?m)^\s*(?:exports\.handler\s*=|module\.exports\.handler\s*=|"
            r"export\s+(?:async\s+)?function\s+handler\s*\(|export\s+const\s+handler\s*=)",
            content,
        ):
            signals.add("function-handler")
        if re.search(r"\b(?:createServer|Bun\.serve)\s*\(|\b\w+\.listen\s*\(", content):
            signals.add("persistent-http-server")
    return signals
