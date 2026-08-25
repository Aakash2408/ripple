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


#: Moved to app/codemod_result.py when the Python codemod needed the same contract.
#: Re-exported here because this module's public surface already included it.
from .codemod_result import CodemodResult  # noqa: E402  (kept next to its users)


def _regions(code: str) -> list:
    """Comment and string spans, delegating to the shared scanner.

    The implementation moved to app/source_regions.py when Python was added: keeping
    a Python scanner in a module named ts_codemod would have made the name a lie,
    and app/diff_contract.py was already importing this private function across the
    boundary. Kept as a thin alias because this module calls it in one place and the
    indirection is cheaper than churning that call site.
    """
    from .source_regions import regions as _shared
    return _shared(code, "typescript")


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


# ---------------------------------------------------------------------------
# TYPE REMOVED
# ---------------------------------------------------------------------------

def _type_refusal_reason(line: str, type_name: str) -> str:
    """Why this particular reference cannot be repaired mechanically.

    One specific reason per shape, so the PR body tells a reviewer what decision
    is being asked of them rather than "a human must decide".
    """
    esc = re.escape(type_name)
    s = line.strip()
    if re.search(rf"\b(?:extends|implements)\s+[\w.,<>\s]*\b{esc}\b", s):
        return ("a base class or implemented interface -- the subtype's entire "
                "contract depends on it, so removing the inheritance is a redesign")
    if re.search(rf"\bnew\s+{esc}\b", s):
        return ("a construction -- there is no value to substitute for the object "
                "it was building")
    if re.search(rf"\binstanceof\s+{esc}\b", s):
        return ("a runtime type test -- deleting it silently changes which branch "
                "runs, which is a behavioural decision")
    if re.search(rf"^\s*(?:export\s+)?(?:async\s+)?function\b.*\b{esc}\b", s) \
            or re.search(rf"\)\s*:\s*[\w<>\[\]|\s]*\b{esc}\b", s):
        return ("part of a function signature -- deleting the function, changing "
                "the parameter, or inlining the fields are three different "
                "decisions")
    if re.search(rf":\s*[\w<>\[\]|\s]*\b{esc}\b", s):
        return ("a type annotation on live code -- removing the annotation would "
                "hide the break rather than fix it")
    if re.search(rf"\bas\s+{esc}\b", s):
        return ("a type assertion -- dropping it changes what the compiler is "
                "allowed to infer about live code")
    if re.search(rf"\b{esc}\s+as\s+\w+", s):
        return ("an aliased import whose local name is used elsewhere; dropping it "
                "would orphan every use of the alias")
    return ("referenced in a way this codemod cannot repair mechanically. A "
            "deleted type has no substitute value, so refusing is the only safe "
            "answer")


def _rewrite_binding_list(raw: str, esc: str) -> tuple:
    """Split a `{ A, B as C }` binding list and drop the exact name.

    Returns (kept, dropped). An aliased binding (`User as U`) is NOT dropped: the
    local name is `U` and something else uses it, so removing the import would
    orphan those uses. It stays, and the classification pass refuses it.
    """
    kept, dropped = [], []
    for part in raw.split(","):
        p = part.strip()
        if not p:
            continue
        # `type User` inside a mixed `import { type User, Address }` list.
        if re.fullmatch(rf"(?:type\s+)?{esc}", p):
            dropped.append(p)
        else:
            kept.append(p)
    return kept, dropped


def remove_type(code: str, type_name: str) -> CodemodResult:
    """Remove references to a deleted TYPE from TypeScript or JavaScript, or refuse.

    WHY THIS REPLACES THE REGEX
    Two separate defects, in two languages, from one shared table.

    TypeScript went through `_TYPE_REF_PATTERNS['typescript']`, whose first entry is

        r'^\\s*import\\s+.*\\b{name}\\b.*$'

    which deletes the ENTIRE import statement when the removed name appears anywhere
    on it -- the identical bug already fixed in py_codemod. Measured:

        import { User, Address } from './models';   ->  (line deleted)
        export function label(a: Address) { ... }   ->  UNCHANGED

    so `Address` became undefined while it reported "Removed references to deleted
    type 'User' (1 lines affected)". `tsc` would eventually catch the fallout, but
    the fix path still returns a broken file and calls it a success.

    JavaScript was worse: it has NO entry in that table at all, so it fell through
    to `_generic_remove`, which deletes every line matching any CASE VARIANT of the
    name. `name_variants("User")` yields snake `user` and camel `user` -- the
    conventional variable name for a User -- so it deleted essentially every line of
    a real consumer. Measured, a two-function module was reduced to `}`:

        export function fmt(user) { return user.email; }   ->   }
        claim: "Removed references to deleted type 'User' (2 lines affected)."

    And JavaScript has no wired validator, so nothing downstream caught it.

    WHAT IS ACTUALLY MECHANICAL WHEN A TYPE IS DELETED
    Almost nothing, and that is the honest answer rather than a limitation -- the
    same conclusion py_codemod.remove_type reached. A consumer with
    `function label(u: User): string` cannot be repaired by any transformation:
    deleting the function, changing the signature, and inlining the fields are three
    different decisions with different consequences.

    The ONE safe edit is removing the name from an import (or `require`) binding list
    while leaving its neighbours alone, and deleting the statement only when nothing
    is left to import. That is a complete fix exactly when the type was imported but
    never used -- a stale import -- and the only case where nothing else must change.

    Everything else REFUSES, so `complete` is False and the fix path returns the
    original code.

    ONE SHARED IMPLEMENTATION FOR BOTH LANGUAGES
    TS and JS already share `remove_field` here, because the shapes that matter are
    the shapes they have in common. Type-only syntax (`import type`, `: User`) simply
    never appears in a `.js` file, so the TS-specific handling is inert there rather
    than wrong. The one JS-only shape is CommonJS `require`, handled below.
    """
    if not type_name:
        return CodemodResult(code, False, refusals=["no type name given"])

    anywhere = re.compile(rf"\b{re.escape(type_name)}\b")
    if not anywhere.search(code):
        return CodemodResult(code, False)

    edits, refusals, notes = [], [], []
    out = code
    esc = re.escape(type_name)
    DELETE = "\x00DELETE\x00"

    # 1. Single-line ES import with a braced binding list, optionally preceded by a
    #    default binding:
    #        import { User, Address } from './m';
    #        import type { User } from './m';
    #        import Default, { User } from './m';
    named = re.compile(
        rf"^(?P<indent>[ \t]*)import[ \t]+(?P<type>type[ \t]+)?"
        rf"(?P<default>[A-Za-z_$][\w$]*[ \t]*,[ \t]*)?"
        rf"\{{(?P<names>[^{{}}\n]*)\}}[ \t]*from[ \t]*(?P<src>['\"][^'\"\n]+['\"])"
        rf"(?P<semi>;?)[ \t]*$",
        re.MULTILINE)

    def _rewrite_named(m):
        kept, dropped = _rewrite_binding_list(m.group("names"), esc)
        if not dropped:
            return m.group(0)
        default = (m.group("default") or "").strip().rstrip(",").strip()
        if not kept and not default:
            edits.append({"shape": "import statement (last binding removed)",
                          "removed": m.group(0).strip()})
            return DELETE
        edits.append({"shape": "name in import list",
                      "removed": f"{type_name} from `{m.group(0).strip()}`"})
        ty = m.group("type") or ""
        head = f"{default}, " if default else ""
        if not kept:
            # Only the default binding survives: `import D from './m';`
            return (f"{m.group('indent')}import {ty}{default} from "
                    f"{m.group('src')}{m.group('semi')}")
        return (f"{m.group('indent')}import {ty}{head}{{ {', '.join(kept)} }} from "
                f"{m.group('src')}{m.group('semi')}")

    out = named.sub(_rewrite_named, out)

    # 2. Single-line CommonJS destructured require:
    #        const { User, Address } = require('./m');
    cjs = re.compile(
        rf"^(?P<indent>[ \t]*)(?P<kw>const|let|var)[ \t]+"
        rf"\{{(?P<names>[^{{}}\n]*)\}}[ \t]*=[ \t]*(?P<call>require\([^)\n]*\))"
        rf"(?P<semi>;?)[ \t]*$",
        re.MULTILINE)

    def _rewrite_cjs(m):
        kept, dropped = _rewrite_binding_list(m.group("names"), esc)
        if not dropped:
            return m.group(0)
        if not kept:
            edits.append({"shape": "require statement (last binding removed)",
                          "removed": m.group(0).strip()})
            return DELETE
        edits.append({"shape": "name in require binding list",
                      "removed": f"{type_name} from `{m.group(0).strip()}`"})
        return (f"{m.group('indent')}{m.group('kw')} {{ {', '.join(kept)} }} = "
                f"{m.group('call')}{m.group('semi')}")

    out = cjs.sub(_rewrite_cjs, out)

    # 3. A sole default import or require of exactly this name:
    #        import User from './m';        const User = require('./m');
    #    Safe only because there is no binding list to preserve.
    sole = re.compile(
        rf"^[ \t]*(?:import[ \t]+(?:type[ \t]+)?{esc}[ \t]+from[ \t]*['\"][^'\"\n]+['\"];?"
        rf"|(?:const|let|var)[ \t]+{esc}[ \t]*=[ \t]*require\([^)\n]*\);?)[ \t]*$",
        re.MULTILINE)
    for m in list(sole.finditer(out)):
        edits.append({"shape": "sole import of the deleted type",
                      "removed": m.group(0).strip()})
    out = sole.sub(DELETE, out)

    # 4. Multi-line braced import/require with one binding per line -- remove just
    #    that line. Only attempted when the name genuinely sits alone on its line,
    #    so a nested or exotic form falls through to the refusal pass instead of
    #    being guessed at.
    per_line = re.compile(rf"^[ \t]*(?:type[ \t]+)?{esc}[ \t]*,?[ \t]*$\n?", re.MULTILINE)
    if re.search(r"(?:import|require\()\s*\{[^}]*\n", out) or re.search(r"\{\s*\n", out):
        for m in list(per_line.finditer(out)):
            edits.append({"shape": "name on its own line in a multi-line import",
                          "removed": m.group(0).strip()})
        out = per_line.sub("", out)

    # Drop the marked whole-statement deletions, taking their newline with them.
    out = re.sub(rf"{re.escape(DELETE)}\n?", "", out)

    # 5. Classify what is left. Every remaining reference in CODE refuses: a type is
    #    not a value, so there is no equivalent of dropping an interpolation or an
    #    object-literal property the way remove_field can. A mention in a comment or
    #    a string is a NOTE -- it cannot break a build, and blocking the fix over it
    #    would block nearly every real consumer.
    regions = _regions(out)
    lines = out.split("\n")
    seen = set()
    for m in anywhere.finditer(out):
        line_no = out[:m.start()].count("\n") + 1
        line = lines[line_no - 1] if line_no - 1 < len(lines) else ""
        kind = _kind_at(m.start(), regions)
        key = (line_no, kind)
        if key in seen:
            continue
        seen.add(key)
        if kind == "comment":
            notes.append(f"line {line_no}: mentioned in a comment -- left as is, "
                         f"but it is now stale: `{line.strip()[:70]}`")
        elif kind == "string":
            notes.append(f"line {line_no}: appears in a string literal -- left as "
                         f"is, since editing it could change behaviour: "
                         f"`{line.strip()[:70]}`")
        else:
            refusals.append(
                f"line {line_no}: `{line.strip()[:80]}` -- "
                f"{_type_refusal_reason(line, type_name)}")

    return CodemodResult(out, out != code, edits, refusals, notes)
