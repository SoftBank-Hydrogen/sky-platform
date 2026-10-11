"""Bounded Python binding analysis for SQLite connection-context conversions.

This proves common imports, connection factories, annotations and FastAPI dependencies,
not arbitrary Python execution or dynamically constructed dependency injection.
"""
from __future__ import annotations

import ast
from pathlib import Path

CONNECTION = '<database connection>'
CONNECTORS = {'sqlite3.connect', 'psycopg.connect', 'psycopg.Connection.connect'}
CONNECTION_TYPES = {'sqlite3.Connection', 'psycopg.Connection', 'psycopg.connection.Connection'}
EXCLUDED = {'node_modules', '.venv', 'venv', 'vendor', '__pycache__', 'tests'}


def _modules(root: Path):
    result = {}
    for path in sorted(root.rglob('*.py')):
        relative = path.relative_to(root)
        if path.is_symlink() or any(part in EXCLUDED for part in relative.parts):
            continue
        name = '.'.join(relative.with_suffix('').parts)
        if name.endswith('.__init__'):
            name = name[:-9]
        try:
            tree = ast.parse(path.read_text(encoding='utf-8-sig'), filename=relative.as_posix())
        except (SyntaxError, UnicodeError):
            # Syntax/build validation remains responsible for files that cannot be parsed.
            continue
        result[name] = (relative, tree)
    return result


class _Bindings:
    def __init__(self, modules, factories=(), original_arguments=None):
        self.modules = modules
        self.factories = set(factories)
        self.arguments = {}
        self.original_arguments = original_arguments or {}
        self.discovered = set()
        self.errors = []
        self.uses_psycopg = False
        self.propagated = {}
        self.signatures = {}
        for module, (_, tree) in modules.items():
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.signatures[module + '.' + node.name] = [a.arg for a in
                        [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]]

    def calls(self, node, env):
        """Propagate proven connection arguments to simple local/imported helpers."""
        if node is None or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        if isinstance(node, ast.Call):
            name = self.expression(node.func, env)
            signature = self.signatures.get(name, [])
            known = self.propagated.setdefault(name, set()) if signature else set()
            for parameter, arg in zip(signature, node.args):
                if self.expression(arg, env) == CONNECTION:
                    known.add(parameter)
            for kw in node.keywords:
                if kw.arg in signature and self.expression(kw.value, env) == CONNECTION:
                    known.add(kw.arg)
        for child in ast.iter_child_nodes(node):
            # Statement bodies are visited with their own scope/bindings in block().
            if not isinstance(child, ast.stmt):
                self.calls(child, env)

    def expression(self, node, env):
        if isinstance(node, ast.Name):
            return env.get(node.id)
        if isinstance(node, ast.Attribute):
            base = self.expression(node.value, env)
            return base + '.' + node.attr if base else None
        if isinstance(node, ast.Await):
            return self.expression(node.value, env)
        if isinstance(node, ast.Call):
            function = self.expression(node.func, env)
            if function in CONNECTORS or function in self.factories:
                return CONNECTION
            if function == 'fastapi.Depends' and node.args:
                dependency = self.expression(node.args[0], env)
                if dependency in self.factories or dependency == CONNECTION:
                    return CONNECTION
        return None

    def annotated_connection(self, node, env):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                node = ast.parse(node.value, mode='eval').body
            except SyntaxError:
                return False
        if node is None:
            return False
        return (self.expression(node, env) in CONNECTION_TYPES
                or self.expression(node, env) == CONNECTION
                or any(self.annotated_connection(child, env) for child in ast.iter_child_nodes(node)))

    @staticmethod
    def bind(target, value, env):
        if isinstance(target, ast.Name):
            env[target.id] = value
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                _Bindings.bind(element, None, env)

    def block(self, statements, env, module, scope, relative):
        returns_connection = False
        for node in statements:
            self.calls(node, env)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    env[alias.asname or alias.name.split('.')[0]] = alias.name if alias.asname else alias.name.split('.')[0]
                    self.uses_psycopg |= alias.name.split('.')[0] == 'psycopg'
            elif isinstance(node, ast.ImportFrom):
                package = module.split('.') if relative.name == '__init__.py' else module.split('.')[:-1]
                base = '.'.join(package[:len(package) - node.level + 1]) if node.level else ''
                source = '.'.join(p for p in (base, node.module) if p)
                self.uses_psycopg |= source.split('.')[0] == 'psycopg'
                for alias in node.names:
                    env[alias.asname or alias.name] = source + '.' + alias.name
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = scope + '.' + node.name
                env[node.name] = qualified
                local = dict(env)
                arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
                defaults = ([None] * (len(node.args.posonlyargs) + len(node.args.args) - len(node.args.defaults))
                            + list(node.args.defaults) + list(node.args.kw_defaults))
                known = set()
                for arg, default in zip(arguments, defaults):
                    local[arg.arg] = None  # Parameters shadow module and enclosing bindings.
                    if (self.annotated_connection(arg.annotation, env)
                            or self.expression(default, env) == CONNECTION
                            or arg.arg in self.original_arguments.get(qualified, ())):
                        local[arg.arg] = CONNECTION
                        known.add(arg.arg)
                self.arguments[qualified] = known
                if self.block(node.body, local, module, qualified, relative) or self.annotated_connection(node.returns, env):
                    self.discovered.add(qualified)
            elif isinstance(node, ast.ClassDef):
                self.block(node.body, dict(env), module, scope + '.' + node.name, relative)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = self.expression(node.value, env)
                if isinstance(node, ast.AnnAssign) and self.annotated_connection(node.annotation, env):
                    value = CONNECTION
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    self.bind(target, value, env)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    binding = self.expression(item.context_expr, env)
                    # A direct `with psycopg.connect(...) as conn` defines an intentional
                    # connection lifetime. Reject reuse of an already-bound connection only.
                    if binding == CONNECTION and isinstance(item.context_expr, (ast.Name, ast.Attribute)):
                        self.errors.append((relative.as_posix(), node.lineno, ast.unparse(item.context_expr)))
                    if item.optional_vars:
                        self.bind(item.optional_vars, binding, env)
                returns_connection |= self.block(node.body, env, module, scope, relative)
            elif isinstance(node, ast.Return):
                returns_connection |= self.expression(node.value, env) == CONNECTION
            elif isinstance(node, ast.Expr) and isinstance(node.value, (ast.Yield, ast.YieldFrom)):
                returns_connection |= self.expression(node.value.value, env) == CONNECTION
            elif isinstance(node, ast.If):
                branches = []
                for body in (node.body, node.orelse):
                    branch = dict(env)
                    returns_connection |= self.block(body, branch, module, scope, relative)
                    branches.append(branch)
                for key in branches[0].keys() | branches[1].keys():
                    a, b = branches[0].get(key), branches[1].get(key)
                    env[key] = a if a == b else None
            elif isinstance(node, (ast.Try, ast.TryStar)):
                returns_connection |= self.block(node.body, env, module, scope, relative)
                for handler in node.handlers:
                    returns_connection |= self.block(handler.body, dict(env), module, scope, relative)
                returns_connection |= self.block(node.orelse + node.finalbody, env, module, scope, relative)
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                local = dict(env)
                if isinstance(node, (ast.For, ast.AsyncFor)):
                    self.bind(node.target, None, local)
                returns_connection |= self.block(node.body + node.orelse, local, module, scope, relative)
        return returns_connection

    def run(self):
        for module, (relative, tree) in self.modules.items():
            # Resolve imports and local factories even when declared after a route definition.
            env = {n.name: module + '.' + n.name for n in tree.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            self.block([n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))],
                       env, module, module, relative)
            self.block(tree.body, env, module, module, relative)
        return self


def _analyse(modules, original_arguments=None):
    factories = set()
    arguments = {name: set(values) for name, values in (original_arguments or {}).items()}
    # Fixed point over statically known return/yield factories, including relative imports.
    limit = 1 + sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    for _, tree in modules.values() for n in ast.walk(tree))
    for _ in range(limit + 1):
        result = _Bindings(modules, factories, arguments).run()
        added = result.discovered - factories
        changed = False
        for name, values in result.propagated.items():
            known = arguments.setdefault(name, set())
            changed |= bool(values - known)
            known.update(values)
        if not added and not changed:
            return result
        factories.update(added)
    return result


def check_python_connection_transactions(original: Path, work: Path) -> None:
    """Called only for approved SQLite conversions; never changes application files."""
    before = _analyse(_modules(original))
    after = _analyse(_modules(work), before.arguments)
    if not after.uses_psycopg or not after.errors:
        return
    details = '; '.join(f'{path}:{line}: with {name}:' for path, line, name in sorted(set(after.errors)))
    raise ValueError(
        'CV-04: SQLite→psycopg connection context changes transaction semantics: ' + details + '. '
        'sqlite3 keeps the connection open, but psycopg closes it on context exit. '
        'Use with conn.transaction(): (replace conn with the actual connection variable), '
        'or explicit conn.commit()/conn.rollback(); close the connection only at request/lifetime end. '
        'Preserve commit boundaries: if an earlier SELECT already began a transaction, transaction() '
        'is only a savepoint. Use autocommit=True with explicit transaction() blocks, or explicitly '
        'commit/rollback the outer transaction. Verify signup and create-then-read routes, not only health.')
