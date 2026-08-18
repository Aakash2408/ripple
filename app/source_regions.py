"""Where are the comments and string literals in this source file?

WHY THIS IS ITS OWN MODULE
It began as `_regions` inside app/ts_codemod.py, which was the right home while only
TypeScript was scanned. Teaching that module Python would have made its name a lie,
and app/diff_contract.py already reached into it for a private function -- a shape
that invites the next person to add "just one more" language to a file named after a
different one.

WHAT DEPENDS ON GETTING THIS RIGHT
Two decisions, in opposite directions, and both are bugs:

    a COMMENT read as code    -> the codemod deletes a customer's prose, and the
                                 diff contract's "comments must survive" rule cannot
                                 catch it because it uses this same function
    a CODE span read as string -> the reference is reported as a harmless NOTE and
                                 the fix silently does not happen

INTERPOLATION IS THE HARD PART, IN BOTH LANGUAGES
Inside a TS template literal the TEXT is string content but `${...}` holds real
code. Python f-strings are exactly the same shape with different syntax:

    `phone: ${user.phoneNumber}`      text is string, ${...} is code
    f"phone: {user.phone_number}"     text is string, {...} is code

So both scanners emit the literal text as several string runs and leave the
interpolated spans out entirely, which is what makes them count as code positions.
"""
from __future__ import annotations

#: Languages with a real scanner. Anything else falls back to the TypeScript rules,
#: which is WRONG for Python-like syntax -- so callers that care must gate on this
#: set rather than assuming coverage. app/fix_generator.py does exactly that.
SCANNED = ("typescript", "javascript", "python")

#: Valid Python string prefixes, longest first so `rb` is matched before `r`.
_PY_PREFIXES = ("rb", "br", "rf", "fr", "b", "r", "u", "f")


def regions(code: str, language: str = "typescript") -> list:
    """Spans of comment and string CONTENT, as (start, end, kind)."""
    if (language or "").lower() == "python":
        return _python_regions(code)
    return _ts_regions(code)


def _ts_regions(code: str) -> list:
    """`//`, `/* */`, quoted strings, and template literals split around `${...}`."""
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


def _py_prefix_at(code: str, i: int) -> str:
    """The string prefix ending at i, e.g. the `rf` in `rf"..."`. "" if none.

    Read BACKWARDS from the quote so `format_rf` is not mistaken for an `rf` prefix:
    the character before the prefix must not be an identifier character.
    """
    for p in _PY_PREFIXES:
        k = i - len(p)
        if k < 0 or code[k:i].lower() != p:
            continue
        before = code[k - 1] if k > 0 else ""
        if before and (before.isalnum() or before == "_"):
            continue                     # part of a longer identifier
        return code[k:i].lower()
    return ""


def _python_regions(code: str) -> list:
    """`#` comments, quoted strings, triple-quoted strings, f-string splitting.

    ORDER MATTERS: a triple quote must be tested before a single one, or `\"\"\"` is
    read as an empty string followed by a stray quote and every docstring in the
    file becomes code.
    """
    out, i, n = [], 0, len(code)
    while i < n:
        ch = code[i]

        if ch == "#":
            j = code.find("\n", i)
            j = n if j == -1 else j
            out.append((i, j, "comment"))
            i = j
            continue

        if ch in "'\"":
            prefix = _py_prefix_at(code, i)
            raw = "r" in prefix
            fstring = "f" in prefix
            triple = code[i:i + 3] in ('"""', "'''")
            quote = code[i:i + 3] if triple else ch
            start = i + len(quote)

            if fstring:
                out.extend(_fstring_runs(code, start, quote, raw, triple))
                i = _skip_literal(code, start, quote, raw, triple)
                continue

            end = _skip_literal(code, start, quote, raw, triple)
            out.append((i, end, "string"))
            i = end
            continue

        i += 1
    return out


def _skip_literal(code: str, start: int, quote: str, raw: bool, triple: bool) -> int:
    """Index just past the closing quote of a literal whose body starts at `start`."""
    n, j = len(code), start
    while j < n:
        if not raw and code[j] == "\\":
            j += 2
            continue
        if code.startswith(quote, j):
            return min(j + len(quote), n)
        # A single-quoted literal cannot cross a newline; an unterminated one ends
        # there rather than swallowing the rest of the file as string content.
        if not triple and code[j] == "\n":
            return j
        j += 1
    return n


def _fstring_runs(code: str, start: int, quote: str, raw: bool,
                  triple: bool) -> list:
    """String runs of an f-string body, EXCLUDING the `{...}` interpolations.

    `{{` and `}}` are literal braces, not interpolation -- treating them as one
    would put the following text in a code span and stop the fix seeing it.
    """
    out, n, j, run_start = [], len(code), start, start
    while j < n:
        if not raw and code[j] == "\\":
            j += 2
            continue
        if code.startswith(quote, j):
            break
        if not triple and code[j] == "\n":
            break
        if code[j] == "{":
            if code.startswith("{{", j):        # escaped brace, still string text
                j += 2
                continue
            out.append((run_start, j, "string"))
            depth, k = 1, j + 1
            while k < n and depth:
                if code[k] == "{":
                    depth += 1
                elif code[k] == "}":
                    depth -= 1
                k += 1
            j = run_start = k
            continue
        if code.startswith("}}", j):
            j += 2
            continue
        j += 1
    out.append((run_start, min(j, n), "string"))
    return out
