"""Declared capability claims -- the two facts that cannot be derived.

app/capabilities.py computes detect / find_consumer / generate_fix from the code.
Two facts cannot be computed:

    validate      nothing in this codebase can mechanically prove that generated
                  output compiles. Declared per LANGUAGE, because validation is a
                  property of a toolchain, not of a change type.
    e2e_tested    requires a fixture that exercised the whole path. Declared per
                  (language, contract, operation), and every claim must name the
                  test that proves it.

`production` is NOT a third declared fact. It is a pure function of all five, so
it cannot be set by hand -- that is the entire mechanism. Someone wanting to ship
a combination has to make the other five true.

WHY VALIDATION IS THREE-VALUED
------------------------------
app/validated_fix.py._validate_code ends with:

    else:
        # Can't validate -- assume valid
        return True, ""

That single line is why non-compiling output shipped. Measured: it returns VALID
for `phoneNumber: int32;` (TypeScript has no int32), for `public int32
PhoneNumber` (C# has no int32), for a half-fix that accepts a parameter and never
sends it, and for the literal string "!!! not rust". Six for six on garbage.

For a product that modifies other people's code, UNKNOWN IS NOT VALID. So the
state is three-valued and UNABLE_TO_VALIDATE never collapses into VALID. It is
also not INVALID -- claiming a fix is broken when you did not check is a
different lie.

WHY EVERY CLAIM POINTS AT EVIDENCE
----------------------------------
A boolean `e2e_tested: True` is a comment. A test NAME is checkable: CI asserts
the function exists and ran. Without that the registry becomes a nicer place to
store the same unverified assertion it was built to eliminate.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app import capabilities as cap


class ValidationState(str, Enum):
    """Three states. UNKNOWN is not VALID and it is not INVALID."""
    VALID = "VALID"
    INVALID = "INVALID"
    UNABLE_TO_VALIDATE = "UNABLE_TO_VALIDATE"


@dataclass(frozen=True)
class ValidatorSpec:
    """How a language's output would be mechanically verified.

    `implemented_by` is a dotted path to a callable that runs the real toolchain.
    Empty means the validator does NOT exist -- declaring the toolchain is not
    the same as having it, and this field is what keeps those apart.
    """
    language: str
    toolchain: str
    implemented_by: str = ""
    note: str = ""

    @property
    def is_wired(self) -> bool:
        """DERIVED: the dotted path must actually resolve to a callable.

        `bool(self.implemented_by)` was not enough. A declared path that does not
        import is a lie of exactly the kind this codebase keeps producing --
        app/impact_prediction.py was referenced, unimportable, and counted as real
        until someone tried to import it. Resolving the path makes the claim
        falsifiable at audit time rather than at runtime.
        """
        if not self.implemented_by:
            return False
        module, _, attr = self.implemented_by.partition(":")
        if not module or not attr:
            return False
        try:
            import importlib
            return callable(getattr(importlib.import_module(module), attr, None))
        except Exception:
            return False


# The toolchain each language WOULD need. TypeScript is now WIRED -- its
# implemented_by resolves to a real container-backed runner. python and go remain
# declarations: the toolchain is named, nothing runs it, and is_wired proves that by
# failing to resolve rather than by trusting an empty string.
#
# Deliberately not listing a validator for the other 11 languages. An entry here
# with no implementation is a plan; silence is the same state and makes no claim.
VALIDATORS: dict[str, ValidatorSpec] = {
    "typescript": ValidatorSpec(
        "typescript", "tsc --noEmit",
        implemented_by="app.validation:validate_typescript",
        note="Type errors are the failure mode that shipped -- `phoneNumber: "
             "int32` is syntactically fine and semantically impossible, so brace "
             "matching cannot catch it."),
    "python": ValidatorSpec(
        "python", "compileall + optional pytest",
        note="compile() catches syntax only. It passed a half-fix that accepts a "
             "parameter and never sends it, which is the most common defect."),
    "go": ValidatorSpec(
        "go", "go build ./...",
        note="Unused imports and unused variables are compile ERRORS in Go, so "
             "removing a field's last use breaks the build -- exactly the case a "
             "removal fix creates."),
}

# Claims that a fixture proved the complete path, keyed by cell, valued by the
# test that proves it.
#
# THE BAR: a fixture qualifies only if it exercises detection -> consumer
# discovery -> fix generation -> validation -> PR body. Partial-path tests do not
# count, however good they are -- tests/test_regression.py has several that go
# diff -> fix, and one that posts a synthetic push payload, but none reach a
# validated PR because validation does not exist yet.
#
# Empty is therefore the correct content, not an oversight. It becomes non-empty
# when the sandboxed validation runner exists.
E2E_FIXTURES: dict[tuple, str] = {}


# --------------------------------------------------------------------------
# Declared facts
# --------------------------------------------------------------------------

def validation_state(language: str) -> ValidationState:
    """What Ripple can say about generated output in this language.

    Never returns VALID from absence of evidence. VALID is reserved for a real
    toolchain run against real generated code, which no combination reaches yet.
    """
    spec = VALIDATORS.get(language)
    if spec is None or not spec.is_wired:
        return ValidationState.UNABLE_TO_VALIDATE
    return ValidationState.VALID


def validates(language: str) -> bool:
    """Boolean view for the production predicate. UNABLE_TO_VALIDATE is False."""
    return validation_state(language) is ValidationState.VALID


def e2e_evidence(language: str, contract: str, op: str) -> str:
    """The test name backing an e2e claim, or "" when there is no claim."""
    return E2E_FIXTURES.get((language, contract, op), "")


def e2e_tested(language: str, contract: str, op: str) -> bool:
    return bool(e2e_evidence(language, contract, op))


# --------------------------------------------------------------------------
# production -- a pure function, never a declaration
# --------------------------------------------------------------------------

def required_facts(op: str) -> tuple:
    """Which facts must hold for this operation to be production-ready.

    ONE definition, consumed by production_ready(), blocking_reasons() and
    tools/audit_capabilities.py. The first version encoded the rule separately in
    the predicate and in the audit; making the predicate category-aware left the
    audit asserting the old rule, and it reported 98 violations against correct
    behaviour. Two copies of a rule is the defect this registry exists to remove,
    so it is not repeated -- not even here.
    """
    category = _category_of(op)
    if category in ("wire_only", "non_breaking"):
        # Changing code would be WRONG: source never references proto field
        # numbers, and a non-breaking change is filtered before any fix path.
        # Requiring generate_fix would demand a transformation the category
        # forbids; requiring validation would demand validating code that was
        # never generated.
        return ("detect",)
    return ("detect", "find_consumer", "generate_fix", "validate", "e2e_tested")


def _fact_value(fact: str, language: str, contract: str, op: str):
    return {
        "detect": lambda: cap.detect(contract, op),
        "find_consumer": lambda: cap.find_consumer(language),
        "generate_fix": lambda: cap.generate_fix(language, op),
        "validate": lambda: validates(language),
        "e2e_tested": lambda: e2e_tested(language, contract, op),
    }[fact]()


def production_ready(language: str, contract: str, op: str) -> bool:
    """Every fact that APPLIES must hold. There is no way to assert this."""
    return all(_fact_value(f, language, contract, op)
               for f in required_facts(op))


def _category_of(op: str) -> str:
    from app.change_types import CANONICAL_OPS
    entry = CANONICAL_OPS.get(op)
    return entry[0] if entry else "unknown"


def blocking_reasons(language: str, contract: str, op: str) -> list:
    """Why a cell is not production-ready. Empty list means it is."""
    reasons = []
    for fact in required_facts(op):
        if _fact_value(fact, language, contract, op):
            continue
        if fact == "detect":
            reasons.append(f"no engine detects {op} in {contract}")
        elif fact == "find_consumer":
            reasons.append(f"{language} has no specific consumer matcher "
                           f"(generic fallback only)")
        elif fact == "generate_fix":
            reasons.append(f"no {language} transformation for {op}")
        elif fact == "validate":
            spec = VALIDATORS.get(language)
            want = f" (needs {spec.toolchain})" if spec else ""
            reasons.append(f"validation is "
                           f"{validation_state(language).value}{want}")
        elif fact == "e2e_tested":
            reasons.append("no end-to-end fixture")
    return reasons


def claim_row(language: str, contract: str, op: str) -> dict:
    """Derived facts plus declared facts plus the computed verdict."""
    row = cap.derived_row(language, contract, op)
    row["validate"] = validation_state(language).value
    row["e2e_tested"] = e2e_tested(language, contract, op)
    row["e2e_evidence"] = e2e_evidence(language, contract, op)
    row["production"] = production_ready(language, contract, op)
    return row


def claim_matrix(languages_=None, contracts=None) -> list:
    return [claim_row(r["language"], r["contract"], r["operation"])
            for r in cap.derived_matrix(languages_, contracts)]


def summary() -> dict:
    rows = claim_matrix()
    return {
        **cap.summary(),
        "cells": len(rows),
        "detect_ok": sum(1 for r in rows if r["detect"]),
        "find_consumer_ok": sum(1 for r in rows if r["find_consumer"]),
        "generate_fix_ok": sum(1 for r in rows if r["generate_fix"]),
        "validate_ok": sum(1 for r in rows if r["validate"] == "VALID"),
        "e2e_ok": sum(1 for r in rows if r["e2e_tested"]),
        "production_ready": sum(1 for r in rows if r["production"]),
        "validators_declared": len(VALIDATORS),
        "validators_wired": sum(1 for v in VALIDATORS.values() if v.is_wired),
    }
