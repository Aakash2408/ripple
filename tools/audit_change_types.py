#!/usr/bin/env python3
"""Prove every change_type the engines emit is classified.

A change_type with no canonical operation reaches fix_templates as
"Unknown change_type", leaves the code unchanged, and therefore opens NO PR --
Ripple detects a breaking change and silently does nothing. This audit fails
if any engine can emit such a type.

Usage: python3.12 tools/audit_change_types.py
Exit 1 if any emitted type is unclassified.
"""
from __future__ import annotations

import ast
import collections
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from app.ast_compat import str_literal
from app.change_types import canonical_op, category, CANONICAL_OPS, CHANGE_TYPE_MAP


def emitted_types() -> tuple:
    """Scan every diff engine for the change_type strings it constructs.

    Returns ``(found, scanned)``. ``scanned`` is returned so the caller can tell
    "no engines on disk" apart from "engines on disk, extracted nothing" -- the
    second is a bug and must fail the audit.
    """
    found = collections.defaultdict(set)
    paths = sorted(glob.glob(os.path.join(ROOT, "app", "*diff*.py")))
    paths.append(os.path.join(ROOT, "app", "diff_engine.py"))
    scanned = []

    for path in paths:
        if not os.path.exists(path):
            continue
        name = os.path.basename(path)
        scanned.append(name)
        tree = ast.parse(open(path).read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # change_type="x" keyword
            for kw in node.keywords:
                if kw.arg == "change_type":
                    literal = str_literal(kw.value)
                    if literal is not None:
                        found[name].add(literal)
            # _bc("x", ...) positional helper
            if getattr(node.func, "id", "") == "_bc" and node.args:
                literal = str_literal(node.args[0])
                if literal is not None:
                    found[name].add(literal)
    return found, sorted(set(scanned))


def main() -> int:
    found, paths_scanned = emitted_types()
    all_types = set()
    for types in found.values():
        all_types |= types

    print("=" * 74)
    print("CHANGE TYPE COVERAGE AUDIT")
    print("=" * 74)

    unclassified = []
    by_category = collections.defaultdict(list)
    for ct in sorted(all_types):
        op = canonical_op(ct)
        cat = category(ct)
        if not op:
            unclassified.append(ct)
        else:
            by_category[cat].append((ct, op))

    print(f"\n  {len(all_types)} change types emitted by {len(found)} engines")
    print(f"  {len(CANONICAL_OPS)} canonical operations\n")

    # An extractor that can return empty MUST be asserted non-empty, or every
    # check below it becomes a vacuous truth. This audit passed for an unknown
    # length of time on Python 3.7 having found zero types, because "every
    # emitted type maps to a canonical op" is trivially true of no types. The
    # mechanism was ast.Str vs ast.Constant (see app/ast_compat.py); this guard
    # is here so the next mechanism -- whatever it is -- fails loudly instead.
    if not all_types:
        print("  ✗ extracted ZERO change types from "
              f"{len(paths_scanned)} engine file(s).")
        print("    The engines demonstrably do emit some (app/diff_engine.py "
              "constructs change_type=\"removed_field\"), so this is an "
              "extractor fault, not an empty codebase.")
        print("    Refusing to report a pass over an empty set.")
        return 1

    for cat in ("mechanical", "judgment", "wire_only"):
        rows = by_category.get(cat, [])
        print(f"  {cat.upper()} ({len(rows)})")
        for ct, op in rows:
            explicit = "" if ct in CHANGE_TYPE_MAP else "  [via suffix fallback]"
            print(f"      {ct:36} -> {op}{explicit}")
        print()

    if unclassified:
        print("  UNCLASSIFIED -- these reach fix_templates as 'Unknown change_type',")
        print("  produce no code change, and therefore open NO PR:")
        for ct in unclassified:
            print(f"      {ct}")
        return 1

    print("  ✅ every emitted change type maps to a canonical operation")

    # Event-layer types are emitted where this audit does not scan: a diff
    # engine compares two versions of ONE file and cannot observe a deletion.
    # Verify each registered emitter really does emit it, so the registry cannot
    # become a way to silence the audit with a claim.
    from app.change_types import EVENT_LAYER_TYPES
    event_types = set()
    for ct, where in EVENT_LAYER_TYPES.items():
        srcfile = where.split("::")[0]
        try:
            body = open(os.path.join(ROOT, srcfile)).read()
        except OSError:
            print(f"  ✗ {ct}: registered emitter {srcfile} does not exist")
            return 1
        if f'"{ct}"' not in body and f"'{ct}'" not in body:
            print(f"  ✗ {ct}: registered as emitted by {where}, but that file "
                  f"never mentions it")
            return 1
        event_types.add(ct)
    if event_types:
        print(f"\n  {len(event_types)} change type(s) emitted by the event layer "
              f"(not by a diff engine), each verified present in its emitter:")
        for ct in sorted(event_types):
            print(f"      {ct:18} <- {EVENT_LAYER_TYPES[ct]}")

    # Report ops with no emitter, so the taxonomy does not drift into fiction
    used_ops = {op for _, op in
                [(c, canonical_op(c)) for c in all_types | event_types]}
    unused = sorted(set(CANONICAL_OPS) - used_ops)
    if unused:
        print(f"\n  note: {len(unused)} canonical op(s) currently emitted by no engine: "
              f"{', '.join(unused)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
