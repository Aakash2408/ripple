from __future__ import annotations
"""
ripple/app/schema_parse.py

Shared, brace-aware primitives for the schema diff engines.

WHY THIS EXISTS
---------------
Every engine used a regex of the shape:

    r'message\\s+(\\w+)\\s*\\{([^}]*)\\}'

`[^}]*` cannot cross a '}'. The moment a block contains ANY nested brace --
a nested message, a `oneof`, an inner `enum`, a `map` default of `{}`, a
GraphQL default value of `{x: 1}` -- the body is truncated at that first
inner brace and every field after it becomes invisible to the parser.

The consequence is the worst possible failure mode for this product: a
SILENT FALSE NEGATIVE. Ripple reports "no breaking changes" for a schema
that just broke its consumers, and the user trusts it.

Separately, no engine stripped comments, so a commented-out field parsed as
a live field. Deleting or uncommenting a comment fabricated a breaking
change -- a FALSE POSITIVE that opens a PR for a change that never happened.

These helpers fix both classes once, so engines share one correct
implementation instead of five subtly different regexes.
"""

import re


# --------------------------------------------------------------- comments
def strip_comments(text: str, hash_comments: bool = False) -> str:
    """Remove comments while PRESERVING line numbers and string literals.

    Replaces comment characters with spaces rather than deleting them, so
    reported line numbers still line up with the user's file.

    hash_comments: also treat '#' as a line comment (proto, GraphQL,
    Thrift and Python-ish schema formats allow it; C-like ones do not).
    """
    out = []
    i = 0
    n = len(text)
    in_line_comment = False
    in_block_comment = False
    quote = ""  # active string delimiter, if any

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
                out.append(ch)
            else:
                out.append(" ")
            i += 1
            continue

        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                out.append("  ")
                i += 2
                continue
            out.append("\n" if ch == "\n" else " ")
            i += 1
            continue

        if quote:
            out.append(ch)
            # Skip escaped characters inside strings
            if ch == "\\" and nxt:
                out.append(nxt)
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue

        # Not in a comment or string
        if ch in ('"', "'"):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            in_line_comment = True
            out.append("  ")
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            out.append("  ")
            i += 2
            continue
        if hash_comments and ch == "#":
            in_line_comment = True
            out.append(" ")
            i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out)


# ----------------------------------------------------------------- blocks
def extract_blocks(text: str, keyword: str) -> list[tuple[str, str]]:
    """Extract `keyword Name { ...body... }` blocks, brace-aware.

    Returns [(name, body), ...] where body is the FULL balanced body,
    including any nested blocks. This is what `[^}]*` could not do.

    Only top-level occurrences of the keyword are returned; nested blocks
    remain inside the parent's body (use extract_blocks again on a body to
    walk into them).
    """
    results = []
    # `keyword Name` optionally followed by other tokens before '{'
    pattern = re.compile(
        r'\b' + re.escape(keyword) + r'\s+(\w+)[^{\n]*\{'
    )

    pos = 0
    while True:
        match = pattern.search(text, pos)
        if not match:
            break
        name = match.group(1)
        body_start = match.end()  # just past the '{'
        body_end = _find_matching_brace(text, body_start)
        if body_end == -1:
            # Unbalanced braces -- malformed schema; stop rather than guess
            break
        results.append((name, text[body_start:body_end]))
        pos = body_end + 1

    return results


def _find_matching_brace(text: str, start: int) -> int:
    """Index of the '}' closing the '{' that precedes `start`.

    Brace counting skips braces inside string literals so a default value
    like `= "}"` does not terminate the block early. Returns -1 if
    unbalanced.
    """
    depth = 1
    i = start
    n = len(text)
    quote = ""

    while i < n:
        ch = text[i]

        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue

        if ch in ('"', "'"):
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1

    return -1


def remove_nested_blocks(body: str, keywords: tuple) -> str:
    """Blank out nested `keyword Name { ... }` blocks within a body.

    Used so a parent's own fields can be parsed without the nested block's
    fields leaking in and being misattributed to the parent. Replaced with
    blank lines to keep line numbers stable.
    """
    result = body
    for keyword in keywords:
        while True:
            blocks = extract_blocks(result, keyword)
            if not blocks:
                break
            changed = False
            for name, inner in blocks:
                # Rebuild the exact source span to excise
                pattern = re.compile(
                    r'\b' + re.escape(keyword) + r'\s+' + re.escape(name) + r'[^{\n]*\{'
                )
                m = pattern.search(result)
                if not m:
                    continue
                end = _find_matching_brace(result, m.end())
                if end == -1:
                    continue
                span = result[m.start():end + 1]
                result = result.replace(span, "\n" * span.count("\n"), 1)
                changed = True
                break
            if not changed:
                break
    return result
