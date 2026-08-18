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

    # DETECTION IS BY WORD BOUNDARY, NOT BY MEMBER ACCESS.
    #
    # The first version searched only for `.field`. Measured against a REAL
    # repository in Stage 7 -- the billing-api demo consumer -- it returned
    # changed=False, edits=0, REFUSALS=0 despite the file containing four
    # references: two interface property declarations, a function parameter, and a
    # shorthand object property. "Nothing to do" and "four things I cannot do" are
    # different answers, and reporting the first for the second is the silent-gap
    # defect this project keeps rediscovering.
    anywhere = re.compile(rf"\b{re.escape(field)}\b")
    if not anywhere.search(code):
        return CodemodResult(code, False, refusals=[])   # genuinely nothing to do

    edits, refusals = [], []
    out = code

    # 1. Type / interface property DECLARATION on its own line:
    #        phoneNumber: string;      phoneNumber?: string;
    #    Safe: the upstream field is gone, so a mirror declaration of it is dead.
    #    Runs BEFORE the object-literal rule, and cannot collide with it because the
    #    field must be the KEY here and the value there.
    decl = re.compile(
        rf"^[ \t]*{re.escape(field)}\??[ \t]*:[ \t]*[^=\n]*?[;,]?[ \t]*$\n?",
        re.MULTILINE)
    for m in list(decl.finditer(out)):
        edits.append({"shape": "type property declaration",
                      "removed": m.group(0).strip()})
    out = decl.sub("", out)

    # 2. Object-literal property whose VALUE is the member expression. Matched as a
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

    # 3. Template interpolation whose entire contents are the member expression.
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

    # 4. Anything STILL referencing the field is a shape this codemod will not
    #    touch. Named individually, because "could not finish" is not a reason.
    #    Comments count: a stale comment is not a compile error, so it is reported
    #    rather than silently edited.
    for m in anywhere.finditer(out):
        line_no = out[:m.start()].count("\n") + 1
        line = out.split("\n")[line_no - 1].strip()
        refusals.append(
            f"line {line_no}: `{line[:80]}` -- not a shape this transformation can "
            f"remove safely. Removing a function parameter breaks every caller; "
            f"removing a shorthand property or a used value changes behaviour. A "
            f"human must decide.")

    return CodemodResult(out, out != code, edits, refusals)
