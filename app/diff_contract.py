"""Did the transformation change ONLY what it was supposed to change?

WHY THE COMPILER IS NOT ENOUGH -- MEASURED, NOT ARGUED
Five corrupting mutations were applied to a fix that `tsc --noEmit` had accepted:

    delete an unrelated field       VALID   <- compiler blind
    change the wrong property       VALID   <- compiler blind
    delete an unrelated function    VALID   <- compiler blind
    introduce a syntax error        INVALID
    no-op                           INVALID

Three of five passed. `{ email: user.email }` -> `{ email: user.fullName }` typechecks
perfectly: both are `string`. Deleting a field nobody reads typechecks perfectly.
Deleting an entire unrelated function typechecks perfectly.

So a green compiler means "the result is well-typed", never "the change was correct".
Validation and diff correctness are different questions and only one of them was
being asked.

THE CONTRACT FOR A REMOVAL
Every deletion must justify itself by referencing the removed field, and nothing may
be added:

    deleted line       must contain the field
    modified line      only DELETIONS within it, and the deleted text must contain
                       the field -- zero insertions
    added line         forbidden outright
    comments           every comment in `before` must survive
    string literals    every string in `before` must survive

The "zero insertions" rule is what catches a changed property: replacing
`user.email` with `user.fullName` inserts `fullName`, and no correct removal ever
inserts anything.

The "every deleted line contains the field" rule is what catches collateral damage,
and it is stated as a requirement on EACH deletion rather than as a count. A count
would pass a patch that deleted one correct line and one wrong one.

WHAT THIS DOES NOT DO
It cannot tell whether removing the reference was the right product decision -- only
that the mechanical change was confined to references of that field. Semantic
correctness of the intent stays with the human, which is what REVIEW is for.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field as _field

from app.ts_codemod import _regions


@dataclass
class DiffVerdict:
    ok: bool
    violations: list = _field(default_factory=list)
    summary: dict = _field(default_factory=dict)

    def as_detail(self) -> dict:
        return {"diff_ok": self.ok,
                "diff_violations": self.violations[:5],
                **{f"diff_{k}": v for k, v in self.summary.items()}}


def _comments_and_strings(code: str) -> list:
    """The text of every comment and string literal, for survival checking."""
    out = []
    for start, end, kind in _regions(code):
        text = code[start:end].strip()
        if len(text) > 2:               # ignore empty template runs and ""
            out.append((kind, text))
    return out


def check(before: str, after: str, field: str) -> DiffVerdict:
    """Verify a removal changed only references to `field`."""
    if not field:
        return DiffVerdict(False, ["no field name given"])

    b_lines = before.splitlines(keepends=True)
    a_lines = after.splitlines(keepends=True)
    sm = difflib.SequenceMatcher(None, b_lines, a_lines, autojunk=False)

    violations = []
    deleted = modified = added = 0

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            added += j2 - j1
            for line in a_lines[j1:j2]:
                violations.append(
                    f"ADDED a line, which no removal should ever do: "
                    f"`{line.strip()[:70]}`")
        elif tag == "delete":
            deleted += i2 - i1
            for line in b_lines[i1:i2]:
                if not re.search(rf"\b{re.escape(field)}\b", line):
                    violations.append(
                        f"DELETED a line that does not reference {field!r} -- "
                        f"collateral damage: `{line.strip()[:70]}`")
        else:                            # replace
            # Pair the replaced blocks up; anything unpaired is an add or delete.
            b_block, a_block = b_lines[i1:i2], a_lines[j1:j2]
            if len(a_block) > len(b_block):
                added += len(a_block) - len(b_block)
                violations.append(
                    f"ADDED {len(a_block) - len(b_block)} line(s) inside a "
                    f"replacement")
            for bl, al in zip(b_block, a_block):
                if bl == al:
                    continue
                modified += 1
                ins, dele = _char_delta(bl, al)
                if ins:
                    violations.append(
                        f"INSERTED text into a line, which a removal never does: "
                        f"{ins!r} in `{al.strip()[:60]}`")
                if dele and not re.search(rf"\b{re.escape(field)}\b", dele):
                    violations.append(
                        f"REMOVED text that does not reference {field!r}: "
                        f"{dele!r}")
            for bl in b_block[len(a_block):]:
                deleted += 1
                if not re.search(rf"\b{re.escape(field)}\b", bl):
                    violations.append(
                        f"DELETED a line that does not reference {field!r}: "
                        f"`{bl.strip()[:70]}`")

    # The field must be GONE from code positions. A surviving reference in a comment
    # or string is expected and reported elsewhere as a note.
    regions = _regions(after)
    for m in re.finditer(rf"\b{re.escape(field)}\b", after):
        if not any(s <= m.start() < e for s, e, _ in regions):
            line_no = after[:m.start()].count("\n") + 1
            violations.append(
                f"{field!r} still present in CODE at line {line_no} -- the removal "
                f"is incomplete, so the file will not compile")
            break

    # Comments and strings must survive. Rewriting a customer's prose or a log
    # message is not this transformation's business.
    survived = {t for _k, t in _comments_and_strings(after)}
    for kind, text in _comments_and_strings(before):
        if text not in survived:
            violations.append(f"a {kind} did not survive the change: {text[:60]!r}")

    if not deleted and not modified:
        violations.append("nothing changed -- a no-op is not a fix")

    return DiffVerdict(
        ok=not violations,
        violations=violations,
        summary={"deleted_lines": deleted, "modified_lines": modified,
                 "added_lines": added, "field": field},
    )


def _char_delta(before_line: str, after_line: str) -> tuple:
    """(inserted_text, deleted_text) between two versions of one line."""
    sm = difflib.SequenceMatcher(None, before_line, after_line, autojunk=False)
    ins, dele = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("insert", "replace"):
            ins.append(after_line[j1:j2])
        if tag in ("delete", "replace"):
            dele.append(before_line[i1:i2])
    return "".join(ins).strip(), "".join(dele)
