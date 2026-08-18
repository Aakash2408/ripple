"""Remove references to a deleted field from TypeScript, or refuse.

WHY THIS REPLACES THE REGEX
The previous `_remove_field_typescript` applied context-free substitutions to
context-sensitive syntax. Measured against the golden fixture in Stage 4 it produced:

    -    phone: user.phoneNumber,
    -  };
    +    phone: user.};

...which does not parse, while reporting "Removed all references to field
'phoneNumber' (1 lines affected)". A wider regex cannot fix this: removing
`user.phoneNumber` from an expression requires knowing what the expression IS.

THREE OUTCOMES PER REFERENCE, NOT TWO
The first version had only "handled" and "refused", and that conflated two very
different things. Measured against the twelve adversarial shapes, seven were
refused -- but only three of those were genuine judgment calls. The other four were
either an oversight (optional chaining) or, worse, *benign*:

    console.log("phoneNumber");     a string that merely mentions the name
    // phoneNumber is deprecated    a comment

Neither is a compile error and neither should be edited. But refusing them set
`complete = False`, so the whole file became unfixable and no PR opened. Nearly
every real consumer has a log line or a comment naming the field it uses, which is
why the one real repository tested in Stage 7 came back BLOCKED. A safety rule that
blocks the safe cases is not conservative, it is broken.

So references are now classified three ways:

    EDIT      a shape that can be removed with no behavioural change
    REFUSAL   a shape whose removal requires a human decision  -> blocks the fix
    NOTE      a mention in a comment or string literal          -> reported only

`complete` ignores notes. They travel to the PR body so a reviewer can see the
stale comment, which is more useful than silently rewriting their prose.

WHAT COUNTS AS AN EDITABLE SHAPE
    template interpolation    `${user.phoneNumber}`      delete the whole ${...}
    object-literal property   `phone: user.phoneNumber,` delete the whole property
    type property declaration `phoneNumber: string;`     delete the whole line

All three are safe because the value has no remaining effect: the field is gone
upstream, so sending it, printing it, or mirroring its type is dead. Member chains
may use optional chaining (`user?.phoneNumber`) and may be nested
(`response.user.phoneNumber`) -- `?.` changes nothing about whether the reference is
removable, and treating it as unhandled was simply an oversight.

WHAT IS STILL REFUSED, CORRECTLY
    const { name, phoneNumber } = user;      destructuring
    function send(phoneNumber: string) {}    parameter -- breaks every caller
    const phone = user.phoneNumber;          aliased, then used

Removing any of these forces a behavioural decision no transformation can make. A
transformation that abstains when unsure, paired with a validator that catches it
when wrong anyway, is what makes the cell safe.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as _field

#: A member chain ending in `.field`, allowing optional chaining and nesting:
#: `user.f`, `user?.f`, `response.user.f`, `a?.b?.f`
_CHAIN = r"[A-Za-z_$][\w$]*(?:\s*\??\.\s*[A-Za-z_$][\w$]*)*\s*\??\."


def _inside_jsx_tag(code: str, pos: int, limit: int = 4000) -> bool:
    """Is `pos` inside a JSX opening tag's attribute list?

    Walks BACKWARDS from pos to find the `<Tag` that opens the element.

    THE PART THAT IS NOT OBVIOUS: it must SKIP BALANCED GROUPS. The first version
    treated `}` as "the tag already closed", which broke both of the shapes that
    matter most:

        <Row a={x} phone={user.phoneNumber} />       the `}` of a={x}
        <Row                                        the `}` of name={...}
          name={user.fullName}
          phone={user.phoneNumber}

    Both were refused. So on a closing delimiter we jump to its opener and carry
    on, which also skips past arrow functions in sibling attributes -- `onClick={()
    => f()}` contains `>`, and reading that as the end of the tag would be wrong.
    Quoted attribute values are skipped for the same reason: `title="a>b"`.

    An UNMATCHED opener going backwards means we are inside an expression rather
    than an attribute list, so that still stops the scan.
    """
    pairs = {"}": "{", ")": "(", "]": "["}
    i = pos - 1
    stop = max(0, pos - limit)
    while i >= stop:
        ch = code[i]

        if ch in "\"'`":
            # Skip a quoted run backwards to its opening quote.
            j = i - 1
            while j >= stop and not (code[j] == ch and (j == 0 or code[j - 1] != "\\")):
                j -= 1
            i = j - 1
            continue

        if ch in pairs:
            opener, depth, j = pairs[ch], 1, i - 1
            while j >= stop and depth:
                if code[j] == ch:
                    depth += 1
                elif code[j] == opener:
                    depth -= 1
                j -= 1
            if depth:
                return False             # unbalanced -- give up rather than guess
            i = j
            continue

        if ch == "<":
            nxt = code[i + 1] if i + 1 < len(code) else ""
            return bool(nxt) and (nxt.isalpha() or nxt in "_$")

        if ch in ">;{(,":
            return False

        i -= 1
    return False


@dataclass
class CodemodResult:
    code: str
    changed: bool
    edits: list = _field(default_factory=list)
    refusals: list = _field(default_factory=list)
    notes: list = _field(default_factory=list)

    @property
    def complete(self) -> bool:
        """Every reference that MATTERS was handled.

        Notes are excluded deliberately: a comment or string mentioning the field
        is not a compile error, so letting it veto the fix would block almost every
        real consumer. A partial removal, by contrast, still leaves a type error, so
        "some edits" is not success -- it is a different failure.
        """
        return self.changed and not self.refusals


def _regions(code: str) -> list:
    """Spans of comment and string CONTENT, as (start, end, kind).

    Template literals are split: the literal text is "string", but the contents of
    each `${...}` are real code and are NOT included. Getting this wrong in either
    direction is a bug -- treating `${user.phoneNumber}` as string content would
    stop the fix working, and treating a comment as code would delete prose.
    """
    out, i, n = [], 0, len(code)
    while i < n:
        ch = code[i]
        nxt = code[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            j = code.find("\n", i)
            j = n if j == -1 else j
            out.append((i, j, "comment"))
            i = j
        elif ch == "/" and nxt == "*":
            j = code.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out.append((i, j, "comment"))
            i = j
        elif ch in "'\"":
            j, quote = i + 1, ch
            while j < n:
                if code[j] == "\\":
                    j += 2
                    continue
                if code[j] == quote or code[j] == "\n":
                    break
                j += 1
            out.append((i, min(j + 1, n), "string"))
            i = min(j + 1, n)
        elif ch == "`":
            # Walk the template, emitting text runs and SKIPPING ${...} contents.
            j, run_start = i + 1, i + 1
            while j < n:
                if code[j] == "\\":
                    j += 2
                    continue
                if code[j] == "`":
                    break
                if code[j] == "$" and j + 1 < n and code[j + 1] == "{":
                    out.append((run_start, j, "string"))
                    depth, k = 1, j + 2
                    while k < n and depth:
                        if code[k] == "{":
                            depth += 1
                        elif code[k] == "}":
                            depth -= 1
                        k += 1
                    j = k
                    run_start = k
                    continue
                j += 1
            out.append((run_start, min(j, n), "string"))
            i = min(j + 1, n)
        else:
            i += 1
    return out


def _kind_at(pos: int, regions: list) -> str:
    for start, end, kind in regions:
        if start <= pos < end:
            return kind
    return "code"


def _interpolation_spans(code: str) -> list:
    """(start, end) of every `${...}` inside a template literal, brace-aware."""
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
    """Remove references to `field`; refuse or note the rest."""
    if not field:
        return CodemodResult(code, False, refusals=["no field name given"])

    # Word boundary, not member access. Searching only for `.field` made four real
    # references invisible in Stage 7 -- "nothing to do" reported for "four things I
    # cannot do". `\b` also means `phoneNumberFormatter` is correctly not a match.
    anywhere = re.compile(rf"\b{re.escape(field)}\b")
    if not anywhere.search(code):
        return CodemodResult(code, False)

    edits, refusals, notes = [], [], []
    out = code
    esc = re.escape(field)

    # 1. A property whose KEY is the field, on its own line -- either a type
    #    declaration (`phoneNumber: string;`) or an inert object-literal entry
    #    (`phoneNumber: "555",`).
    #
    #    SIDE-EFFECTING VALUES ARE REFUSED. The first version removed
    #    `phoneNumber: getPhone(),` and `phoneNumber: await fetchPhone(),` outright,
    #    deleting a call. Nothing would have caught it: the compiler is happy, and
    #    the diff contract is satisfied because the deleted line DOES reference the
    #    field. Whether dropping that call is correct depends on what it does, which
    #    makes it a judgment call, not a removal.
    decl = re.compile(rf"^[ \t]*{esc}\??[ \t]*:[ \t]*(?P<value>[^=\n]*?)[;,]?[ \t]*$\n?",
                      re.MULTILINE)
    SIDE_EFFECTS = ("(", "await ", "=>", "new ", "++", "--", "yield ")
    keep = []
    # A reference must produce exactly ONE reason. Without this, a side-effecting
    # value was refused here AND again by the final classification pass, so the PR
    # body would say the same thing twice and the counts would double.
    _refused_lines: set = set()
    for m in list(decl.finditer(out)):
        value = m.group("value")
        marker = next((t for t in SIDE_EFFECTS if t in value), None)
        if marker:
            line_no = out[:m.start()].count("\n") + 1
            refusals.append(
                f"line {line_no}: `{m.group(0).strip()[:70]}` -- the value contains "
                f"`{marker.strip()}`, so removing the property would also remove "
                f"something that may have effects. Whether that is correct depends "
                f"on what it does. A human must decide.")
            keep.append(m.span())
            _refused_lines.add(m.group(0).strip())
        else:
            edits.append({"shape": "keyed property (inert value)",
                          "removed": m.group(0).strip()})
    # Rebuild without the removable matches, preserving the refused ones.
    if any(m.span() not in keep for m in decl.finditer(out)):
        pieces, last = [], 0
        for m in decl.finditer(out):
            if m.span() in keep:
                continue
            pieces.append(out[last:m.start()])
            last = m.end()
        pieces.append(out[last:])
        out = "".join(pieces)

    # 2. Object-literal property whose VALUE is a member chain ending in the field.
    prop = re.compile(
        rf"^[ \t]*[A-Za-z_$][\w$]*[ \t]*:[ \t]*{_CHAIN}{esc}[ \t]*,?[ \t]*$\n?",
        re.MULTILINE)
    for m in list(prop.finditer(out)):
        edits.append({"shape": "object-literal property",
                      "removed": m.group(0).strip()})
    out = prop.sub("", out)

    # 3. Template interpolation whose entire contents are the member chain.
    inner_only = re.compile(rf"^\s*{_CHAIN}{esc}\s*$")
    for start, end in reversed(_interpolation_spans(out)):
        if not inner_only.match(out[start + 2:end - 1]):
            continue
        cut = start - 1 if start > 0 and out[start - 1] == " " else start
        edits.append({"shape": "template interpolation",
                      "removed": out[start:end]})
        out = out[:cut] + out[end:]

    # 4. JSX attribute whose value is exactly the member chain:
    #    `<Row phone={user.phoneNumber} />` -> `<Row />`
    #
    #    WHY THIS IS SAFE TO PATTERN-MATCH WITHOUT A JSX PARSER -- TWO GUARDS,
    #    AND MEASUREMENT SAYS WHICH ONE MATTERS
    #
    #    Guard 1, the pattern: the braces must contain EXACTLY a member chain ending
    #    in the field. That shape is valid ONLY as a JSX attribute value:
    #
    #        <Row phone={user.phoneNumber} />        JSX          <- matches
    #        { a: {user.phoneNumber} }               not valid JS
    #        function f(a = {user.phoneNumber})      not valid JS
    #
    #    Guard 2, _inside_jsx_tag(): a backward scan for the opening `<`.
    #
    #    I assumed guard 1 was the load-bearing one. It is not. Loosening the pattern
    #    to accept anything in the braces did NOT break the default-parameter case --
    #    guard 2 rejected it, because scanning back from the attribute hits `(`.
    #    Removing guard 2 and loosening guard 1 together produces real damage:
    #
    #        function f(opts={user: user.phoneNumber}) { return opts; }
    #          ->  function f() { return opts; }
    #
    #    a destroyed signature with the body still using the parameter. So guard 2 is
    #    the one that must never be deleted, and `default-parameter-object-value` in
    #    the coverage corpus fails the build if it ever is.
    #
    #    WHY EDIT AND NOT JUDGMENT
    #    Same reasoning as the object-literal property: the field no longer exists
    #    upstream, so passing it conveys nothing. If the prop is REQUIRED by the
    #    component, `tsc` reports the missing prop and the validator blocks the fix
    #    -- the compiler is the right place to decide that, not a regex.
    jsx_attr = re.compile(
        rf"(?P<lead>[ \t]*)(?P<name>[A-Za-z_$][\w$-]*)[ \t]*=[ \t]*"
        rf"\{{[ \t]*{_CHAIN}{esc}[ \t]*\}}")
    for m in reversed(list(jsx_attr.finditer(out))):
        if not _inside_jsx_tag(out, m.start()):
            continue                     # not an attribute; leave it to step 5
        line_start = out.rfind("\n", 0, m.start()) + 1
        line_end = out.find("\n", m.end())
        line_end = len(out) if line_end == -1 else line_end
        alone = (out[line_start:m.start()].strip() == ""
                 and out[m.end():line_end].strip() == "")
        if alone:
            # The attribute owns the whole line. Remove the line, or a blank line
            # is left behind and the diff stops being scannable.
            cut_start, cut_end = line_start, min(line_end + 1, len(out))
        else:
            cut_start, cut_end = m.start(), m.end()
        edits.append({"shape": "JSX attribute",
                      "removed": out[m.start():m.end()].strip()})
        out = out[:cut_start] + out[cut_end:]

    # 5. Classify what remains. A mention in a comment or a string is a NOTE, not a
    #    refusal: it cannot break a build, so blocking the fix over it would block
    #    nearly every real consumer.
    regions = _regions(out)
    for m in anywhere.finditer(out):
        line_no = out[:m.start()].count("\n") + 1
        line = out.split("\n")[line_no - 1].strip()
        if line in _refused_lines:
            continue                     # already explained by the step-1 guard
        kind = _kind_at(m.start(), regions)
        if kind == "comment":
            notes.append(f"line {line_no}: mentioned in a comment -- left as is, "
                         f"but it is now stale: `{line[:70]}`")
        elif kind == "string":
            notes.append(f"line {line_no}: appears in a string literal -- left as "
                         f"is, since editing it could change behaviour: "
                         f"`{line[:70]}`")
        else:
            refusals.append(
                f"line {line_no}: `{line[:80]}` -- not a shape this transformation "
                f"can remove safely. Removing a function parameter breaks every "
                f"caller; removing a destructured binding or an aliased value "
                f"changes behaviour. A human must decide.")

    return CodemodResult(out, out != code, edits, refusals, notes)
