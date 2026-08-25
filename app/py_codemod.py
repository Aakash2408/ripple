"""Remove references to a deleted field from Python, or refuse.

WHY THIS REPLACES THE REGEX
`_remove_field_python` did not work at all, and said it had. Measured on a realistic
consumer:

    input                                       output
    f"{user.full_name} {user.phone_number}"     UNCHANGED
    "phone": user.phone_number,                 UNCHANGED

    reported: "Removed references to field 'phone_number' (2 lines affected).
               Cleaned: struct/class declarations, accessor methods, function
               params, object literals, and direct field access patterns for python."

Every reference survived, one blank line was collapsed, and the explanation named
five categories it had not touched. A no-op that changes whitespace is worse than a
no-op that changes nothing: `changed` is True, so the pipeline believes a fix exists
and only the validator stands between that and a PR. This is the same defect shape
as the Stage 4 TypeScript baseline that forced app/ts_codemod.py to be written.

THE SAME THREE OUTCOMES AS TYPESCRIPT
Deliberately identical, sharing CodemodResult so `complete` has one definition:

    EDIT      a shape that can be removed with no behavioural change
    REFUSAL   a shape whose removal requires a human decision  -> blocks the fix
    NOTE      a mention in a comment or string literal          -> reported only

WHAT COUNTS AS AN EDITABLE SHAPE IN PYTHON
    annotated class attribute   `phone_number: str`            delete the line
    dict-literal entry          `"phone": user.phone_number,`   delete the entry
    f-string interpolation      `{user.phone_number}`           delete the {...}

All three are safe for the same reason as their TypeScript equivalents: the field is
gone upstream, so mirroring its type, sending it, or printing it is dead. A default
value with side effects is refused, exactly as in TypeScript -- deleting
`phone_number: str = fetch_phone()` removes a call, and whether that is correct
depends on what the call does.

WHAT IS REFUSED, CORRECTLY
    def send(phone_number: str):        parameter -- breaks every caller
    phone = user.phone_number           aliased, then used
    name, phone_number = row            unpacking
    send(phone_number=user.phone_number)  keyword argument -- the callee still wants it
    user["phone_number"]                subscript -- see below

THE SUBSCRIPT CASE, WHICH IS NOT A NOTE
`user["phone_number"]` puts the field name inside a string literal, so the region
scanner classifies it as a string and the naive rule would file it as a NOTE --
reported but not blocking. It is a real attribute access that will raise KeyError at
runtime, and Python has no compiler to catch it, so a NOTE here would let a fix ship
with a live break in it. A string literal immediately preceded by `[` is therefore a
REFUSAL, which is strictly safer than the TypeScript module's treatment of the same
shape.

WHY NOT ast
`ast.parse` plus `ast.unparse` would be syntax-aware and simpler, and it reformats
the entire file -- every quote style, every blank line, every line break normalised.
That destroys the minimal diff the fixtures assert and would make a one-field change
unreviewable. `end_lineno` (Python 3.8) is also absent on the 3.7 interpreter this
repo still runs locally. So this stays text-surgical with a region scanner, matching
ts_codemod.
"""

from __future__ import annotations

import re

from .codemod_result import CodemodResult

#: A member chain ending in `.`, so `_CHAIN + field` matches `user.phone_number`
#: and `response.user.phone_number`. No optional-chaining operator in Python.
_CHAIN = r"[A-Za-z_][\w]*(?:\s*\.\s*[A-Za-z_][\w]*)*\s*\."

#: Tokens whose presence in a default value means removing the line would remove
#: something that may have effects. Same list as TypeScript minus its JS-isms.
_SIDE_EFFECTS = ("(", "await ", "lambda", "yield ", ":=")


def _regions(code: str) -> list:
    from .source_regions import regions as _shared
    return _shared(code, "python")


def _kind_at(pos: int, regions: list) -> str:
    for start, end, kind in regions:
        if start <= pos < end:
            return kind
    return "code"


def _fstring_interpolation_spans(code: str) -> list:
    """(start, end) of every `{...}` inside an f-string, brace-aware.

    `end` is exclusive of the closing brace's successor, i.e. code[start:end] is the
    whole `{...}` including both braces. `{{` and `}}` are literal braces and are
    skipped -- treating `{{` as an interpolation opener would mis-span every dict
    printed inside an f-string.
    """
    spans = []
    n = len(code)
    i = 0
    while i < n:
        # find an f-string prefix
        if code[i] in "fF" or (code[i] in "rRbB" and i + 1 < n and code[i + 1] in "fF"):
            j = i
            while j < n and code[j] in "fFrRbBuU":
                j += 1
            if j < n and code[j] in "'\"":
                quote = code[j]
                triple = code[j:j + 3] in ("'''", '"""')
                closing = quote * 3 if triple else quote
                k = j + (3 if triple else 1)
                while k < n:
                    if code[k] == "\\":
                        k += 2
                        continue
                    if code.startswith(closing, k):
                        break
                    if code[k] == "{":
                        if code.startswith("{{", k):
                            k += 2
                            continue
                        depth, m = 1, k + 1
                        while m < n and depth:
                            if code[m] == "{":
                                depth += 1
                            elif code[m] == "}":
                                depth -= 1
                            m += 1
                        if depth == 0:
                            spans.append((k, m))
                            k = m
                            continue
                    k += 1
                i = k
                continue
        i += 1
    return spans


def _interpolation_expression(inner: str) -> str:
    """The expression part of an f-string replacement field.

    `{u.phone_number!r}` and `{u.phone_number:>10}` are the same reference as
    `{u.phone_number}`; the conversion and format spec are not part of it. Splitting
    on the first `!` or `:` at depth zero is enough -- a format spec cannot contain an
    unbracketed one, and a walrus is refused elsewhere.
    """
    depth = 0
    for idx, ch in enumerate(inner):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0 and ch in "!:":
            # `!=` is an operator, not a conversion
            if ch == "!" and idx + 1 < len(inner) and inner[idx + 1] == "=":
                continue
            return inner[:idx]
    return inner


def _refusal_reason(line: str, field: str) -> str:
    """The most specific true statement about why this reference cannot be removed."""
    stripped = line.strip()
    esc = re.escape(field)
    if re.search(rf"^\s*(async\s+)?def\s+\w+\s*\([^)]*\b{esc}\b", line):
        return ("a function parameter -- removing it changes the signature and "
                "breaks every caller")
    if re.search(rf"\b{esc}\s*=[^=]", stripped) and not re.search(rf"\.\s*{esc}\b", stripped):
        return ("either a keyword argument or an assignment target. Removing a "
                "keyword argument changes a call the callee still expects; removing "
                "an assignment orphans whatever reads the name")
    if re.search(rf"^\s*[\w\s,()\[\]]*\b{esc}\b[\w\s,()\[\]]*=[^=]", stripped):
        return "an unpacking target -- the arity of the assignment would change"
    if re.search(rf"=\s*{_CHAIN}{esc}\b", stripped):
        return ("aliased into a local name that is then used elsewhere. Removing the "
                "read requires knowing what the alias feeds")
    return ("referenced in an expression whose surrounding syntax this codemod does "
            "not recognise. Refusing rather than guessing at the boundaries")


def remove_type(code: str, type_name: str) -> CodemodResult:
    """Remove references to a deleted TYPE from Python, or refuse.

    WHY THIS REPLACES THE REGEX
    The shared `_TYPE_REF_PATTERNS['python']` was not merely useless like the old
    remove_field, it was destructive. Its first pattern:

        r'^\\s*from\\s+\\S+\\s+import\\s+.*\\b{name}\\b.*$'

    deletes the ENTIRE import statement when the removed name appears anywhere on it.
    Measured:

        from src.models import User, Address     ->  (line deleted)
        def f(u: User) -> str: ...               ->  UNCHANGED

    So `Address` -- still used two functions below -- became undefined, every `User`
    reference survived, and it reported "Removed references to deleted type 'User'
    (1 lines affected): imports, declarations, type annotations and constructions".
    The output does not import-clean, let alone run.

    WHAT IS ACTUALLY MECHANICAL WHEN A TYPE IS DELETED
    Almost nothing, and that is the honest answer rather than a limitation. A
    consumer with `def format_contact(user: User) -> str` cannot be repaired by any
    transformation: deleting the function, changing its signature, or inlining the
    fields are three different decisions with different consequences, and choosing
    among them is what a human is for.

    The ONE safe edit is removing the name from an import list while leaving its
    neighbours alone -- and deleting the statement only when the list becomes empty.
    That is a complete fix exactly when the type was imported but never used, which
    is a real case (a stale import) and the only one where nothing else must change.

    Everything else REFUSES, so `complete` is False and the fix path returns the
    original code. A caller that wanted breadth here would be asking for the
    behaviour that just got removed.
    """
    if not type_name:
        return CodemodResult(code, False, refusals=["no type name given"])

    anywhere = re.compile(rf"\b{re.escape(type_name)}\b")
    if not anywhere.search(code):
        return CodemodResult(code, False)

    edits, refusals, notes = [], [], []
    out = code
    esc = re.escape(type_name)

    # 1. `from X import A, B` on one line, optionally parenthesised. Remove ONLY the
    #    name; keep every neighbour. Delete the statement only if nothing is left.
    single = re.compile(
        rf"^(?P<indent>[ \t]*)from[ \t]+(?P<mod>[\w.]+)[ \t]+import[ \t]+"
        rf"(?P<open>\()?(?P<names>[^()\n]+)(?P<close>\))?[ \t]*$",
        re.MULTILINE)

    def _rewrite_import(m):
        raw = m.group("names")
        parts = [p.strip() for p in raw.split(",")]
        kept, dropped = [], []
        for p in parts:
            if not p:
                continue
            # `User as U` is an alias whose local name is used elsewhere; removing the
            # import would orphan it, so leave the statement alone and let the final
            # pass refuse.
            if re.fullmatch(rf"{esc}", p):
                dropped.append(p)
            else:
                kept.append(p)
        if not dropped:
            return m.group(0)
        if not kept:
            edits.append({"shape": "import statement (only name removed)",
                          "removed": m.group(0).strip()})
            return "\x00DELETE\x00"
        edits.append({"shape": "name in import list",
                      "removed": f"{type_name} from `{m.group(0).strip()}`"})
        o, c = m.group("open") or "", m.group("close") or ""
        return f"{m.group('indent')}from {m.group('mod')} import {o}{', '.join(kept)}{c}"

    out = single.sub(_rewrite_import, out)
    # Drop the marked whole-statement deletions, taking their newline with them.
    out = re.sub(r"\x00DELETE\x00\n?", "", out)

    # 2. Parenthesised multi-line import with one name per line -- remove just that
    #    line. Anything more exotic falls through to the refusal pass rather than
    #    being guessed at.
    per_line = re.compile(rf"^[ \t]*{esc}[ \t]*,?[ \t]*$\n?", re.MULTILINE)
    inside_parens = "import (" in out or "import(" in out
    if inside_parens:
        for m in list(per_line.finditer(out)):
            edits.append({"shape": "name on its own line in a parenthesised import",
                          "removed": m.group(0).strip()})
        out = per_line.sub("", out)

    # 3. Classify what is left. Every remaining reference in code REFUSES: a type is
    #    not a value, so there is no equivalent of dropping an interpolation or a dict
    #    entry -- whatever mentions it needs a decision.
    regions = _regions(out)
    seen = set()
    for m in anywhere.finditer(out):
        kind = _kind_at(m.start(), regions)
        line_start = out.rfind("\n", 0, m.start()) + 1
        line_end = out.find("\n", m.start())
        line = out[line_start:line_end if line_end != -1 else len(out)]
        line_no = out[:m.start()].count("\n") + 1
        key = (line_no, kind)
        if key in seen:
            continue
        seen.add(key)
        if kind in ("comment", "string"):
            notes.append(f"line {line_no} ({kind}): {line.strip()[:70]}")
            continue
        refusals.append(
            f"line {line_no}: `{line.strip()[:70]}` -- {_type_refusal_reason(line, type_name)}")

    return CodemodResult(out, out != code, edits, refusals, notes)


def _type_refusal_reason(line: str, type_name: str) -> str:
    esc = re.escape(type_name)
    stripped = line.strip()
    if re.search(rf"^\s*class\s+\w+\s*\([^)]*\b{esc}\b", line):
        return ("a base class -- the subclass's entire contract depends on it, so "
                "removing the inheritance is a redesign")
    if re.search(rf"^\s*(async\s+)?def\s+.*\b{esc}\b", line):
        return ("part of a function signature -- deleting the function, changing the "
                "parameter, or inlining the fields are three different decisions")
    if re.search(rf"\b{esc}\s*\(", stripped):
        return ("a construction -- there is no value to substitute for the object it "
                "was building")
    if re.search(rf":\s*{esc}\b", stripped) or re.search(rf"->\s*{esc}\b", stripped):
        return ("a type annotation on live code -- removing the annotation would hide "
                "the break rather than fix it")
    if re.search(rf"\bas\s+\w+", stripped) and re.search(rf"\b{esc}\b", stripped):
        return ("an aliased import whose local name is used elsewhere; dropping it "
                "would orphan every use of the alias")
    return ("referenced in a way this codemod cannot repair mechanically. A deleted "
            "type has no substitute value, so refusing is the only safe answer")


def remove_field(code: str, field: str) -> CodemodResult:
    """Remove references to `field`; refuse or note the rest."""
    if not field:
        return CodemodResult(code, False, refusals=["no field name given"])

    anywhere = re.compile(rf"\b{re.escape(field)}\b")
    if not anywhere.search(code):
        return CodemodResult(code, False)

    edits, refusals, notes = [], [], []
    out = code
    esc = re.escape(field)

    # 1. Annotated class attribute on its own line, with or without a default.
    #    SIDE-EFFECTING DEFAULTS ARE REFUSED, for the same reason as TypeScript:
    #    the type checker is happy either way and the diff contract is satisfied
    #    because the deleted line does reference the field, so nothing else would
    #    catch the deleted call.
    decl = re.compile(
        rf"^[ \t]*{esc}[ \t]*:[ \t]*(?P<ann>[^=\n]+?)"
        rf"(?:[ \t]*=[ \t]*(?P<default>[^\n]*?))?[ \t]*$\n?",
        re.MULTILINE)
    keep = set()
    # A reference must produce exactly ONE reason. Without this, a side-effecting
    # default was refused here AND again by the final classification pass, so the PR
    # body said the same thing twice with a vaguer second reason and the refusal
    # count doubled. ts_codemod hit the identical problem and solved it the same way;
    # keyed on the stripped TEXT rather than the line number because pass 1 removes
    # lines and renumbers everything after it.
    _refused_text: set = set()
    for m in list(decl.finditer(out)):
        default = m.group("default") or ""
        marker = next((t for t in _SIDE_EFFECTS if t in default), None)
        if marker:
            line_no = out[:m.start()].count("\n") + 1
            refusals.append(
                f"line {line_no}: `{m.group(0).strip()[:70]}` -- the default "
                f"contains `{marker.strip()}`, so removing the attribute would also "
                f"remove something that may have effects. A human must decide.")
            keep.add(m.span())
            _refused_text.add(m.group(0).strip())
        else:
            edits.append({"shape": "annotated class attribute",
                          "removed": m.group(0).strip()})
    if any(m.span() not in keep for m in decl.finditer(out)):
        pieces, last = [], 0
        for m in decl.finditer(out):
            if m.span() in keep:
                continue
            pieces.append(out[last:m.start()])
            last = m.end()
        pieces.append(out[last:])
        out = "".join(pieces)

    # 2. Dict-literal entry whose VALUE is a member chain ending in the field.
    entry = re.compile(
        rf"^[ \t]*(?P<key>\"[^\"]*\"|'[^']*'|[A-Za-z_]\w*)[ \t]*:[ \t]*"
        rf"{_CHAIN}{esc}[ \t]*,?[ \t]*$\n?",
        re.MULTILINE)
    for m in list(entry.finditer(out)):
        edits.append({"shape": "dict-literal entry",
                      "removed": m.group(0).strip()})
    out = entry.sub("", out)

    # 3. f-string interpolation whose entire expression is the member chain.
    #    Reversed so earlier spans keep their offsets as later ones are removed.
    inner_only = re.compile(rf"^\s*{_CHAIN}{esc}\s*$")
    for start, end in reversed(_fstring_interpolation_spans(out)):
        expr = _interpolation_expression(out[start + 1:end - 1])
        if not inner_only.match(expr):
            continue
        edits.append({"shape": "f-string interpolation",
                      "removed": out[start:end]})
        out = out[:start] + out[end:]

    # 4. Classify whatever is left. Every surviving occurrence produces exactly ONE
    #    reason -- edited shapes are already gone from `out`, so they cannot be
    #    double-counted here the way the TypeScript version's refusals once were.
    regions = _regions(out)
    seen_lines = set()
    for m in anywhere.finditer(out):
        kind = _kind_at(m.start(), regions)
        line_start = out.rfind("\n", 0, m.start()) + 1
        line_end = out.find("\n", m.start())
        line = out[line_start:line_end if line_end != -1 else len(out)]
        line_no = out[:m.start()].count("\n") + 1

        if kind in ("comment", "string"):
            # A string literal immediately preceded by `[` is a subscript, which is a
            # real access and will raise KeyError. Python has no compiler to catch
            # it, so filing it as a NOTE would let a fix ship containing a live
            # break. Strictly safer than treating every string as inert.
            before = out[:m.start()].rstrip()
            quote_before = before[-1:] in ("'", '"')
            if quote_before and before[:-1].rstrip().endswith("["):
                key = (line_no, "subscript")
                if key not in seen_lines:
                    seen_lines.add(key)
                    refusals.append(
                        f"line {line_no}: `{line.strip()[:70]}` -- a subscript "
                        f"access. Removing it requires knowing what the surrounding "
                        f"expression should become, and nothing would catch it at "
                        f"import time")
                continue
            key = (line_no, kind)
            if key not in seen_lines:
                seen_lines.add(key)
                notes.append(f"line {line_no} ({kind}): {line.strip()[:70]}")
            continue

        key = (line_no, "code")
        if line.strip() in _refused_text:
            # Pass 1 already gave this exact line a specific reason. Adding the
            # generic one would restate it worse.
            continue
        if key not in seen_lines:
            seen_lines.add(key)
            refusals.append(f"line {line_no}: `{line.strip()[:70]}` -- "
                            + _refusal_reason(line, field))

    return CodemodResult(out, out != code, edits, refusals, notes)
