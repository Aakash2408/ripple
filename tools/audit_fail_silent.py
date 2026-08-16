#!/usr/bin/env python3
"""Find failure paths that produce NO diagnostic signal.

THE BUG CLASS
-------------
The problem is not that a function returns "" or [] on failure. It is that
it does so WITHOUT SAYING WHY, which makes a fault byte-identical to a
healthy no-op:

    files_found: 0     <- no consumers? bad token? empty search index?
                          NameError mid-loop? all four looked the same.

That ambiguity is what turned a 13-bug night into 13 deploy cycles, and it
is why `event`/`after_sha` sat broken inside `except Exception: pass` for
weeks without anyone noticing.

WHAT COUNTS AS SILENT
  * an `except` handler whose body is only pass/continue/return, with no
    logging call anywhere inside it
  * a bare `return ""` / `return []` / `return {}` / `return None` inside a
    try/except or after a failure check, with no logging in the same block

WHAT IS LEGITIMATELY SILENT (allow-listed by intent, see ALLOW_SILENT)
  * telemetry that must never break the thing it observes
  * optional-import fallbacks that have a working alternative path

Usage:
    python3.12 tools/audit_fail_silent.py            # summary
    python3.12 tools/audit_fail_silent.py --verbose  # every site
"""
from __future__ import annotations

import ast
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Functions whose failures are genuinely safe to swallow, with the reason.
ALLOW_SILENT = {
    "_log_activity": "telemetry must not break the pipeline it observes",
    "record": "telemetry write",
    "_persist": "telemetry write",
    "_load": "corrupt telemetry must not take down the service",
    "_store_dir": "probing candidate directories is expected to fail",
}

LOG_MARKERS = (
    "_log_activity", "activity.record", "logger", "logging",
    "print", "warnings.warn", "_log", "raise",
)


def _has_log(node: ast.AST) -> bool:
    """Does this subtree emit any diagnostic signal?"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            src = ast.dump(sub.func)
            if any(m in src for m in LOG_MARKERS):
                return True
        if isinstance(sub, (ast.Raise, ast.Assert)):
            return True
    return False


def _is_empty_return(node: ast.AST) -> bool:
    if not isinstance(node, ast.Return):
        return False
    v = node.value
    if v is None:
        return True
    if isinstance(v, ast.Constant) and v.value in ("", None, 0, False):
        return True
    if isinstance(v, (ast.List, ast.Dict, ast.Tuple, ast.Set)) and not getattr(v, "elts", getattr(v, "keys", [])):
        return True
    return False


def audit_file(path: str) -> list:
    tree = ast.parse(open(path).read())
    findings = []

    # map nodes -> enclosing function name
    owner = {}

    def walk(node, fn):
        for child in ast.iter_child_nodes(node):
            nxt = child.name if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
            owner[id(child)] = nxt
            walk(child, nxt)

    walk(tree, "<module>")

    for node in ast.walk(tree):
        fn = owner.get(id(node), "<module>")
        if fn in ALLOW_SILENT:
            continue

        # 1. swallowed except handlers
        if isinstance(node, ast.ExceptHandler):
            body_is_trivial = all(
                isinstance(s, (ast.Pass, ast.Continue, ast.Break))
                or _is_empty_return(s)
                for s in node.body
            )
            if body_is_trivial and not _has_log(node):
                exc = "Exception"
                if node.type is not None:
                    try:
                        exc = ast.unparse(node.type)
                    except Exception:
                        exc = "?"
                findings.append({
                    "kind": "swallowed_except",
                    "line": node.lineno,
                    "func": fn,
                    "detail": f"except {exc}: {'/'.join(type(s).__name__ for s in node.body)}",
                })

        # 2. empty return inside a try, with no logging in that try
        if isinstance(node, ast.Try):
            for sub in ast.walk(node):
                if _is_empty_return(sub) and not _has_log(node):
                    findings.append({
                        "kind": "silent_empty_return",
                        "line": sub.lineno,
                        "func": fn,
                        "detail": "empty return inside try/except with no signal",
                    })
                    break

    return findings


def main(argv: list) -> int:
    verbose = "--verbose" in argv
    files = sorted(glob.glob(os.path.join(ROOT, "app", "*.py")))

    total = 0
    by_file = {}
    for path in files:
        found = audit_file(path)
        if found:
            by_file[os.path.basename(path)] = found
            total += len(found)

    print("=" * 74)
    print("FAIL-SILENT AUDIT")
    print("=" * 74)
    print(f"\n  {total} silent failure path(s) across {len(by_file)} file(s)\n")

    for name in sorted(by_file, key=lambda k: -len(by_file[k])):
        found = by_file[name]
        swallowed = sum(1 for f in found if f["kind"] == "swallowed_except")
        returns = len(found) - swallowed
        print(f"  {name:28} {len(found):3}  "
              f"({swallowed} swallowed except, {returns} silent return)")
        if verbose:
            for f in found:
                print(f"      line {f['line']:5}  {f['func']:34} {f['detail']}")

    print(f"\n  allow-listed as legitimately silent: {', '.join(sorted(ALLOW_SILENT))}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
