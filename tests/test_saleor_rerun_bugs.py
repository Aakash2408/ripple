"""The 15 wrong fixes left after the first round of real-repo fixes (saleor
be378235b and 3093be1b7, re-run on df6a798), each reduced to its smallest form.

Three causes:
  1. the TS remove-field codemod deleted the line that OPENS a multi-line value
     (`orderLine: {`) and left its body;
  2. the enum-value template deleted any line containing `<anything>.VALUE`,
     unscoped to the enum that changed, and treated `_` as a boundary;
  3. `orderLine` counted as specific because it has two words, although the
     schema uses it on eight types.
Plus the CLI diff headers, which ran together so no tool could apply them.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.consumer_finder import ConsumerMatch  # noqa: E402
from app.diff_engine import BreakingChange  # noqa: E402
from app.fix_templates import MARKER  # noqa: E402


def _bc(field, change_type="field_removed", owner="CustomerEvent",
        shared=False):
    return BreakingChange(change_type=change_type, path="schema.graphql",
                          method=owner, field_name=field, field_type="",
                          location="graphql", severity="breaking",
                          description="", name_shared=shared)


def _ts(path="f.ts"):
    return ConsumerMatch(file_path=path, line_number=1, code_snippet="",
                         confidence="high", match_reason="", language="typescript")


def _fix(code, bc, consumer=None):
    from app.fix_generator import _generate_with_template
    return _generate_with_template(code, consumer or _ts(), bc)


def _code_lines(text):
    return [ln for ln in text.split("\n") if MARKER not in ln]


# --- 1. multi-line values -------------------------------------------------

_NESTED = """export const lines = [
  {
    id: "1",
    orderLine: {
      id: "ol1",
      productName: "x",
    },
    quantity: 2,
  },
];
"""


def test_multiline_value_is_removed_with_its_whole_block():
    from app.ts_codemod import remove_field
    r = remove_field(_NESTED, "orderLine")
    assert r.code == ('export const lines = [\n  {\n    id: "1",\n'
                      '    quantity: 2,\n  },\n];\n'), r.code
    assert any(e["shape"] == "keyed property (inert multi-line value)"
               for e in r.edits), r.edits


def test_multiline_value_with_a_call_inside_is_refused():
    from app.ts_codemod import remove_field
    code = "const h = {\n  orderLine: {\n    id: getId(),\n  },\n};\n"
    r = remove_field(code, "orderLine")
    assert r.code == code and r.refusals, r


def test_block_that_does_not_end_on_its_own_line_is_refused():
    from app.ts_codemod import remove_field
    code = "const h = {\n  orderLine: {\n    id: 1,\n  }, other: 2,\n};\n"
    r = remove_field(code, "orderLine")
    assert r.code == code and r.refusals, r


def test_value_followed_by_another_property_on_the_line_is_refused():
    # The value pattern accepts commas: `orderLine: { a: 1 }, x: 2,` used to be
    # removed whole, deleting `x: 2` along with it.
    from app.ts_codemod import remove_field
    code = "const g = {\n  orderLine: { a: 1 }, x: 2,\n  y: 3,\n};\n"
    r = remove_field(code, "orderLine")
    assert r.code == code and r.refusals, r
    # ...while a separator INSIDE the value's own brackets is fine.
    code = "const g = {\n  orderLine: { a: 1, b: [2, 3] },\n  y: 3,\n};\n"
    assert remove_field(code, "orderLine").code == "const g = {\n  y: 3,\n};\n"


def test_contract_accepts_a_whole_block_but_not_its_body_alone():
    from app.diff_contract import check
    whole = 'const g = {\n  y: 1,\n};\n'
    head = 'const g = {\n  orderLine: {\n  },\n  y: 1,\n};\n'
    before = 'const g = {\n  orderLine: {\n    id: 1,\n  },\n  y: 1,\n};\n'
    assert check(before, whole, "orderLine").ok
    # Body gone, head kept: `id: 1,` is collateral damage.
    v = check(before, head, "orderLine")
    assert not v.ok and any("id: 1" in x for x in v.violations), v
    # A block opened by a line that does NOT name the field owns nothing.
    other = 'const g = {\n  meta: {\n    orderLine: 1,\n  },\n};\n'
    assert not check(other, 'const g = {\n};\n', "orderLine").ok


def test_any_template_edit_that_unbalances_brackets_becomes_markers():
    # The gate in _generate_with_template, not the codemod: a Java template
    # deleting `if (x.getOrderLine() != null) {` alone would shift `{` by one.
    from app.source_regions import brackets_preserved
    assert not brackets_preserved("a {\n b\n}\n", "b\n}\n")
    assert brackets_preserved("a {\n b\n}\n", "a {\n}\n")
    assert brackets_preserved('x = "{"\n', 'x = ""\n')   # string content
    assert brackets_preserved("x // {\n", "x\n")           # comment content
    import app.fix_generator as fg
    real = fg._template_fix
    fg._template_fix = lambda code, c, bc: (code.replace("  if (a) {\n", ""), "x")
    try:
        code = "function f() {\n  if (a) {\n    orderLine();\n  }\n}\n"
        out, why = _fix(code, _bc("orderLine"))
    finally:
        fg._template_fix = real
    assert _code_lines(out) == code.split("\n") and MARKER in out, out
    assert "unbalanced brackets" in why, why


# --- 2. enum values -------------------------------------------------------

_ENUM = """export enum TransactionActionEnum {
  CANCEL = 'CANCEL',
  VOID = 'VOID',
}
export enum OrderAction {
  VOID = 'VOID',
}
function f(action) {
  if (action === TransactionActionEnum.VOID) {
    onVoid();
  }
  const canVoid = actions.includes(OrderAction.VOID);
  const e = OrderErrorCode.VOID_INACTIVE_PAYMENT;
  const z = TransactionActionEnum.VOID_LEGACY;
  switch (action) {
    case TransactionActionEnum.CANCEL:
      return 1;
    case TransactionActionEnum.VOID:
      return intl.formatMessage({
        id: "x",
        defaultMessage: "Void",
      });
    case OrderEventsEnum.TRANSACTION_VOID_REQUESTED:
      return 3;
    default:
      return 0;
  }
}
"""


def _void():
    return _bc("VOID", change_type="enum_value_removed",
               owner="TransactionActionEnum", shared=True)


def test_enum_member_and_whole_arm_are_removed():
    out, _ = _fix(_ENUM, _void())
    assert "  VOID = 'VOID',\n}\nfunction" not in out.split("OrderAction")[0]
    assert "TransactionActionEnum {\n  CANCEL = 'CANCEL',\n}" in out, out
    assert "case TransactionActionEnum.VOID" not in out, out
    assert 'defaultMessage: "Void"' not in out, out         # whole arm, body too
    from app.source_regions import brackets_preserved
    assert brackets_preserved(_ENUM, out)


def test_other_enums_with_the_same_value_are_untouched():
    out, _ = _fix(_ENUM, _void())
    for kept in ("export enum OrderAction {\n  VOID = 'VOID',\n}",
                 "const canVoid = actions.includes(OrderAction.VOID);",
                 "const e = OrderErrorCode.VOID_INACTIVE_PAYMENT;",
                 "const z = TransactionActionEnum.VOID_LEGACY;",
                 "    case OrderEventsEnum.TRANSACTION_VOID_REQUESTED:\n      return 3;"):
        assert kept in out, kept
    # No marker on them either: only the owner's value is a reference.
    lines = out.split("\n")
    for i, ln in enumerate(lines):
        if MARKER in ln:
            assert re.search(r"TransactionActionEnum\.VOID(?!\w)", lines[i + 1]), \
                lines[i + 1]


def test_comparison_line_is_marked_not_deleted():
    out, _ = _fix(_ENUM, _void())
    i = out.index("  if (action === TransactionActionEnum.VOID) {\n    onVoid();\n  }")
    assert MARKER in out[:i].split("\n")[-2], out


def test_arm_that_another_arm_falls_into_is_only_marked():
    code = ("switch (a) {\n  case TransactionActionEnum.CANCEL:\n    doCancel();\n"
            "  case TransactionActionEnum.VOID:\n    doVoid();\n    break;\n}\n")
    out, _ = _fix(code, _void())
    assert _code_lines(out) == code.split("\n") and MARKER in out, out


def test_arm_that_falls_through_itself_is_only_marked():
    code = ("switch (a) {\n  case TransactionActionEnum.VOID:\n    doVoid();\n"
            "  case TransactionActionEnum.CANCEL:\n    doCancel();\n    break;\n}\n")
    out, _ = _fix(code, _void())
    assert _code_lines(out) == code.split("\n") and MARKER in out, out


def test_grouped_label_is_removed_and_the_shared_body_kept():
    code = ("switch (a) {\n  case TransactionActionEnum.VOID:\n"
            "  case TransactionActionEnum.REFUND:\n    return 9;\n}\n")
    out, _ = _fix(code, _void())
    assert out == ("switch (a) {\n  case TransactionActionEnum.REFUND:\n"
                   "    return 9;\n}\n"), out


def test_enum_without_an_owner_is_never_edited():
    out, _ = _fix(_ENUM, _bc("VOID", change_type="enum_value_removed", owner=""))
    assert _code_lines(out) == _ENUM.split("\n"), out


def test_enum_edits_only_in_typescript_and_javascript():
    py = ("from enum import Enum\nclass TransactionActionEnum(Enum):\n"
          "    VOID = 'VOID'\n\nif a == TransactionActionEnum.VOID:\n    x()\n")
    c = ConsumerMatch(file_path="f.py", line_number=1, code_snippet="",
                      confidence="high", match_reason="", language="python")
    out, _ = _fix(py, _void(), c)
    assert _code_lines(out) == py.split("\n") and MARKER in out, out


# --- 3. names the schema shares across types ------------------------------

def test_graphql_engine_flags_names_used_by_other_types():
    from app.graphql_diff import diff_graphql
    old = ("type CustomerEvent {\n  id: ID!\n  orderLine: OrderLine\n}\n"
           "type FulfillmentLine {\n  orderLine: OrderLine\n}\n"
           "type A {\n  onlyHere: Int\n  other: Int\n}\n"
           "enum E {\n  VOID\n  CANCEL\n}\nenum F {\n  VOID\n}\nenum G {\n  KEEP\n  GONE\n}\n")
    new = (old.replace("  id: ID!\n  orderLine: OrderLine\n", "  id: ID!\n")
              .replace("  onlyHere: Int\n", "")
              .replace("enum E {\n  VOID\n", "enum E {\n")
              .replace("  KEEP\n  GONE\n", "  KEEP\n"))
    got = {(c.method, c.field_name): c.name_shared for c in diff_graphql(old, new)}
    assert got == {("CustomerEvent", "orderLine"): True, ("A", "onlyHere"): False,
                   ("E", "VOID"): True, ("G", "GONE"): False}, got


def test_shared_multiword_field_gets_markers_not_edits():
    out, why = _fix(_NESTED, _bc("orderLine", shared=True))
    assert _code_lines(out) == _NESTED.split("\n") and MARKER in out, out
    assert "used by other types" in why, why
    # The same name, not shared, is still removed.
    out, _ = _fix(_NESTED, _bc("orderLine", shared=False))
    assert "orderLine" not in out and "productName" not in out, out


def test_shared_field_consumer_search_requires_the_owner():
    from app.smart_consumer_finder import consumer_query, needs_owner_scope
    assert needs_owner_scope(_bc("orderLine", shared=True))
    assert not needs_owner_scope(_bc("orderLine", shared=False))
    assert consumer_query(_bc("orderLine", shared=True)) == (
        "CustomerEvent.orderLine", "scoped_field")


# --- the CLI diff -----------------------------------------------------------

def test_diff_headers_are_separate_lines_and_git_applies_it():
    from app.fix_generator import _compute_diff
    d = _compute_diff("a\nb\nc\n", "a\nc\n", "x.ts")
    assert d.split("\n")[:3] == ["--- a/x.ts", "+++ b/x.ts", "@@ -1,3 +1,2 @@"], d
    with tempfile.TemporaryDirectory() as t:
        for name, before, after in (("x.ts", "a\nb\nc\n", "a\nc\n"),
                                    ("y.ts", "a\nb\nc", "a\nc"),    # no final \n
                                    ("z.ts", "a\nb", "a\nc")):       # last line changed
            with open(os.path.join(t, name), "w") as f:
                f.write(before)
            with open(os.path.join(t, name + ".diff"), "w") as f:
                f.write(_compute_diff(before, after, name))
            r = subprocess.run(["git", "apply", "--unsafe-paths", "--directory", "",
                                name + ".diff"], cwd=t, capture_output=True, text=True)
            if r.returncode:   # git apply outside a repo: fall back to patch -p1
                r = subprocess.run(["patch", "-p1", "-i", name + ".diff"], cwd=t,
                                   capture_output=True, text=True)
            assert r.returncode == 0, (name, r.stderr)
            with open(os.path.join(t, name)) as f:
                assert f.read() == after, name


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _runner import run
    sys.exit(run(globals()))
