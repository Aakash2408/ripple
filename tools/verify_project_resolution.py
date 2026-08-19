#!/usr/bin/env python3
"""Build the monorepo shapes that break naive resolution, and check each answer.

WHY THESE SHAPES
"Nearest manifest wins" is most of the answer and is wrong in three specific ways
that all occur in real repositories:

    hoisted workspace     the package holds tsconfig.json; node_modules and
                          package.json live at the workspace root. app/validation.py
                          requires both in ONE directory, so this is the case that
                          silently becomes UNABLE_TO_VALIDATE if nobody reports it.
    polyglot repo         a .ts file under a go.mod. Resolving by nearest manifest
                          alone hands a TypeScript file to a Go project.
    no owning project      a loose file with no config above it. Falling back to the
                          repo root is the tempting answer and the wrong one: the
                          root tsconfig either excludes the file (a broken fix
                          validates clean) or includes thousands of unrelated ones.

Each case asserts the ROOT chosen, not merely that something was returned.

Usage:
    python tools/verify_project_resolution.py
"""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from app.project_resolution import group, resolve  # noqa: E402


def _write(tree: str, rel: str, body: str = "{}") -> None:
    path = os.path.join(tree, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(body)


def _build(tree: str) -> None:
    # 1. Flat repo: everything at the root.
    _write(tree, "flat/package.json")
    _write(tree, "flat/tsconfig.json")
    _write(tree, "flat/src/a.ts", "export const a = 1;\n")

    # 2. Monorepo, package self-contained.
    _write(tree, "mono/package.json", '{"workspaces":["packages/*"]}')
    _write(tree, "mono/tsconfig.base.json")
    _write(tree, "mono/packages/api/package.json")
    _write(tree, "mono/packages/api/tsconfig.json")
    _write(tree, "mono/packages/api/src/user.ts", "export const u = 1;\n")
    # A sibling that must NOT be chosen for api's file.
    _write(tree, "mono/packages/web/package.json")
    _write(tree, "mono/packages/web/tsconfig.json")
    _write(tree, "mono/packages/web/src/page.tsx", "export const p = 1;\n")

    # 3. HOISTED: package has tsconfig only; package.json is at the workspace root.
    _write(tree, "hoist/package.json", '{"workspaces":["packages/*"]}')
    _write(tree, "hoist/packages/api/tsconfig.json")
    _write(tree, "hoist/packages/api/src/user.ts", "export const u = 1;\n")

    # 3b. A plain directory of packages -- NO `workspaces` key. Dependencies really
    #     do live in the package, so this must not read as hoisted.
    _write(tree, "plain/packages/api/package.json")
    _write(tree, "plain/packages/api/tsconfig.json")
    _write(tree, "plain/packages/api/src/user.ts", "export const u = 1;\n")

    # 4. POLYGLOT: a .ts file living under a Go module.
    _write(tree, "poly/go.mod", "module example.com/x\n")
    _write(tree, "poly/main.go", "package main\n")
    _write(tree, "poly/scripts/tool.ts", "export const t = 1;\n")

    # 5. NO PROJECT: a loose file with no config above it.
    _write(tree, "loose/src/orphan.ts", "export const o = 1;\n")

    # 6. NESTED: a project inside a project -- the inner one owns the file.
    _write(tree, "nested/package.json")
    _write(tree, "nested/tsconfig.json")
    _write(tree, "nested/inner/package.json")
    _write(tree, "nested/inner/tsconfig.json")
    _write(tree, "nested/inner/src/deep.ts", "export const d = 1;\n")

    # 7. Python and Go, to prove the table is not TypeScript-only.
    _write(tree, "py/pyproject.toml", "[project]\nname='x'\n")
    _write(tree, "py/pkg/mod.py", "x = 1\n")
    _write(tree, "gomod/go.mod", "module example.com/y\n")
    _write(tree, "gomod/cmd/main.go", "package main\n")


CASES = [
    ("flat repo", "flat/src/a.ts", "flat", False),
    # `mono/package.json` declares `workspaces`, so dependencies resolve from the
    # WORKSPACE ROOT even though the package has its own package.json. These were
    # labelled self-contained before workspace detection existed -- that label was an
    # artifact of treating the nearest package.json as the install root, which made
    # npm install succeed with nothing to install and then left tsc missing.
    ("monorepo package", "mono/packages/api/src/user.ts", "mono/packages/api", True),
    ("monorepo sibling", "mono/packages/web/src/page.tsx", "mono/packages/web", True),
    ("hoisted workspace", "hoist/packages/api/src/user.ts", "hoist/packages/api", True),
    # A plain DIRECTORY of packages -- no `workspaces` key anywhere. Dependencies
    # really do live in the package, so this must NOT be reported as hoisted. It
    # exercises the fallback branch that the workspace cases no longer reach.
    ("packages dir, no workspaces", "plain/packages/api/src/user.ts",
     "plain/packages/api", False),
    ("nested project", "nested/inner/src/deep.ts", "nested/inner", False),
    ("python project", "py/pkg/mod.py", "py", False),
    ("go module", "gomod/cmd/main.go", "gomod", False),
    ("polyglot .ts under go.mod", "poly/scripts/tool.ts", None, False),
    ("no owning project", "loose/src/orphan.ts", None, False),
]


def main(argv: list) -> int:
    print("=" * 78)
    print("PROJECT RESOLUTION -- the shapes that break 'nearest manifest wins'")
    print("=" * 78)

    failures = []
    with tempfile.TemporaryDirectory(prefix="ripple-mono-") as tree:
        _build(tree)

        for label, rel, expect_root, expect_hoist in CASES:
            project = resolve(tree, rel)
            got = None if project is None else (project.rel_root or ".")
            want = expect_root if expect_root is not None else None
            if want == "":
                want = "."
            ok = got == want

            hoisted = bool(project) and bool(project.deps_root) and \
                os.path.normpath(project.deps_root) != os.path.normpath(project.root)
            if ok and hoisted != expect_hoist:
                ok = False

            print(f"\n  {'ok ' if ok else 'BAD'} {label:<28} -> "
                  f"{got if got is not None else 'None (REVIEW)'}")
            if project:
                print(f"      {project.reason[:104]}")
            if not ok:
                failures.append(
                    f"{label}: root {got!r} (wanted {want!r}), "
                    f"hoisted={hoisted} (wanted {expect_hoist})")

        # Grouping: one change touching two packages must yield TWO projects, each
        # validated in its own root. Collapsing them is the bug this prevents.
        grouped, unresolved = group(tree, [
            "mono/packages/api/src/user.ts",
            "mono/packages/web/src/page.tsx",
            "loose/src/orphan.ts",
        ])
        print("\n" + "-" * 78)
        print(f"  grouping: {len(grouped)} project(s), "
              f"{len(unresolved)} unresolved")
        for root, (project, files) in sorted(grouped.items()):
            print(f"      {project.rel_root or '.':<24} {len(files)} file(s)")
        if len(grouped) != 2:
            failures.append(f"grouping collapsed two packages into {len(grouped)}")
        if unresolved != ["loose/src/orphan.ts"]:
            failures.append(f"unresolved was {unresolved}")

    print()
    if failures:
        print(f"  {len(failures)} FAILURE(S):")
        for msg in failures:
            print(f"      {msg}")
        return 1
    print("  every shape resolved to the right project, and the unowned file to none.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
