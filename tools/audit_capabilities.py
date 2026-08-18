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
#: Proof that the e2e test actually COMPILED something, written only on a genuine
#: validated run. tests/.last_run.json cannot serve here: it records a skipped test
#: as "passed", so on a runner without docker the e2e claim would be honoured and
#: AUTO would fire behind a test that did nothing. Found in Stage 8, in Stage 6's own
#: work -- absence of evidence standing in for evidence, one layer up from where the
#: rule was originally applied.
E2E_EVIDENCE = os.path.join(ROOT, "tests", ".e2e_evidence.json")


def _e2e_proof() -> tuple:
    """(proven_cells, error). Missing proof is a FAILURE, never a pass."""
    if not os.path.exists(E2E_EVIDENCE):
        return set(), ("no e2e proof at tests/.e2e_evidence.json -- the e2e test "
                       "writes it only after a real compile, so either it has not "
                       "run or it SKIPPED for lack of a validation backend")
    try:
        with open(E2E_EVIDENCE) as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return set(), f"e2e proof unreadable: {type(exc).__name__}: {exc}"
    if not data.get("validated") or data.get("typecheck_exit") != 0:
        return set(), (f"e2e proof exists but does not show a successful compile: "
                       f"{json.dumps(data)[:160]}")
    return {tuple(data["cell"])}, ""


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

    proven, proof_error = _e2e_proof()
    unproven = sorted(set(cc.E2E_FIXTURES) - proven)
    print("\n  e2e claims backed by a real compile:")
    if proof_error:
        print(f"      FAIL  {proof_error}")
    for cell in unproven:
        print(f"      FAIL  {cell} claims e2e evidence with no proof of a compile")
    if not proof_error and not unproven:
        print(f"      {len(proven)} cell(s) proven: "
              f"{', '.join('/'.join(c) for c in sorted(proven))}")

    outside = _gate_capability_claims_outside_the_registry()

    if violations:
        print(f"\n  {len(violations)} VIOLATION(S):")
        for v in violations[:30]:
            print(f"      {v}")
        return 1

    if outside:
        return 1

    if proof_error or unproven:
        return 1

    print("\n  no unearned claims: every production=true has all five facts, "
          "and every e2e claim names a test that ran")
    return 0


# Eligibility lists that still decide capability WITHOUT consulting the registry.
# BLOCKING since Stage 6. Every file that held a per-language eligibility list is
# now deleted, and this asserts they do not come back.
#
# The distinction that matters: a per-language PATTERN TABLE is legitimate -- it is
# what generate_fix() derives FROM. A per-language ELIGIBILITY LIST is a second
# capability claim, and the registry cannot govern what it does not gate.
#
# All three offenders turned out to be DEAD, which is the honest finding:
#   fix_generator_multi.py   862 lines, zero production callers  (deleted, 9ece147)
#   ai_confidence.py         KNOWN_LANGUAGES, zero callers       (deleted, Stage 6)
#   impact_prediction.py     two lists, unreferenced AND unimportable (Stage 6)
# ai_confidence.py also carried a CATEGORY ERROR -- it listed `proto` and `graphql`
# as LANGUAGES when they are contract types; app/languages.py has always been
# right, so the error never reached a decision.
#
# Counting files had made these look like competing implementations. The call graph
# showed all three were dead, so none of their lists ever decided anything. What
# actually governs routing now is app/routing.py, which asks the registry.
_DELETED_ELIGIBILITY_LISTS = {
    "app/fix_generator_multi.py": "SUPPORTED_LANGUAGES + two inline tuples",
    "app/ai_confidence.py": "KNOWN_LANGUAGES (also listed proto/graphql as languages)",
    "app/impact_prediction.py": "two per-language lists",
}

# The one module allowed to answer eligibility, and the registry functions it must
# consult. Asserted so a future edit cannot quietly reintroduce a local list.
_ROUTER = "app/routing.py"


def _gate_capability_claims_outside_the_registry() -> int:
    problems = []

    for path, what in sorted(_DELETED_ELIGIBILITY_LISTS.items()):
        if os.path.exists(os.path.join(ROOT, path)):
            problems.append(
                f"{path} is back. It held {what}, which is a capability claim "
                f"outside the registry. Ask app/capability_claims.py instead.")

    router = os.path.join(ROOT, _ROUTER)
    if not os.path.exists(router):
        problems.append(f"{_ROUTER} is missing -- nothing governs routing.")
    else:
        src = open(router).read()
        if "capability_claims" not in src:
            problems.append(
                f"{_ROUTER} no longer consults capability_claims, so the registry "
                f"has stopped governing routing.")
        problems.extend(_language_lists_declared_in(router, src))

    print("\n  capability claims outside the registry:")
    if problems:
        for p in problems:
            print(f"      FAIL  {p}")
        return 1
    print(f"      none -- {len(_DELETED_ELIGIBILITY_LISTS)} list-holding module(s) "
          f"deleted and still gone")
    print(f"      {_ROUTER} decides routing by asking capability_claims")
    return 0


def _language_lists_declared_in(path: str, src: str) -> list:
    """Module-level collections of language names, found by PARSING not grepping.

    The first version of this check tested `"KNOWN_LANGUAGES" in src` and failed on
    app/routing.py's own docstring, which explains that KNOWN_LANGUAGES was deleted.
    A gate that cannot tell a declaration from prose about a declaration is the same
    defect as the phantom-field gate that grepped for getattr instead of parsing it.

    Checking SHAPE rather than NAME also makes it stronger: any module-level literal
    holding three or more known language names is an eligibility list whatever it is
    called, so renaming it does not evade the gate.
    """
    import ast
    from app import languages as _languages

    known = set(_languages.languages())
    problems = []
    tree = ast.parse(src)
    for node in tree.body:                      # module level only
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if isinstance(value, ast.Call):         # frozenset({...}) / set([...])
            value = value.args[0] if value.args else None
        if not isinstance(value, (ast.Set, ast.List, ast.Tuple)):
            continue
        names = {e.value for e in value.elts
                 if isinstance(e, ast.Constant) and isinstance(e.value, str)}
        hits = names & known
        if len(hits) >= 3:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            label = ", ".join(getattr(t, "id", "?") for t in targets)
            problems.append(
                f"{os.path.basename(path)} declares {label} at line {node.lineno} "
                f"-- {len(hits)} language name(s). Routing must ASK the registry, "
                f"not keep a copy.")
    return problems


if __name__ == "__main__":
    sys.exit(main(sys.argv))
