"""Literal extraction that works on every interpreter this repo runs on.

WHY THIS EXISTS
---------------
Python 3.7 parses a string literal to ``ast.Str`` (value on ``.s``). Python 3.8+
parses it to ``ast.Constant`` (value on ``.value``). Five call sites tested only
for ``ast.Constant``, so on 3.7 they extracted NOTHING -- and every one of them
then reported success over an empty set:

    tools/audit_change_types.py   printed "every emitted change type maps to a
                                  canonical operation" having found zero types
    tools/audit_capabilities.py   printed "capability claims outside the
                                  registry: none" being unable to find any list
    tools/audit_fail_silent.py    could not recognise `return ""` as an empty
                                  return, so it under-reported fail-silent paths
    app/capabilities.py (x2)      emitted_change_types() returned ~nothing, which
                                  collapsed detectable_pairs() from 63 to 10 and
                                  made every mechanical op look undetectable

The last one is the serious one: it is production code, it feeds
capability_claims -> routing, and the dev desktop runs 3.7 while CI and Railway
run 3.11. So the capability matrix was correct in production and wrong on the
machine where it was being read, which is the worst way round.

This is the third appearance of the same defect SHAPE in this repo -- a gate
whose PASS message asserts a property it never exercised. The first two were
logical (an unexercised ladder write, a stale read-back). This one is
environmental, which is why mutation testing did not catch it: the code is
correct on the interpreter the mutants ran under.

The lesson encoded here: an extractor that can return empty must be asserted
non-empty by its caller. ``str_literal`` fixes the mechanism;
``audit_change_types`` now fails on a zero extraction, which fixes the class.
"""

from __future__ import annotations

import ast

# ast.Str/Num/NameConstant are deprecated from 3.8 and slated for removal, so
# resolve them dynamically rather than referencing them at import time. On 3.8+
# the ast.Constant branch matches first and these are never consulted.
_AST_STR = getattr(ast, "Str", None)
_AST_NUM = getattr(ast, "Num", None)
_AST_NAME_CONSTANT = getattr(ast, "NameConstant", None)
_AST_BYTES = getattr(ast, "Bytes", None)

_MISSING = object()


def str_literal(node: ast.AST):
    """Return the ``str`` a string-literal node holds, else ``None``.

    ``None`` means "not a string literal", which is distinct from a node that
    holds an empty string -- callers that care about ``""`` must compare against
    ``None`` explicitly rather than testing truthiness.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if _AST_STR is not None and isinstance(node, _AST_STR):
        return node.s if isinstance(node.s, str) else None
    return None


def literal_value(node: ast.AST):
    """Return ``(found, value)`` for any literal node.

    Needed by the fail-silent audit, which tests membership in
    ``("", None, 0, False)`` -- so it needs numbers and ``None`` as well as
    strings. Returning a ``found`` flag rather than a sentinel value keeps a
    literal ``None`` distinguishable from "this was not a literal at all"; the
    audit's whole job is spotting ``return None``, so conflating those would
    reintroduce the bug in a new place.
    """
    if isinstance(node, ast.Constant):
        return True, node.value
    for kind, attr in ((_AST_STR, "s"), (_AST_BYTES, "s"),
                       (_AST_NUM, "n"), (_AST_NAME_CONSTANT, "value")):
        if kind is not None and isinstance(node, kind):
            return True, getattr(node, attr, _MISSING)
    return False, None


def source_of(node: ast.AST) -> str:
    """Render a node back to something source-like. ``ast.unparse`` when available.

    ``ast.unparse`` arrived in Python 3.9. Two audits used it unguarded, so on the
    3.7 dev desktop tools/audit_pipeline_governance.py did not merely mis-report --
    it died with ``AttributeError: module 'ast' has no attribute 'unparse'`` before
    reaching its conclusion. That is a better failure than a vacuous pass (it is
    loud) but it still means one of the ten gates could not be run locally at all.

    The fallback covers only the shapes the callers actually match on -- names,
    dotted attributes, calls, tuples and subscripts -- and yields "" for anything
    else. It is NOT a general unparser and must not be used as one: callers here
    test for a name or a substring, never for round-trippable source.

    NOTHING here swallows an exception. An earlier draft wrapped both branches in
    ``except Exception: return ""`` and the fail-silent gate rejected it, correctly:
    a render failure returning "" makes the CALLER report "no such loop found",
    which reads as a governance finding rather than as a broken renderer. Letting
    it raise costs a traceback and buys an accurate diagnosis.
    """
    unparse = getattr(ast, "unparse", None)
    if unparse is not None:
        return unparse(node)

    def render(n) -> str:
        if isinstance(n, ast.Name):
            return n.id
        if isinstance(n, ast.Attribute):
            base = render(n.value)
            return f"{base}.{n.attr}" if base else n.attr
        if isinstance(n, ast.Call):
            return f"{render(n.func)}(...)"
        if isinstance(n, ast.Tuple):
            return "(" + ", ".join(render(e) for e in n.elts) + ")"
        if isinstance(n, ast.Subscript):
            return f"{render(n.value)}[...]"
        found, value = literal_value(n)
        if found:
            return repr(value)
        # An unhandled node type. "" is a VALUE here, not a swallowed error: the
        # renderer is documented as partial, and the caller's substring test simply
        # will not match. Distinguish this from the exception case above.
        return ""

    return render(node)
