#!/usr/bin/env python3
"""Full change_type x language coverage matrix.

Asserts the invariant this whole plan exists to establish:

    NO change_type any engine can emit may reach fix_templates and produce
    "Unknown change_type".

That matters because an unknown type leaves the code unchanged, so
fixed_code == content, so NO PR opens -- Ripple detects a breaking change and
then silently does nothing. Before this work fix_templates recognised 3 of 47
emitted types.

Also checks that each CATEGORY behaves per its contract:

    mechanical    should usually produce a change on representative code
    judgment      MUST produce a non-empty, RIPPLE-ACTION-REQUIRED-marked diff
                  (an empty diff opens no PR = silence)
    wire_only     MUST leave code unchanged AND say so explicitly, without
                  reading as a failure
    non_breaking  no fix expected

Usage:
    python3.12 tools/coverage_matrix.py            # summary
    python3.12 tools/coverage_matrix.py --verbose  # per-type rows
Exit 1 on any contract violation.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from app.change_types import CHANGE_TYPE_MAP, canonical_op, category
from app.fix_templates import apply_fix_template, MARKER

LANGUAGES = ["go", "typescript", "javascript", "python", "java",
             "rust", "ruby", "kotlin", "csharp"]

# Representative code per language, containing a field, a type reference,
# an enum arm and an operation call -- so every operation has something to act
# on and a "no change" result means a real gap rather than a thin fixture.
SAMPLES = {
    "go": 'type Wrap struct {\n\tPhoneNumber string\n\tUser *User\n}\n\nfunc f(c *Client) error {\n\tu := User{PhoneNumber: p}\n\tr, err := c.svc.DeleteUser(ctx)\n\tswitch s {\n\tcase Status_LEGACY:\n\t\treturn nil\n\t}\n\treturn err\n}',
    "typescript": 'import { User } from "./gen";\n\ninterface Wrap {\n  phoneNumber: string;\n  user: User;\n}\n\nconst u: User = new User();\nawait c.deleteUser(req);\nswitch (s) {\n  case Status.LEGACY:\n    break;\n}',
    "javascript": 'const { User } = require("./gen");\nconst u = new User();\nc.deleteUser(req);\nswitch (s) {\n  case Status.LEGACY:\n    break;\n}',
    "python": 'from gen import User\n\nclass Wrap:\n    phone_number: str\n    user: User\n\nu = User()\nr = c.delete_user(req)\nclass Status(Enum):\n    LEGACY = 1',
    "java": 'import com.x.User;\n\nclass Wrap {\n  private String phoneNumber;\n  private User user;\n  User u = new User();\n  Resp r = c.deleteUser(req);\n}\nenum Status { LEGACY, ACTIVE }',
    "rust": 'use gen::User;\n\nstruct Wrap {\n    phone_number: String,\n    user: User,\n}\nlet u: User = User {};\nlet r = c.delete_user(req);\nmatch s {\n    LEGACY => 1,\n}',
    "ruby": 'require "user"\nu = User.new\nr = c.delete_user(req)\ncase s\nwhen LEGACY\n  1\nend',
    "kotlin": 'import gen.User\n\ndata class Wrap(\n    val phoneNumber: String,\n    val user: User,\n)\nval u: User = User()\nval r = c.deleteUser(req)\nwhen (s) {\n    LEGACY -> 1\n}',
    "csharp": 'using Gen.User;\n\nclass Wrap {\n  public string PhoneNumber { get; set; }\n  public User User { get; set; }\n  var u = new User();\n  var r = c.DeleteUser(req);\n}\nenum Status { LEGACY, ACTIVE }',
}

# Symbol to target, per canonical operation
SYMBOL = {
    "remove_field": "phone_number",
    "change_field_type": "phone_number",
    "rename_field": "phone_number",
    "remove_type": "User",
    "rename_type": "User",
    "remove_enum_value": "LEGACY",
    "remove_operation": "DeleteUser",
    "add_required": "country",
    "restrict_schema": "DeleteUser",
    "wire_incompatible": "phone_number",
    "add_optional": "country",
}

FAILURE_STRINGS = ("Unknown change_type", "Unclassified change_type")


def main(argv: list) -> int:
    verbose = "--verbose" in argv
    violations = []
    stats = {"combos": 0, "changed": 0, "unchanged": 0, "unknown": 0}
    by_cat = {}

    for ct in sorted(CHANGE_TYPE_MAP):
        op = canonical_op(ct)
        cat = category(ct)
        symbol = SYMBOL.get(op, "phone_number")
        row = []

        for lang in LANGUAGES:
            code = SAMPLES[lang]
            stats["combos"] += 1
            try:
                fixed, expl = apply_fix_template(
                    code, lang, ct, symbol,
                    new_name="Account", old_type="string", new_type="int32",
                )
            except Exception as e:
                violations.append(f"{ct}/{lang}: raised {type(e).__name__}: {e}")
                row.append("X")
                continue

            unknown = any(s in expl for s in FAILURE_STRINGS)
            changed = fixed != code

            if unknown:
                stats["unknown"] += 1
                violations.append(f"{ct}/{lang}: unknown-type escape")
                row.append("U")
                continue

            # per-category contracts
            if cat == "judgment":
                if not changed or MARKER not in fixed:
                    violations.append(
                        f"{ct}/{lang}: judgment produced no marked diff "
                        f"(opens no PR = silence)")
                    row.append("!")
                    continue
            elif cat == "wire_only":
                if changed:
                    violations.append(
                        f"{ct}/{lang}: wire-only modified source")
                    row.append("!")
                    continue
                if "NO SOURCE CHANGE REQUIRED" not in expl:
                    violations.append(
                        f"{ct}/{lang}: wire-only lacks explicit no-op wording")
                    row.append("!")
                    continue

            stats["changed" if changed else "unchanged"] += 1
            row.append("." if changed else "-")

        by_cat.setdefault(cat, []).append((ct, op, "".join(row)))

    print("=" * 78)
    print("COVERAGE MATRIX")
    print("=" * 78)
    print(f"\n  {len(CHANGE_TYPE_MAP)} change types x {len(LANGUAGES)} languages "
          f"= {stats['combos']} combos\n")

    for cat in ("mechanical", "judgment", "wire_only", "non_breaking"):
        rows = by_cat.get(cat, [])
        if not rows:
            continue
        print(f"  {cat.upper()} ({len(rows)} types)")
        if verbose:
            for ct, op, row in rows:
                print(f"      {ct:34} {op:18} {row}")
        else:
            clean = all(c in ".-" for _, _, r in rows for c in r)
            print(f"      {'all rows conform to contract' if clean else 'SEE VIOLATIONS'}")
        print()

    print(f"  legend: . changed   - unchanged   U unknown-type   ! contract violation   X raised")
    print(f"  changed {stats['changed']} | unchanged {stats['unchanged']} | "
          f"unknown {stats['unknown']}")

    if violations:
        print(f"\n  {len(violations)} VIOLATION(S):")
        for v in violations[:25]:
            print(f"      {v}")
        return 1

    print("\n  ✅ no unknown-type escapes; every category honours its contract")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
