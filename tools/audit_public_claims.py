#!/usr/bin/env python3
"""Compare what Ripple CLAIMS in public against what the registry DERIVES.

WHY THIS IS A CI GATE AND NOT A DOC REVIEW
------------------------------------------
Two surfaces drifted from the code in ways nobody noticed until they were
measured:

  landing/index.html   said "3 platforms" and "82 tests" while claiming
                       "Done in 15 seconds" with no partial-fix caveat. It was
                       edited the day before it was found -- unread but still
                       maintained.
  changelog.html       four days stale, missing every correctness fix.

A number in prose has no owner and no test, so it decays silently. This derives
each claim from the registry and fails when the published figure disagrees.

WHAT IT DELIBERATELY DOES NOT CHECK
-----------------------------------
Prose that is not a derivable fact -- "7 platforms", pricing, positioning. Those
are real claims but nothing in the code can adjudicate them, and a gate that
guesses is worse than no gate.

Usage:
    python tools/audit_public_claims.py           # gate
    python tools/audit_public_claims.py --fix     # print the corrected lines
Exit 1 on any discrepancy.
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from app import capabilities as cap                      # noqa: E402
from app import capability_claims as cc                   # noqa: E402
from app.change_types import CHANGE_TYPE_MAP             # noqa: E402
from app.languages import languages                      # noqa: E402

# Files that make public claims. website/ is the served site; docs/ is the
# Docsify site; README is the repo front page.
SURFACES = [
    "README.md",
    "docs/index.html", "docs/getting-started.md", "docs/how-it-works.md",
    "docs/platforms.md", "docs/ci-cd-gate.md",
    "website/src/data/ripple.ts",
]


def _derived() -> dict:
    """The numbers the code actually supports."""
    n_tests = 0
    try:
        import json
        with open(os.path.join(ROOT, "tests", ".last_run.json")) as fh:
            n_tests = json.load(fh).get("total", 0)
    except (OSError, ValueError):
        n_tests = 0
    return {
        "contract types": len(cap.CONTRACT_ENGINES),
        "change types": len(CHANGE_TYPE_MAP),
        "languages": len(languages()),
        "tests": n_tests,
    }


# Claims that map 1:1 onto a derived fact. Exact match required.
PATTERNS = {
    r"(\d+)\s+contract\s+types?": "contract types",
    r"(\d+)\s+change\s+types?": "change types",
    r"(\d+)\s+tests?": "tests",
}

# "N languages" is NOT one fact, which is the whole point of the registry:
#
#     15  entries in the extension->language map          (detection)
#     11  languages with a SPECIFIC consumer matcher      (the other 4 fall
#                                                          through to generic)
#    8-15 languages with a fix handler, depending on the operation
#      0  cells that generate a fix AND are production-ready
#
# So a bare "12 languages" cannot be corrected to a number -- there is no single
# right one. An earlier version of this tool asserted it should be 15 and would
# have replaced one wrong figure with another, which is the exact over-claiming
# it exists to prevent.
#
# Instead the claim must say WHICH sense it means. Any of these qualifiers
# satisfies it; an unqualified count does not.
_LANGUAGE_CLAIM = re.compile(r"(\d+)\s+[Ll]anguages?")
_QUALIFIERS = (
    "detect", "detected", "detection", "consumer match", "matcher",
    "experimental", "fix template", "templates for", "scanned",
    "not production", "unvalidated", "repositories", "corpus", "dataset",
    "benchmark", "propbench",
)


def main(argv: list) -> int:
    derived = _derived()
    findings = []
    unqualified = []

    for rel in SURFACES:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            continue
        for lineno, line in enumerate(
                open(path, encoding="utf-8", errors="ignore"), start=1):
            for pat, key in PATTERNS.items():
                for m in re.finditer(pat, line):
                    claimed = int(m.group(1))
                    want = derived[key]
                    if want and claimed != want:
                        findings.append((rel, lineno, key, claimed, want,
                                         line.strip()[:70]))
            # Language counts must state which sense they mean.
            for m in _LANGUAGE_CLAIM.finditer(line):
                low = line.lower()
                if not any(q in low for q in _QUALIFIERS):
                    unqualified.append((rel, lineno, int(m.group(1)),
                                        line.strip()[:70]))

    print("=" * 74)
    print("PUBLIC CLAIMS vs DERIVED CAPABILITY")
    print("=" * 74)
    print("\n  derived from the code:")
    for k, v in derived.items():
        print(f"    {k:<16} {v}")

    n_specific = cc.summary()["find_consumer_ok"] and len(
        [l for l in languages() if cap.find_consumer(l)])
    ready = cc.summary()["production_ready"]
    fixable_ready = sum(
        1 for r in cc.claim_matrix()
        if r["production"] and "generate_fix" in cc.required_facts(r["operation"]))
    print(f"\n  context the numbers above do NOT convey:")
    print(f"    languages with a SPECIFIC consumer matcher   {n_specific} of "
          f"{len(languages())}")
    print(f"    cells production-ready (any category)        {ready}")
    print(f"    cells production-ready that GENERATE A FIX   {fixable_ready}")
    print(f"    -> a language count is not a support claim: SUPPORTED is not "
          f"FIXABLE is not VALIDATED")

    if not findings and not unqualified:
        print("\n  no numeric claim disagrees with the registry, and every "
              "language count states which sense it means")
        return 0

    if findings:
        print(f"\n  {len(findings)} NUMERIC DISCREPANCY(IES):")
        for rel, lineno, key, claimed, want, text in findings:
            print(f"      {rel}:{lineno}  claims {claimed} {key}, derived {want}")
            print(f"          {text}")
    if unqualified:
        print(f"\n  {len(unqualified)} UNQUALIFIED LANGUAGE CLAIM(S):")
        print(f"      A bare count cannot be checked, because 'languages' is "
              f"four different facts:")
        print(f"        15 detected | 11 with a specific matcher | 8-15 with a "
              f"fix handler | 0 validated")
        print(f"      Say which one. Qualifiers accepted: detection, consumer "
              f"matching, fix templates, experimental.")
        for rel, lineno, claimed, text in unqualified:
            print(f"      {rel}:{lineno}  \"{claimed} languages\" unqualified")
            print(f"          {text}")
    if "--fix" in argv:
        print("\n  corrected values:")
        for rel, lineno, key, claimed, want, text in findings:
            print(f"      {rel}:{lineno}  {claimed} -> {want}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
