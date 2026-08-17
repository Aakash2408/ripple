"""Capability registry: what Ripple can actually do, derived from the code.

WHY THIS EXISTS
---------------
Capability information was spread across language detectors, consumer matchers,
fix templates, validators and routing logic, so "Kotlin is supported" could be
true in one place and false in four others. That is how the repo ended up
claiming 12-language fix generation while change_field_type was broken in all
nine languages it covered, and how six language classifiers existed that
production could not reach.

A hand-maintained registry would become the NINTH place capability lives, and it
would drift for the same reason the others did. So the three facts that can be
computed ARE computed, every time, from the modules that own them:

    detect(contract, op)          <- which change types each diff engine emits
    find_consumer(language)       <- whether the matcher names that language
    generate_fix(language, op)    <- which handler table the operation dispatches to

Nothing here is written down twice. If a handler is added or removed, this module
reports the change on the next run without anyone editing it.

WHAT IS *NOT* HERE
------------------
`validate` and `e2e_tested` cannot be derived -- nothing in the codebase can
mechanically prove that generated output compiles, or that a fixture exercised
the whole path. Those are declared in Stage 3 and verified in CI, and
`production` is a pure function of all five. See capability_claims.py.

THE MATRIX IS SPARSE
--------------------
Contract type and change type are not independent: a proto field-number change
cannot occur in OpenAPI, and tRPC emits only two operations. 53 of the 120
naive (contract x operation) combinations can actually occur. Enumerating the
cross product would report 67 permanently-empty cells and invite someone to
"fix" combinations that are physically impossible.
"""
from __future__ import annotations

import functools
import os
import re

from app.change_types import CANONICAL_OPS, CHANGE_TYPE_MAP, canonical_op
from app.languages import languages

_APP = os.path.dirname(os.path.abspath(__file__))

# Contract name -> the module that diffs it. This is NAMING, not capability: the
# facts below are read out of these files, never asserted about them. Kept
# explicit because filenames do not yield contract names reliably
# (diff_engine.py is OpenAPI, migration_diff.py is the database).
CONTRACT_ENGINES = {
    "openapi": "diff_engine.py",
    "proto": "proto_diff.py",
    "graphql": "graphql_diff.py",
    "database": "migration_diff.py",
    "asyncapi": "asyncapi_diff.py",
    "avro": "avro_diff.py",
    "trpc": "trpc_diff.py",
    "thrift": "thrift_diff.py",
    "jsonschema": "jsonschema_diff.py",
    "smithy": "smithy_diff.py",
}

# Operation -> the fix_templates attribute whose keys are the languages it
# supports. Derived by reading the dispatch in apply_fix_template; see
# test_capability_tables_match_the_dispatch, which fails if this drifts from the
# code it describes.
#
# Note the inversion this exposes: the JUDGMENT operations annotate, so they need
# only a comment token and reach all 15 languages, while the MECHANICAL ones need
# language-specific patterns and reach 8-9. The operations Ripple refuses to
# complete have BROADER language support than the ones it completes.
_OP_TABLE = {
    "remove_field": "REMOVE_HANDLERS",
    "change_field_type": "TYPE_CHANGE_HANDLERS",
    "remove_type": "_TYPE_REF_PATTERNS",
    "rename_type": "_TYPE_REF_PATTERNS",
    "remove_enum_value": "_ENUM_VALUE_PATTERNS",
    "rename_field": "_LINE_COMMENT",        # case-variant replacement, all langs
    "remove_operation": "_LINE_COMMENT",    # comments sites out
    "add_required": "_LINE_COMMENT",        # annotates
    "restrict_schema": "_LINE_COMMENT",     # annotates
    "remove_package": "_LINE_COMMENT",      # annotates
}


# ---------------------------------------------------------------------------
# detect -- per contract. Read out of the engine sources.
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=None)
def emitted_change_types(contract: str) -> frozenset:
    """The dialect strings a contract's engine actually CONSTRUCTS.

    Parses the AST rather than matching text. The first version of this grepped
    for quoted occurrences and reported that OpenAPI could detect rename_field --
    because diff_engine.py has the comment

        change_type: str    # "added_required_field", "removed_field", "renamed_field"

    A registry that infers capability from prose is the exact failure it exists
    to prevent, and it over-claims, which is the dangerous direction.

    Counts a change type as emitted when the string is either the `change_type=`
    keyword of a call, or the first positional argument to a BreakingChange
    constructor or a local `_bc`-style helper (proto_diff uses the latter).
    """
    module = CONTRACT_ENGINES.get(contract)
    if not module:
        return frozenset()
    path = os.path.join(_APP, module)
    if not os.path.exists(path):
        return frozenset()

    import ast
    try:
        tree = ast.parse(open(path, encoding="utf-8", errors="ignore").read())
    except SyntaxError:
        return frozenset()

    found = set()

    def _callee(node) -> str:
        f = node.func
        if isinstance(f, ast.Name):
            return f.id
        if isinstance(f, ast.Attribute):
            return f.attr
        return ""

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # change_type="..." on any call
        for kw in node.keywords:
            if (kw.arg == "change_type" and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)):
                found.add(kw.value.value)
        # _bc("field_removed", ...) / BreakingChange("field_removed", ...)
        name = _callee(node)
        if (name == "BreakingChange" or name.endswith("_bc") or name == "_bc") \
                and node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            found.add(node.args[0].value)

    return frozenset(ct for ct in found if ct in CHANGE_TYPE_MAP)


@functools.lru_cache(maxsize=None)
def event_layer_ops() -> frozenset:
    """Operations detected at the PUSH-EVENT layer, not by any diff engine.

    Every engine has the shape diff_x(old, new, path) and therefore never sees
    more than one file, so a deleted contract file or directory is invisible to
    all of them -- it is detected in webhook._find_removed_specs. These are
    contract-INDEPENDENT: deleting a .proto and deleting an .avsc are the same
    event.

    Without this, the registry reports remove_package as undetectable and
    under-claims a capability that works. Reads the registry change_types
    already keeps for the audit, rather than restating the list here.
    """
    try:
        from app.change_types import EVENT_LAYER_TYPES
    except ImportError:
        return frozenset()
    types = (EVENT_LAYER_TYPES.keys()
             if isinstance(EVENT_LAYER_TYPES, dict) else EVENT_LAYER_TYPES)
    return frozenset(canonical_op(ct) for ct in types if ct in CHANGE_TYPE_MAP)


@functools.lru_cache(maxsize=None)
def detect(contract: str, op: str) -> bool:
    """Can this operation be detected for this contract?

    True either because the contract's engine constructs a dialect for it, or
    because it is detected at the push-event layer for every contract.
    """
    if op in event_layer_ops():
        return True
    return any(canonical_op(ct) == op for ct in emitted_change_types(contract))


@functools.lru_cache(maxsize=None)
def detectable_pairs() -> tuple:
    """Every (contract, op) that can actually occur. Sparse by construction."""
    return tuple(sorted(
        (c, op) for c in CONTRACT_ENGINES for op in CANONICAL_OPS
        if detect(c, op)
    ))


# ---------------------------------------------------------------------------
# find_consumer -- per language. Read out of the matcher.
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=None)
def find_consumer(language: str) -> bool:
    """Does the matcher have language-SPECIFIC handling, or only a fallback?

    smart_consumer_finder keeps no language-keyed table; it branches on string
    literals. A language it does not name still gets matched by generic
    patterns -- which is why this is a capability question and not a crash: the
    cost of an unnamed language is the wrong dialect, silently.
    """
    body = open(os.path.join(_APP, "smart_consumer_finder.py"),
                encoding="utf-8", errors="ignore").read()
    return bool(re.search(rf'["\']{re.escape(language)}["\']', body))


# ---------------------------------------------------------------------------
# generate_fix -- per (language, op). Read out of the handler tables.
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=None)
def _table_languages(attr: str) -> frozenset:
    from app import fix_templates
    table = getattr(fix_templates, attr, None)
    if not isinstance(table, dict):
        return frozenset()
    return frozenset(set(table) & set(languages()))


@functools.lru_cache(maxsize=None)
def generate_fix(language: str, op: str) -> bool:
    """Is there a transformation for this operation in this language?

    NOT a claim that the output is correct, or that it compiles -- only that a
    handler exists. That distinction is the entire point of the registry:
    SUPPORTED is not FIXABLE is not VALIDATABLE.
    """
    attr = _OP_TABLE.get(op)
    if attr is None:
        return False
    return language in _table_languages(attr)


# ---------------------------------------------------------------------------
# The derived view
# ---------------------------------------------------------------------------

def derived_row(language: str, contract: str, op: str) -> dict:
    """The three derivable facts for one cell. No stored state."""
    return {
        "language": language,
        "contract": contract,
        "operation": op,
        "category": CANONICAL_OPS[op][0] if op in CANONICAL_OPS else "unknown",
        "detect": detect(contract, op),
        "find_consumer": find_consumer(language),
        "generate_fix": generate_fix(language, op),
    }


def derived_matrix(languages_=None, contracts=None) -> list:
    """Every (language, contract, op) whose (contract, op) can actually occur."""
    langs = sorted(languages_ or languages())
    pairs = [p for p in detectable_pairs()
             if contracts is None or p[0] in contracts]
    return [derived_row(l, c, op) for l in langs for c, op in pairs]


def summary() -> dict:
    """Counts, for CI output and for spotting drift at a glance."""
    langs = sorted(languages())
    pairs = detectable_pairs()
    return {
        "languages": len(langs),
        "contracts": len(CONTRACT_ENGINES),
        "operations": len(CANONICAL_OPS),
        "detectable_pairs": len(pairs),
        "naive_cross_product": len(CONTRACT_ENGINES) * len(CANONICAL_OPS),
        "languages_with_specific_matcher": sum(1 for l in langs if find_consumer(l)),
        "languages_generic_only": sorted(l for l in langs if not find_consumer(l)),
        "cells": len(langs) * len(pairs),
    }
