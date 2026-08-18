#!/usr/bin/env python3
"""The feature freeze, as a mechanism rather than an intention.

WHY
Ripple detects breaking changes across 10 contract types and 15 languages, and has
PROVEN zero fix-generating cells. Breadth is not the problem to solve; it is the
problem. 2,293 lines across 7 modules are unreachable from production entirely --
built, never wired, and still costing review attention and import surface.

For the next 30 days the only objective is one real breaking change becoming one
validated, mergeable PR. So these modules are frozen: they may SHRINK, they may be
documented, they may be deleted. They may not grow, and they may not be wired up.

A freeze that nobody enforces lasts about four days. This is the enforcement.

WHAT IS MEASURED, AND WHY NOT LINES
AST statement count, not line count. Adding a comment or extending the module
docstring to explain why something is frozen is legitimate and must not fail the
build; adding behaviour must. Line count cannot tell those apart, and a gate that
fires on documentation gets disabled.

Reachability is checked too: a frozen module gaining a production importer is
resurrection, which is the thing being prevented. Growing and being wired up are
both expansion of surface.

ASYMMETRY IS DELIBERATE
The baseline is a CEILING, not an equality. Shrinking is progress and passes.
Deleting a frozen module passes and prints a note to remove its entry. Failing the
build on progress is perverse -- unlike tools/audit_fail_silent.py, where a stale
entry could wave through a new defect, a stale entry here names a file that is gone
and can do no harm.

TO LIFT THE FREEZE
Delete the module's entry, or delete this file. It is meant to be temporary; if it
is still here in 2027 that is itself the finding.

Usage:
    python3.12 tools/audit_frozen_surface.py
"""
from __future__ import annotations

import ast
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# path -> (statement ceiling, why it is frozen)
FROZEN: dict = {
    "app/api_watcher.py": (
        124, "polling spec watcher, 249 lines, no importers -- the webhook path "
             "supersedes it and nothing has ever invoked it"),
    "app/dep_graph_viz.py": (
        138, "dependency graph visualisation, 501 lines, no importers -- the "
             "largest single block of unreachable code in the repo"),
    "app/monorepo.py": (
        124, "monorepo consumer discovery, 265 lines, no importers"),
    "app/multi_step_reasoning.py": (
        127, "reachable from tests only. One of the four 'AI features' announced "
             "on Aug 13; impact_prediction was deleted as unimportable and this "
             "one was never wired"),
    "app/natural_language.py": (
        134, "natural-language change description, 314 lines, no importers -- "
             "also an Aug 13 announced feature that never reached production"),
    "app/slack_notify.py": (
        100, "notifications, 394 lines, no importers. Nothing has ever been sent, "
             "which is why its two fail-silent sites never mattered"),
}


def _statements(path: str) -> int:
    with open(path) as fh:
        return sum(1 for n in ast.walk(ast.parse(fh.read()))
                   if isinstance(n, ast.stmt))


def _production_importers(module: str) -> list:
    """Files under app/ or agent/ that import this module, by ANY import form.

    Parsed, not grepped. A previous version of a sibling gate tested
    `"KNOWN_LANGUAGES" in src` and failed on a docstring that merely mentioned it.
    """
    found = []
    target = os.path.basename(module)[:-3]
    for path in sorted(glob.glob(os.path.join(ROOT, "app", "*.py"))
                       + glob.glob(os.path.join(ROOT, "agent", "*.py"))):
        if os.path.relpath(path, ROOT) == module:
            continue
        try:
            tree = ast.parse(open(path).read())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod.split(".")[-1] == target and (mod.startswith("app") or node.level):
                    found.append(os.path.relpath(path, ROOT)); break
                if mod in ("app", "") and any(a.name == target for a in node.names):
                    found.append(os.path.relpath(path, ROOT)); break
            elif isinstance(node, ast.Import):
                if any(a.name.split(".")[-1] == target and a.name.startswith("app.")
                       for a in node.names):
                    found.append(os.path.relpath(path, ROOT)); break
    return found


def main(argv: list) -> int:
    problems, notes = [], []
    frozen_stmts = 0

    print("=" * 74)
    print("FROZEN SURFACE")
    print("=" * 74)
    print("\n  modules frozen for the 30-day push to one validated autonomous PR:\n")

    for module, (ceiling, why) in sorted(FROZEN.items()):
        path = os.path.join(ROOT, module)
        if not os.path.exists(path):
            notes.append(f"{module} has been DELETED -- remove its entry from FROZEN")
            print(f"  gone   {module:<34} (was {ceiling} stmts)")
            continue
        try:
            actual = _statements(path)
        except SyntaxError as exc:
            problems.append(f"{module} does not parse ({exc})")
            continue
        frozen_stmts += actual

        importers = _production_importers(module)
        if actual > ceiling:
            problems.append(
                f"{module} GREW {ceiling} -> {actual} statements. It is frozen: "
                f"{why}")
            print(f"  GREW   {module:<34} {ceiling} -> {actual} stmts")
        elif importers:
            print(f"  WIRED  {module:<34} {actual} stmts")
        else:
            shrunk = f"  (was {ceiling}, shrunk)" if actual < ceiling else ""
            print(f"  ok     {module:<34} {actual} stmts{shrunk}")

        if importers:
            problems.append(
                f"{module} was WIRED UP -- imported by {', '.join(importers)}. "
                f"Resurrecting frozen debt is the thing this gate prevents.")

    for note in notes:
        print(f"\n  note: {note}")

    print(f"\n  {len(FROZEN)} module(s) frozen, {frozen_stmts} statements held")

    if problems:
        print(f"\n  {len(problems)} PROBLEM(S):")
        for p in problems:
            print(f"      {p}")
        print("\n  The freeze exists because breadth is not the gap -- proof is. If"
              "\n  this change genuinely belongs in the 30-day push, remove the"
              "\n  module's entry from FROZEN and say why in the commit message.")
        return 1

    print("\n  no frozen module grew or was wired up")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
