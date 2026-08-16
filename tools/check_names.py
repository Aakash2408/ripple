#!/usr/bin/env python3
"""Static undefined-global checker.

`ast.parse` only validates SYNTAX. It happily accepts `time.sleep(2)` when
`time` was imported as `import time as _time`, and the NameError only shows
up at runtime -- on Railway, mid-webhook, as a swallowed exception.

This catches that class of bug before deploy: any name loaded inside a
function that is neither a local, a parameter, a module-level binding, nor
a builtin is reported.

Usage:  python3 tools/check_names.py app/webhook.py [more.py ...]
Exit 1 if anything suspicious is found.
"""
from __future__ import annotations

import ast
import builtins
import sys


def module_level_bindings(tree: ast.Module) -> set:
    """Names bound at module scope: imports, assignments, defs, classes."""
    bound = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                # `import time as _time` binds _time, NOT time
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                bound.update(_target_names(target))
        elif isinstance(node, ast.AnnAssign):
            bound.update(_target_names(node.target))
        elif isinstance(node, (ast.If, ast.Try, ast.With)):
            # try/except ImportError fallbacks and __name__ guards
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    for alias in sub.names:
                        bound.add(alias.asname or alias.name.split(".")[0])
                elif isinstance(sub, ast.Assign):
                    for target in sub.targets:
                        bound.update(_target_names(target))
                elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    bound.add(sub.name)
    return bound


def _target_names(target: ast.AST) -> set:
    if isinstance(target, ast.Name):
        return {target.id}
    names = set()
    if isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            names |= _target_names(elt)
    return names


def local_bindings(fn: ast.AST) -> set:
    """Names bound inside a function: params, assignments, imports, comprehensions."""
    bound = set()
    args = fn.args
    # posonlyargs only exists on Python 3.8+
    for group in (getattr(args, "posonlyargs", []), args.args, args.kwonlyargs):
        for a in group:
            bound.add(a.arg)
    if args.vararg:
        bound.add(args.vararg.arg)
    if args.kwarg:
        bound.add(args.kwarg.arg)

    for node in ast.walk(fn):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                bound |= _target_names(t)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            bound |= _target_names(node.target)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            bound |= _target_names(node.target)
        elif isinstance(node, ast.comprehension):
            bound |= _target_names(node.target)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            bound |= _target_names(node.optional_vars)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Global):
            bound.update(node.names)
        elif isinstance(node, ast.Nonlocal):
            bound.update(node.names)
        elif isinstance(node, ast.Lambda):
            la = node.args
            for group in (getattr(la, "posonlyargs", []), la.args, la.kwonlyargs):
                for a in group:
                    bound.add(a.arg)
    return bound


def check(path: str) -> list:
    with open(path) as f:
        source = f.read()
    tree = ast.parse(source, filename=path)

    safe = module_level_bindings(tree) | set(dir(builtins)) | {
        "__name__", "__file__", "__doc__", "self", "cls",
    }

    # Map every function/lambda/class to its enclosing scopes so a closure
    # can see names bound by the function it is nested in. Without this a
    # helper like:
    #
    #     def outer():
    #         ordered = []
    #         def add(v):
    #             ordered.append(v)   # <- reported as undefined
    #
    # produced a false positive, and a checker that cries wolf gets ignored.
    enclosing = {}

    def walk_scopes(node, stack):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.Lambda, ast.ClassDef)):
                enclosing[child] = list(stack)
                walk_scopes(child, stack + [child])
            else:
                walk_scopes(child, stack)

    walk_scopes(tree, [])

    problems = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        allowed = safe | local_bindings(fn)
        # Names from every enclosing scope are visible to this closure
        for outer in enclosing.get(fn, []):
            if isinstance(outer, (ast.FunctionDef, ast.AsyncFunctionDef)):
                allowed |= local_bindings(outer)
            elif isinstance(outer, ast.Lambda):
                la = outer.args
                for group in (getattr(la, "posonlyargs", []), la.args, la.kwonlyargs):
                    for a in group:
                        allowed.add(a.arg)

        # Only inspect statements belonging to THIS function, not nested ones
        nested = {
            n for n in ast.walk(fn)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not fn
        }
        nested_nodes = set()
        for n in nested:
            nested_nodes.update(ast.walk(n))

        for node in ast.walk(fn):
            if node in nested_nodes:
                continue
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if node.id not in allowed:
                    problems.append((node.lineno, fn.name, node.id))
    # dedupe, keep first occurrence per (name, function)
    seen = set()
    unique = []
    for lineno, fname, name in sorted(problems):
        key = (fname, name)
        if key in seen:
            continue
        seen.add(key)
        unique.append((lineno, fname, name))
    return unique


def main(argv: list) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    total = 0
    for path in argv[1:]:
        problems = check(path)
        if problems:
            print(f"{path}: {len(problems)} possibly-undefined name(s)")
            for lineno, fname, name in problems:
                print(f"  line {lineno}: '{name}' in {fname}()")
            total += len(problems)
        else:
            print(f"{path}: OK -- no undefined names")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
