"""Bugs found by replaying 12 real breaking commits (saleor-dashboard, tempo,
immich). Each test is one of those failures reduced to its smallest form.

Baseline report: 1,500+ generated changes, about 13 correct. The rest were Go
syntax corruption, deletions of unrelated same-named fields, phantom GraphQL
breaking changes and a fan-out of thousands of overlapping fixes.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.consumer_finder import ConsumerMatch  # noqa: E402
from app.diff_engine import BreakingChange  # noqa: E402
from app.fix_templates import MARKER  # noqa: E402


def _bc(field, change_type="field_removed", method="T", location="",
        path="spec"):
    return BreakingChange(change_type=change_type, path=path, method=method,
                          field_name=field, field_type="", location=location,
                          severity="breaking", description="")


def _consumer(lang, path="f"):
    return ConsumerMatch(file_path=path, line_number=1, code_snippet="",
                         confidence="high", match_reason="", language=lang)


def _fix(code, lang, bc):
    from app.fix_generator import generate_fix
    f = generate_fix(_consumer(lang), bc, use_llm=False, original_code=code)
    return (f.fixed_code, f.explanation) if f else (code, "")


def _template(code, lang, bc):
    from app.fix_generator import _generate_with_template
    return _generate_with_template(code, _consumer(lang), bc)


def _kept(original, fixed):
    """The original survives line for line once marker lines are dropped."""
    return [ln for ln in fixed.split("\n") if MARKER not in ln] == \
        original.split("\n")


# ── 1. an edit nothing can verify is never shipped ──────────────────────────

_GO = ('func f(rs []R) {\n'
       '\tvar s traceql.Static\n'
       '\tfor _, r := range rs {\n'
       '\t\t_ = r\n'
       '\t}\n'
       '}\n')


def test_go_removal_is_marked_not_corrupted():
    # tempo b42dc9b5d: became `var s traceql._, r := range rs {`.
    fixed, why = _fix(_GO, "go", _bc("static"))
    assert _kept(_GO, fixed), fixed
    assert fixed.count(MARKER) == 1 and "did NOT delete" in why, (fixed, why)


def test_refused_typescript_shape_is_marked_not_line_deleted():
    # saleor be378235b: the line fallback deleted this whole handler.
    ts = ('export function T({ onChange, checked, name }) {\n'
          '  return <Switch onChange={() => onChange({ target: { name, '
          'value: !checked } })} />;\n'
          '}\n')
    for fixed, _why in (_fix(ts, "typescript", _bc("target")),
                        _template(ts, "typescript", _bc("target"))):
        assert _kept(ts, fixed), fixed


def test_verified_typescript_removal_still_edits():
    ts = 'interface User {\n  id: string;\n  phoneNumber: string;\n}\n'
    fixed, why = _fix(ts, "typescript", _bc("phoneNumber"))
    assert MARKER not in fixed and "phoneNumber" not in fixed, (fixed, why)


def test_go_statement_deletion_is_marked_even_when_the_contract_would_pass():
    # tempo a2fccc26f: `b.ss[ts.PromLabels] = existing` deleted whole; every
    # deleted line names the field, so the diff contract alone accepts it.
    # The real tempo case corrupted syntax (`x := ts.(x)`), which the contract
    # does catch. This one it would accept -- a clean struct-field deletion --
    # and it must still not ship, because no Go scanner says the remaining
    # references (Go has no comment/string model here) are gone.
    go = 'type Series struct {\n\tName       string\n\tPromLabels []string\n}\n'
    # (With a `return s.PromLabels` below it, the Go template deletes the whole
    # function -- another reason no unverified Go edit ships.)
    from app.fix_templates import apply_fix_template
    from app.diff_contract import check
    raw, _ = apply_fix_template(go, "go", "field_removed", "prom_labels")
    assert raw != go and check(go, raw, "PromLabels", language="go").ok, raw
    fixed, why = _fix(go, "go", _bc("prom_labels"))
    assert _kept(go, fixed) and MARKER in fixed, (fixed, why)


def test_mentions_in_strings_get_no_marker():
    go = 'log.Error("static has unexpected type")\nx := 1\n'
    fixed, why = _template(go, "go", _bc("static"))
    assert fixed == go and why.startswith("No change"), (fixed, why)


def test_webhook_keeps_markers_as_the_last_resort():
    import app.webhook as w
    fixed, why = w._generate_fix_with_rag_fallback(_GO, _consumer("go", "a.go"),
                                                   _bc("static"), "")
    assert _kept(_GO, fixed) and MARKER in fixed, (fixed, why)
    assert why.startswith("[template]"), why


# ── 2. a one-word field only counts where its owning type appears ──────────

def test_generic_field_is_scoped_to_its_owner():
    from app.smart_consumer_finder import consumer_query, is_distinctive_name
    q = consumer_query(_bc("target", method="AppExtension", location="graphql"))
    assert q == ("AppExtension.target", "scoped_field"), q
    # Two-word names stay global: the tempo/immich consumers that were found
    # correctly (prom_labels 8/8, deviceAssetId 9/9) name no owner.
    assert consumer_query(_bc("prom_labels", method="TimeSeries")) == \
        ("prom_labels", "symbol")
    assert [is_distinctive_name(n) for n in
            ("target", "status", "password", "_entities", "searchData",
             "deviceAssetId", "prom_labels", "URL")] == \
        [False, False, False, False, True, True, True, False]
    # No owner known (OpenAPI verb, JSON Schema root): unchanged.
    assert consumer_query(_bc("status", method="get", path="/a")) == \
        ("status", "symbol")
    assert consumer_query(_bc("status", method="root")) == ("status", "symbol")
    # An AsyncAPI change names a channel, not a type.
    assert consumer_query(_bc("status", method="user/created")) == \
        ("status", "symbol")


def test_scoped_field_needs_the_owner_in_the_file():
    from app.smart_consumer_finder import find_matches_in_file as F
    dom = "el.addEventListener('click', e => go(e.target));\n"
    yml = "coverage:\n  status:\n    project:\n      default:\n        target: auto\n"
    real = ("fragment Ext on AppExtension {\n  id\n  target\n}\n")
    gen = "const t = (ext: AppExtensionFragment) => ext.target;\n"
    q = "AppExtension.target"
    assert F(dom, "a.ts", q, "typescript", vector="scoped_field") == []
    assert F(yml, "codecov.yml", q, "yaml", vector="scoped_field") == []
    assert F(real, "e.graphql", q, "graphql", vector="scoped_field")
    assert F(gen, "g.ts", q, "typescript", vector="scoped_field")
    # SQL table owner: plural table, singular model.
    assert F("u = User.query.get(1)\nprint(u.email)\n", "m.py", "users.email",
             "python", vector="scoped_field")
    assert not F("e.target.email\n", "m.py", "users.email", "python",
                 vector="scoped_field")
    # camelCase/snake owners must start an identifier: `reorders` is not Order.
    assert not F("reorders.status = 1\n", "r.ts", "orders.status", "typescript",
                 vector="scoped_field")
    assert F("order.status = 1\n", "r.ts", "orders.status", "typescript",
             vector="scoped_field")


def test_cli_walk_skips_files_without_the_owner():
    import tempfile
    from app.consumer_finder import find_consumers
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "Switch.tsx"), "w").write(
            "export const S = ({ onChange }) => onChange({ target: 1 });\n")
        open(os.path.join(d, "ext.ts"), "w").write(
            "export const t = (e: AppExtensionFragment) => e.target;\n")
        found = find_consumers([d], _bc("target", method="AppExtension",
                                        location="graphql"))
    assert {os.path.basename(m.file_path) for m in found} == {"ext.ts"}, found


# ── 3. GraphQL descriptions, strings and directives are not schema ─────────

def test_graphql_self_diff_is_empty():
    from app.graphql_diff import diff_graphql
    # saleor: `ripple diff X X` reported two breaking changes.
    s = ('type Mutation {\n'
         '  upd(\n'
         '    """When `{rate: null}` deletes; `{rate: Int}` updates."""\n'
         '    rates: [String!]!\n'
         '    note: String = "a ) # tricky default"\n'
         '  ): String\n'
         '}\n')
    assert diff_graphql(s, s) == []


def test_graphql_description_edit_is_not_a_change():
    from app.graphql_diff import diff_graphql
    old = 'type Query {\n  """\n  Note: this API is slow.\n  """\n  me: User\n}\n'
    new = 'type Query {\n  me: User\n}\n'
    # Was `operation_removed Query.Note`.
    assert diff_graphql(old, new) == []


def test_graphql_enum_values_are_names():
    from app.graphql_diff import diff_graphql
    e = 'enum K {\n  """Docs"""\n  DIGITAL\n  PHYSICAL\n}\n'
    dep = e.replace("  DIGITAL", '  DIGITAL @deprecated(reason: "use PHYSICAL")')
    assert diff_graphql(e, dep) == []                      # deprecated != removed
    assert [(c.change_type, c.field_name) for c in
            diff_graphql(e, e.replace("  DIGITAL\n", ""))] == \
        [("enum_value_removed", "DIGITAL")]
    assert diff_graphql('enum A { X Y }', 'enum A {\n  X\n  Y\n}') == []
    # Un-deprecating is not adding or removing values either.
    assert diff_graphql(dep, e) == []


def test_graphql_hash_comments_are_not_fields():
    from app.graphql_diff import diff_graphql
    d = 'type T {\n  a: Int\n  # c: Int\n  b: Int\n}\n'
    assert diff_graphql(d, d.replace("  # c: Int\n", "")) == []


# ── 4. one fix per file, one walk per run ───────────────────────────────────

def test_combined_fix_folds_every_change_into_one_file_fix():
    import tempfile
    from app.consumer_finder import find_consumers, load_sources
    from app.fix_generator import generate_combined_fixes
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "user.ts")
        open(p, "w").write("export interface User {\n  id: string;\n"
                           "  phoneNumber: string;\n  faxNumber: string;\n}\n")
        open(os.path.join(d, "pnpm-lock.yaml"), "w").write("phoneNumber: 1\n")
        sources = load_sources([d])
        assert [os.path.basename(x) for x, _c, _l in sources] == ["user.ts"]
        changes = [_bc("phone_number", method="User"),
                   _bc("fax_number", method="User")]
        per = [(c, find_consumers([d], c, sources=sources)) for c in changes]
        fixes = generate_combined_fixes(per, use_llm=False,
                                        sources={x: c for x, c, _l in sources})
    assert len(fixes) == 1, [f.consumer.file_path for f in fixes]
    assert fixes[0].fixed_code == "export interface User {\n  id: string;\n}\n", \
        fixes[0].fixed_code
    assert fixes[0].explanation.count("[field_removed") == 2


def test_a_change_is_applied_once_per_file_however_many_lines_match():
    import tempfile
    from app.consumer_finder import find_consumers, load_sources
    from app.fix_generator import generate_combined_fixes
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "u.ts"), "w").write(
            "export interface User {\n  id: string;\n  phoneNumber: string;\n}\n"
            "export interface Admin {\n  phoneNumber: string;\n}\n")
        src = load_sources([d])
        c = _bc("phone_number", method="User")
        per = [(c, find_consumers([d], c, sources=src))]
        assert len(per[0][1]) == 2, per
        fixes = generate_combined_fixes(per, use_llm=False,
                                        sources={x: y for x, y, _l in src})
    assert len(fixes) == 1 and fixes[0].explanation.count("[field_removed") == 1


def test_find_consumers_reads_the_tree_once():
    import tempfile
    import app.consumer_finder as cf
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "a.ts"), "w").write("x.phoneNumber\n")
        sources = cf.load_sources([d])
        real, calls = cf.load_sources, []
        cf.load_sources = lambda *a, **k: calls.append(1) or real(*a, **k)
        try:
            for f in ("phone_number", "fax_number"):
                cf.find_consumers([d], _bc(f), sources=sources)
        finally:
            cf.load_sources = real
    assert calls == [], "find_consumers re-walked the tree"


# ── 5. GraphQL documents embedded in TypeScript ─────────────────────────────

_GQL_TS = ('import { gql } from "@apollo/client";\n'
           'export const F = gql`\n'
           '  fragment TransactionItem on TransactionItem {\n'
           '    id\n'
           '    status\n'
           '    events { status name }\n'
           '    ... on Payment { status }\n'
           '  }\n'
           '`;\n'
           'const msg = `${u.status} done`;\n'
           'export const Q = gql`\n'
           '  query { order(id: 1) { status total } }\n'
           '`;\n')


def _gql_bc(field="status", owner="TransactionItem"):
    return _bc(field, method=owner, location="graphql")


def test_typed_selections_resolve_fragment_and_inline_types():
    from app.operation_sites import graphql_typed_selections as T
    doc = ('fragment TI on TransactionItem { id status events { status } '
           '... on Payment { status } ...Other ... Spaced }')
    got = [(n, t) for _o, _k, n, _d, t in T(doc)]
    assert got == [("id", "TransactionItem"), ("status", "TransactionItem"),
                   ("events", "TransactionItem"), ("status", None),
                   ("status", "Payment")], got


def test_gql_in_ts_removes_only_proven_selections():
    fixed, why = _fix(_GQL_TS, "typescript", _gql_bc())
    lines = fixed.split("\n")
    # TransactionItem.status removed from the fragment...
    assert "    status" not in lines, fixed
    # ...but events { status } (TransactionEvent), Payment.status, the
    # order { status } of another query and the message template are kept.
    for kept in ("    events { status name }", "    ... on Payment { status }",
                 "const msg = `${u.status} done`;",
                 "  query { order(id: 1) { status total } }"):
        assert kept in lines, (kept, fixed)
    # Unproven selections get ONE marker above each gql`` opening line, and
    # nothing is inserted inside a document.
    assert fixed.count(MARKER) == 2, fixed
    for i, ln in enumerate(lines):
        if MARKER in ln:
            assert lines[i + 1].rstrip().endswith("gql`"), (i, fixed)
    assert "Removed 1 selection" in why and "3 selection(s)" in why, why


def test_gql_in_ts_nothing_left_unproven_needs_no_marker():
    ts = ('const F = gql`\n  fragment T on TransactionItem {\n    id\n'
          '    status { code }\n  }\n`;\n')
    fixed, why = _fix(ts, "typescript", _gql_bc())
    assert fixed == ('const F = gql`\n  fragment T on TransactionItem {\n'
                     '    id\n  }\n`;\n'), fixed
    assert MARKER not in fixed


def test_gql_in_ts_removes_an_aliased_selection_with_its_alias():
    ts = ('const F = gql`\n  fragment T on TransactionItem {\n    id\n'
          '    st: status @include(if: $x)\n  }\n`;\n')
    fixed, _ = _fix(ts, "typescript", _gql_bc())
    assert fixed == ('const F = gql`\n  fragment T on TransactionItem {\n'
                     '    id\n  }\n`;\n'), fixed


def test_gql_in_ts_refuses_to_leave_an_empty_selection_set():
    from app.fix_templates import (embedded_graphql_docs,
                                   remove_graphql_selections)
    ts = 'const F = gql`fragment T on TransactionItem { status }`;\n'
    out, removed, unproven = remove_graphql_selections(
        ts, "status", "TransactionItem", embedded_graphql_docs(ts))
    assert (out, removed, unproven) == (ts, 0, 1), out


def test_gql_in_ts_only_for_graphql_changes_and_tagged_templates():
    from app.fix_templates import embedded_graphql_docs
    # A proto/OpenAPI change has no business editing a GraphQL document.
    fixed, _ = _fix(_GQL_TS, "typescript", _bc("status", method="TransactionItem"))
    assert "    status" in fixed.split("\n")
    assert len(embedded_graphql_docs(_GQL_TS)) == 2
    assert embedded_graphql_docs("const a = `fragment X on T { status }`;") == []
    assert len(embedded_graphql_docs("graphql(`{ a }`); /* GraphQL */ `{ b }`")) == 2


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _runner import run
    sys.exit(run(globals()))
