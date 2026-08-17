#!/usr/bin/env python3
"""Fail the build on any capability claim that has not been earned.

Three invariants, in increasing strictness:

  1. production=true requires all five facts. `production` is computed, so this
     cannot be violated by declaration -- but it CAN be violated by someone
     loosening the predicate, so it is asserted rather than assumed.

  2. e2e_tested=true requires a named test that EXISTS.

  3. e2e_tested=true requires that test to have ACTUALLY RUN AND PASSED, read
     from tests/.last_run.json, which the suite writes on every invocation.

(2) is the check Stage 3 shipped and it is too weak on its own: a test can exist
and never execute, or be renamed while the claim keeps pointing at a stale name.
Existence is a reference; the run record is evidence.

MISSING EVIDENCE IS A FAILURE, NOT A PASS. If .last_run.json is absent, this exits
1 rather than skipping -- the same rule the registry applies to validation. A gate
that silently passes when it cannot check is the defect this whole registry exists
to remove.

Usage:
    python tools/audit_capabilities.py            # summary + gate
    python tools/audit_capabilities.py --matrix   # per-cell rows for the V1 set
Exit 1 on any violation.
"""
from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from app import capabilities as cap                      # noqa: E402
from app import capability_claims as cc                   # noqa: E402

RUN_EVIDENCE = os.path.join(ROOT, "tests", ".last_run.json")

# How stale the run record may be. Generous, because CI runs the suite seconds
# earlier; the point is to reject a record from a different session entirely.
MAX_EVIDENCE_AGE_SECONDS = 6 * 60 * 60

# The combinations the roadmap proposes for a narrow V1. Reported explicitly so
# the gap is visible in every build rather than needing to be looked up.
V1_LANGUAGES = ("typescript", "python", "go")
V1_CONTRACTS = ("openapi", "proto")


def _load_run_evidence() -> tuple:
    """(passed_set, error). Absence is an error, never an empty pass."""
    if not os.path.exists(RUN_EVIDENCE):
        return set(), (f"no run evidence at tests/.last_run.json -- run the "
                       f"regression suite first. Cannot verify e2e claims, and "
                       f"'cannot verify' is not 'verified'.")
    try:
        with open(RUN_EVIDENCE) as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        return set(), f"run evidence unreadable: {type(e).__name__}: {e}"
    age = time.time() - float(data.get("ran_at", 0))
    if age > MAX_EVIDENCE_AGE_SECONDS:
        return set(), (f"run evidence is {age/3600:.1f}h old (limit "
                       f"{MAX_EVIDENCE_AGE_SECONDS/3600:.0f}h) -- re-run the suite")
    return set(data.get("passed", [])), ""


def main(argv: list) -> int:
    violations = []
    passed_tests, evidence_error = _load_run_evidence()

    rows = cc.claim_matrix()
    claims = cc.E2E_FIXTURES

    # --- invariant 1: production implies all five -------------------------
    for r in rows:
        if not r["production"]:
            continue
        cell = f'{r["language"]}/{r["contract"]}/{r["operation"]}'
        # Ask capability_claims which facts APPLY. Hardcoding the five here left
        # this audit asserting the old rule after the predicate became
        # category-aware, and it reported 98 violations against correct
        # behaviour: wire_only must NOT have a transformation, and there is no
        # generated code to validate.
        for fact in cc.required_facts(r["operation"]):
            value = r["validate"] == "VALID" if fact == "validate" else r[fact]
            if not value:
                violations.append(
                    f"{cell}: production=true but {fact}="
                    f"{r.get(fact, r['validate'])}")

    # --- invariant 2 + 3: e2e claims name a test that RAN -----------------
    if claims and evidence_error:
        violations.append(f"{len(claims)} e2e claim(s) unverifiable: {evidence_error}")
    for cell, test_name in sorted(claims.items()):
        label = "/".join(cell)
        if not test_name:
            violations.append(f"{label}: e2e claimed with no test named")
            continue
        if not evidence_error and test_name not in passed_tests:
            violations.append(
                f"{label}: names {test_name!r}, which did not run and pass "
                f"in the recorded suite run")

    # --- report -----------------------------------------------------------
    s = cc.summary()
    print("=" * 74)
    print("CAPABILITY REGISTRY")
    print("=" * 74)
    print(f"\n  {s['languages']} languages x {s['detectable_pairs']} detectable "
          f"(contract, operation) pairs = {s['cells']} cells")
    print(f"  naive cross product would be "
          f"{s['contracts']} x {s['operations']} = {s['naive_cross_product']} "
          f"contract/op pairs; {s['naive_cross_product'] - s['detectable_pairs']} "
          f"are impossible\n")
    print(f"  {'fact':<18}{'cells':>8}   derived?")
    print(f"  {'-'*46}")
    for fact, count, derived in (
            ("detect", s["detect_ok"], "yes (AST of each engine)"),
            ("find_consumer", s["find_consumer_ok"], "yes (matcher naming)"),
            ("generate_fix", s["generate_fix_ok"], "yes (handler tables)"),
            ("validate", s["validate_ok"], "NO -- declared per language"),
            ("e2e_tested", s["e2e_ok"], "NO -- declared per cell"),
            ("production", s["production_ready"], "computed from all five")):
        print(f"  {fact:<18}{count:>8}   {derived}")
    print(f"\n  validators declared {s['validators_declared']}, "
          f"wired {s['validators_wired']}")
    print(f"  languages with no specific matcher: "
          f"{', '.join(s['languages_generic_only'])}")

    print(f"\n  V1 candidate set ({', '.join(V1_LANGUAGES)} x "
          f"{', '.join(V1_CONTRACTS)}):")
    v1 = [r for r in rows if r["language"] in V1_LANGUAGES
          and r["contract"] in V1_CONTRACTS]
    ready = [r for r in v1 if r["production"]]
    print(f"    {len(ready)}/{len(v1)} cells production-ready")
    blockers = {}
    for r in v1:
        for why in cc.blocking_reasons(r["language"], r["contract"], r["operation"]):
            key = why.split(" (")[0]
            blockers[key] = blockers.get(key, 0) + 1
    for why, n in sorted(blockers.items(), key=lambda x: -x[1]):
        print(f"    {n:>4} cells blocked: {why}")

    if "--matrix" in argv:
        print(f"\n  {'language':<12}{'contract':<10}{'operation':<20}"
              f"{'det':<5}{'find':<6}{'fix':<5}{'validate':<20}{'e2e':<5}prod")
        for r in sorted(v1, key=lambda x: (x["language"], x["contract"],
                                           x["operation"])):
            print(f"  {r['language']:<12}{r['contract']:<10}{r['operation']:<20}"
                  f"{str(r['detect'])[0]:<5}{str(r['find_consumer'])[0]:<6}"
                  f"{str(r['generate_fix'])[0]:<5}{r['validate']:<20}"
                  f"{str(r['e2e_tested'])[0]:<5}{str(r['production'])[0]}")

    if evidence_error and not claims:
        # No claims to verify, so the missing record is not yet a violation --
        # but say so, rather than printing a clean bill of health.
        print(f"\n  note: {evidence_error}")

    if violations:
        print(f"\n  {len(violations)} VIOLATION(S):")
        for v in violations[:30]:
            print(f"      {v}")
        return 1

    print("\n  no unearned claims: every production=true has all five facts, "
          "and every e2e claim names a test that ran")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
