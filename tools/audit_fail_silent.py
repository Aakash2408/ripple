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
                    "exc": exc,
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
                        "exc": "",
                        "detail": "empty return inside try/except with no signal",
                    })
                    break

    # Stable, line-independent ordinal within (func, kind).
    #
    # WHY NOT THE LINE NUMBER: the first version of the triage keyed on
    # (file, line, func) and detached from the code the moment Stage 3 added 25
    # lines to webhook.py -- _retry_delay moved 1727 -> 1752 and silently lost its
    # LEGITIMATE classification. Worse than losing it: a NEW silent path landing on
    # line 1727 would have inherited that classification and been waved through.
    #
    # The ordinal moves only when the silent paths inside that one function change,
    # which is exactly when a human should re-triage.
    findings.sort(key=lambda f: f["line"])
    seen: dict = {}
    for f in findings:
        group = (f["func"], f["kind"])
        f["ordinal"] = seen.get(group, 0)
        seen[group] = f["ordinal"] + 1

    return findings


def site_key(filename: str, finding: dict) -> tuple:
    """The identity of a silent path, independent of where it sits in the file.

    `exc` is part of the key on purpose: widening `except ValueError` to
    `except Exception` is a different swallow with a different blast radius, so it
    must forfeit the old classification rather than inherit it.
    """
    return (filename, finding["func"], finding["kind"],
            finding["exc"], finding["ordinal"])


def _load_triage():
    sys.path.insert(0, HERE)
    from fail_silent_triage import TRIAGE, FIXED, REAL_BUG, MIN_REASON
    return TRIAGE, FIXED, REAL_BUG, MIN_REASON


def check(sites: dict) -> int:
    """Fail the build on any UNEXPLAINED silent path.

    Deliberately NOT "fail on any silent path": 25 sites are correct-but-invisible
    and making them visible is P0.4/P0.5 work. Gating on zero would either block
    the build for weeks or push people to delete the audit. Gating on
    *classification* is enforceable today and still catches the thing that hurt --
    a new swallow arriving with nobody having thought about it.
    """
    TRIAGE, FIXED, REAL_BUG, MIN_REASON = _load_triage()
    problems = []

    keys = set(sites)
    triaged = set(TRIAGE)

    # 1. a silent path nobody classified
    for key in sorted(keys - triaged):
        f, fn, kind, caught, ordinal = key
        problems.append(
            f"UNCLASSIFIED  {f}:{fn} line {sites[key]['line']}\n"
            f"                {kind} catching {caught or '-'} (#{ordinal})\n"
            f"                Add it to tools/fail_silent_triage.py with a reason.")

    # 2. a fix that came back
    for key in sorted(keys):
        fn_key = (key[0], key[1])
        if fn_key in FIXED:
            problems.append(
                f"REGRESSION    {key[0]}:{key[1]} line {sites[key]['line']}\n"
                f"                This was fixed: {FIXED[fn_key]}")

    # 3. a classification pointing at code that no longer exists
    for key in sorted(triaged - keys):
        f, fn, kind, caught, ordinal = key
        problems.append(
            f"STALE         {f}:{fn}  {kind} catching {caught or '-'} (#{ordinal})\n"
            f"                Classified but the audit no longer finds it. Either "
            f"the site was fixed (move it to FIXED) or the code changed shape "
            f"(re-triage it).")

    # 4. a real bug left standing
    for key, (bucket, _) in sorted(TRIAGE.items()):
        if bucket == REAL_BUG:
            problems.append(
                f"REAL_BUG      {key[0]}:{key[1]}  must be fixed, not annotated.")

    # 5. an annotation that does not explain anything, or a reference that rotted
    from fail_silent_triage import resolve_reason
    for key, (bucket, _) in sorted(TRIAGE.items()):
        try:
            reason = resolve_reason(key)
        except (ValueError, KeyError) as exc:
            problems.append(
                f"BAD REFERENCE {key[0]}:{key[1]}  {exc}")
            continue
        if len(reason.strip()) < MIN_REASON:
            problems.append(
                f"NO REASON     {key[0]}:{key[1]}  {bucket} with a "
                f"{len(reason.strip())}-char reason (minimum {MIN_REASON}).")

    if problems:
        print("\n" + "=" * 74)
        print(f"FAIL-SILENT GATE: {len(problems)} problem(s)")
        print("=" * 74 + "\n")
        for p in problems:
            print("  " + p + "\n")
        return 1

    print(f"\n  gate OK -- all {len(keys)} silent path(s) classified with a reason, "
          f"0 real bugs, {len(FIXED)} fixed function(s) still clean\n")
    return 0


def main(argv: list) -> int:
    verbose = "--verbose" in argv
    files = sorted(glob.glob(os.path.join(ROOT, "app", "*.py")))

    total = 0
    by_file = {}
    sites = {}
    for path in files:
        found = audit_file(path)
        name = os.path.basename(path)
        for f in found:
            sites[site_key(name, f)] = f
        if found:
            by_file[name] = found
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

    if "--check" in argv:
        return check(sites)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
