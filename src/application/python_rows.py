"""Reject positional access to statically known psycopg dict rows."""
from __future__ import annotations

import ast
from pathlib import Path

from application.python_transactions import _modules


class _Rows:
    def __init__(self, modules, factories):
        self.modules, self.factories = modules, factories
        self.discovered, self.errors = {}, []

    @staticmethod
    def shape(value, kind):
        return isinstance(value, tuple) and len(value) == 2 and value[0] == kind

    def style(self, call, env, default):
        for kw in call.keywords:
            value = self.value(kw.value, env)
            if kw.arg is None and isinstance(value, dict) and 'row_factory' in value:
                value = value['row_factory']
            elif kw.arg != 'row_factory':
                continue
            return {'psycopg.rows.dict_row': 'dict', 'psycopg.rows.tuple_row': 'tuple'}.get(value, 'unknown')
        return default

    def value(self, node, env):
        if isinstance(node, ast.Name):
            return env.get(node.id)
        if isinstance(node, ast.Attribute):
            base = self.value(node.value, env)
            return base + '.' + node.attr if isinstance(base, str) else None
        if isinstance(node, ast.Dict):
            return {k.value: self.value(v, env) for k, v in zip(node.keys, node.values)
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if isinstance(node, ast.Await):
            return self.value(node.value, env)
        if isinstance(node, ast.Call):
            fn = self.value(node.func, env)
            if fn in ('psycopg.connect', 'psycopg.Connection.connect'):
                return ('connection', self.style(node, env, 'tuple'))
            if isinstance(fn, str) and fn in self.factories:
                return self.factories[fn]
            if fn == 'fastapi.Depends' and node.args:
                return self.factories.get(self.value(node.args[0], env))
            if isinstance(node.func, ast.Attribute):
                owner, method = self.value(node.func.value, env), node.func.attr
                if self.shape(owner, 'connection') or self.shape(owner, 'cursor'):
                    if method == 'cursor':
                        return ('cursor', self.style(node, env, owner[1]))
                    if method == 'execute':
                        return ('cursor', owner[1])
                    if method == 'fetchone':
                        return ('row', owner[1])
                    if method in ('fetchall', 'fetchmany'):
                        return ('rows', owner[1])
        if isinstance(node, ast.Subscript) and self.shape(self.value(node.value, env), 'rows'):
            return ('rows' if isinstance(node.slice, ast.Slice) else 'row', self.value(node.value, env)[1])
        return None

    def inspect(self, node, env, relative):
        if node is None:
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            local = dict(env)
            for generator in node.generators:
                self.inspect(generator.iter, local, relative)
                source = self.value(generator.iter, local)
                if isinstance(generator.target, ast.Name):
                    local[generator.target.id] = ('row', source[1]) if (self.shape(source, 'rows') or self.shape(source, 'cursor')) else None
                for condition in generator.ifs:
                    self.inspect(condition, local, relative)
            for expression in [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]:
                self.inspect(expression, local, relative)
            return
        if isinstance(node, ast.Subscript) and self.value(node.value, env) == ('row', 'dict'):
            index = node.slice
            positional = (isinstance(index, ast.Constant) and type(index.value) is int
                          or isinstance(index, ast.UnaryOp) and isinstance(index.operand, ast.Constant)
                          and type(index.operand.value) is int or isinstance(index, ast.Slice))
            if positional:
                self.errors.append((relative.as_posix(), node.lineno, ast.unparse(node)))
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, ast.stmt):
                self.inspect(child, env, relative)

    def block(self, body, env, module, relative):
        returned = []
        for node in body:
            self.inspect(node, env, relative)
            if isinstance(node, ast.Import):
                for a in node.names:
                    env[a.asname or a.name.split('.')[0]] = a.name if a.asname else a.name.split('.')[0]
            elif isinstance(node, ast.ImportFrom):
                package = module.split('.') if relative.name == '__init__.py' else module.split('.')[:-1]
                base = '.'.join(package[:len(package) - node.level + 1]) if node.level else ''
                source = '.'.join(p for p in (base, node.module) if p)
                for a in node.names:
                    env[a.asname or a.name] = source + '.' + a.name
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                local = dict(env)
                args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
                defaults = ([None] * (len(node.args.posonlyargs) + len(node.args.args) - len(node.args.defaults))
                            + list(node.args.defaults) + list(node.args.kw_defaults))
                for arg, default in zip(args, defaults):
                    local[arg.arg] = self.value(default, env)
                    if local[arg.arg] is None and arg.annotation is not None:
                        for expression in ast.walk(arg.annotation):
                            if isinstance(expression, ast.Call) and self.value(expression.func, env) == 'fastapi.Depends':
                                local[arg.arg] = self.value(expression, env)
                values = self.block(node.body, local, module, relative)
                if values and all(v == values[0] for v in values) and values[0] is not None:
                    self.discovered[module + '.' + node.name] = values[0]
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = self.value(node.value, env)
                for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                    if isinstance(target, ast.Name):
                        env[target.id] = value
                    elif isinstance(target, ast.Attribute) and target.attr == 'row_factory' and isinstance(target.value, ast.Name):
                        name = target.value.id
                        prior = env.get(name)
                        if self.shape(prior, 'connection') or self.shape(prior, 'cursor'):
                            env[name] = (prior[0], {'psycopg.rows.dict_row': 'dict',
                                                   'psycopg.rows.tuple_row': 'tuple'}.get(value, 'unknown'))
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if isinstance(item.optional_vars, ast.Name):
                        env[item.optional_vars.id] = self.value(item.context_expr, env)
                returned.extend(self.block(node.body, env, module, relative))
            elif isinstance(node, ast.Return):
                returned.append(self.value(node.value, env))
            elif isinstance(node, ast.Expr) and isinstance(node.value, (ast.Yield, ast.YieldFrom)):
                returned.append(self.value(node.value.value, env))
            elif isinstance(node, ast.If):
                branches = []
                for statements in (node.body, node.orelse):
                    local = dict(env)
                    returned.extend(self.block(statements, local, module, relative))
                    branches.append(local)
                for name in branches[0].keys() | branches[1].keys():
                    env[name] = branches[0].get(name) if branches[0].get(name) == branches[1].get(name) else None
            elif isinstance(node, (ast.Try, ast.TryStar)):
                returned.extend(self.block(node.body, env, module, relative))
                for handler in node.handlers:
                    returned.extend(self.block(handler.body, dict(env), module, relative))
                returned.extend(self.block(node.orelse + node.finalbody, env, module, relative))
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                local = dict(env)
                if isinstance(node, (ast.For, ast.AsyncFor)) and isinstance(node.target, ast.Name):
                    source = self.value(node.iter, env)
                    local[node.target.id] = ('row', source[1]) if (self.shape(source, 'rows') or self.shape(source, 'cursor')) else None
                returned.extend(self.block(node.body + node.orelse, local, module, relative))
        return returned

    def run(self):
        for module, (relative, tree) in self.modules.items():
            env = {n.name: module + '.' + n.name for n in tree.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            self.block([n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))], env, module, relative)
            self.block(tree.body, env, module, relative)
        return self


def check_python_dict_row_access(work: Path) -> None:
    """Bounded dataflow check; custom row factories and unknown bindings are left alone."""
    modules, factories = _modules(work), {}
    limit = 2 + sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    for _, tree in modules.values() for n in ast.walk(tree))
    for _ in range(limit):
        analysis = _Rows(modules, factories).run()
        if analysis.discovered == factories:
            break
        factories = analysis.discovered
    if analysis.errors:
        details = '; '.join(f'{p}:{line}: {code}' for p, line, code in sorted(set(analysis.errors)))
        raise ValueError(
            'CV-04: psycopg dict_row does not support positional row access: ' + details + '. '
            'SQLite Row supports both row[0] and row["name"], but dict_row only supports column names. '
            'Use explicit SQL aliases and named keys, e.g. SELECT COUNT(*) AS total then '
            'fetchone()["total"], or INSERT ... RETURNING id then fetchone()["id"]. '
            'Update every consumer of fetched rows, including assigned rows and loops. '
            'Do not switch the whole connection to tuple_row while named-key consumers remain.')
