"""Remove references to a deleted field from TypeScript, or refuse.

WHY THIS REPLACES THE REGEX
The previous `_remove_field_typescript` applied context-free substitutions to
context-sensitive syntax. Measured against the golden fixture in Stage 4 it produced:

    -    phone: user.phoneNumber,
    -  };
    +    phone: user.};

...which does not parse, while reporting "Removed all references to field
'phoneNumber' (1 lines affected)". The destructuring cleanup `\\b{field}\\s*,\\s*`
stripped `phoneNumber,` wherever it appeared, including as the tail of a member
expression. And the access pattern `^\\s*\\S*\\.field\\b.*$` only matched a line whose
FIRST token was the access, so a template-literal interpolation survived untouched.

A wider regex cannot fix this. Removing `user.phoneNumber` from an expression
requires knowing what the surrounding expression IS.

THE DESIGN: NARROW AND HONEST, NOT BROAD AND HOPEFUL
This handles exactly two shapes, both of which are provably safe to remove because
the value has no remaining effect:

    template interpolation   `${user.phoneNumber}`     -> delete the whole ${...}
    object-literal property  `phone: user.phoneNumber,` -> delete the whole property

Everything else is REFUSED: the code comes back unchanged with a stated reason, the
outcome enum reports BLOCKED, and a human decides. That is the correct answer for a
reference the value of which is load-bearing --

    const phone = user.phoneNumber;
    return phone ? `sms:${phone}` : `mailto:${user.email}`;

-- where removal forces a behavioural decision no transformation can make. That case
is `JUDGMENT` wearing a `remove_field` label.

A transformation that abstains when it cannot be sure, paired with a validator that
catches it when it is wrong anyway, is what makes the cell safe. Breadth here would
mean guessing, and a guess that compiles is worse than a refusal.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as _field


@dataclass
class CodemodResult:
    code: str
    changed: bool
    edits: list = _field(default_factory=list)
    refusals: list = _field(default_factory=list)

    @property
    def complete(self) -> bool:
        """Every reference was handled. A partial removal still leaves a type error,
        so 'some edits' is not success -- it is a different failure."""
        return self.changed and not self.refusals


def _interpolation_spans(code: str) -> list:
    """(start, end) of every `${...}` inside a template literal, brace-aware.

    Tracks backticks so `${...}` in a normal string is not touched, and counts
    nested braces so `${ {a:1}.a }` ends at the right place.
    """
    spans, i, n = [], 0, len(code)
    in_template = False
    while i < n:
        ch = code[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "`":
            in_template = not in_template
            i += 1
            continue
        if in_template and ch == "$" and i + 1 < n and code[i + 1] == "{":
            depth, j = 1, i + 2
            while j < n and depth:
                if code[j] == "{":
                    depth += 1
                elif code[j] == "}":
                    depth -= 1
                j += 1
            if depth == 0:
                spans.append((i, j))
                i = j
                continue
        i += 1
    return spans


def remove_field(code: str, field: str) -> CodemodResult:
    """Remove references to `field`, or refuse per-reference with a reason."""
    if not field:
        return CodemodResult(code, False, refusals=["no field name given"])

    access = re.compile(rf"\.\s*{re.escape(field)}\b")
    if not access.search(code):
        return CodemodResult(code, False, refusals=[])   # nothing to do, not a refusal

    edits, refusals = [], []
    out = code

    # 1. Object-literal property whose VALUE is the member expression. Matched as a
    #    whole line on purpose: a property occupying its own line can be deleted
    #    entirely, which is what makes this shape safe. A property sharing a line
    #    with others is refused rather than sliced.
    prop = re.compile(
        rf"^[ \t]*[A-Za-z_$][\w$]*[ \t]*:[ \t]*[A-Za-z_$][\w$.]*\.{re.escape(field)}"
        rf"[ \t]*,?[ \t]*$\n?",
        re.MULTILINE)
    for m in list(prop.finditer(out)):
        edits.append({"shape": "object-literal property",
                      "removed": m.group(0).strip()})
    out = prop.sub("", out)

    # 2. Template interpolation whose entire contents are the member expression.
    #    Recomputed on the current text, and applied right-to-left so earlier spans
    #    keep their offsets.
    inner_only = re.compile(rf"^\s*[A-Za-z_$][\w$.]*\.{re.escape(field)}\s*$")
    for start, end in reversed(_interpolation_spans(out)):
        inner = out[start + 2:end - 1]
        if not inner_only.match(inner):
            continue
        # Absorb ONE adjacent space so `<a> ${x}` does not become `<a> `.
        cut_start = start
        if cut_start > 0 and out[cut_start - 1] == " ":
            cut_start -= 1
        edits.append({"shape": "template interpolation",
                      "removed": out[start:end]})
        out = out[:cut_start] + out[end:]

    # 3. Anything still referencing the field is a shape this codemod will not touch.
    for m in access.finditer(out):
        line_no = out[:m.start()].count("\n") + 1
        line = out.split("\n")[line_no - 1].strip()
        refusals.append(
            f"line {line_no}: `{line[:80]}` -- the value is used, so removing the "
            f"reference would change behaviour. A human must decide what this code "
            f"should do without the field.")

    return CodemodResult(out, out != code, edits, refusals)
