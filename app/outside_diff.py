"""Find the code a change breaks that the change does not touch.

THE MISS THIS EXISTS FOR
------------------------
gstack PR #2141 records a real review miss. A client validator was loosened to
accept a typeless `.pdf`; an UNCHANGED submit helper still forced every typeless
file to `text/plain`, so the PDF was uploaded as garbage text. A diff-scoped
review could not see it, because the code that broke never appeared in the diff.
The proposed remedy was a checklist line asking the reviewer to "trace the value
through every consumer outside the diff" -- i.e. asking the model to try harder.

This module does that tracing deterministically. Three detectors, each a plain
text rule a human can check by reading the line it cites:

  same_predicate   An unchanged line BRANCHES ON the same property the change
                   branches on (`file.type === ''` in both). If the change
                   altered what values reach that property, the other branch
                   may now handle a case it never saw.
  caller           An unchanged line CALLS a function whose body the change
                   edited. Its behaviour changed underneath the caller.
  stale_message    An unchanged human-readable string near changed logic, whose
                   words overlap the identifiers the change touched. The logic
                   moved; the sentence describing it may not have.

WHAT THIS IS NOT
----------------
It is a recall net, not a proof. Every finding is a CANDIDATE, ranked, with the
rule that produced it. It does not parse or type-check; it reads lines. It will
miss a consumer that reaches the value through a different name, and it will
flag a line that happens to branch on an identically named property of a
different object -- which is why receiver and literal agreement raise severity,
and why a key matching too many sites is reported as AMBIGUOUS instead of being
listed as dozens of findings.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import languages

# Property names too generic to carry meaning as a predicate key. `x.length`
# in a changed condition says nothing about which other `.length` checks are
# related, and every repository has hundreds.
GENERIC_PROPS = frozenset({
    "length", "size_", "prototype", "constructor", "current", "default",
    "props", "state", "self", "this", "data", "value", "id", "env", "exports",
    "module", "console", "log", "error", "window", "document", "process",
    "then", "catch", "finally",
})

# A key matching more outside sites than this is ambiguous: reported once as
# a count, at low severity, rather than as a wall of findings nobody reads.
AMBIGUOUS_AT = 25

# A literal shared by more sites than this is an enum-like discriminator and
# stops counting as agreement.
DISCRIMINATOR_AT = 5

# More callers of one edited function than this collapse into one finding.
GROUP_CALLERS_AT = 3

# Lines of context around a change searched for stale human-readable strings.
MESSAGE_WINDOW = 12

STOPWORDS = frozenset("""
a an and are as at be but by can const def do else false for from function if
in into is it let new no not null of on or return self so that the then this
to true undefined var was when which while will with you your get set use
""".split())

HASH_COMMENT_LANGS = frozenset({"python", "ruby", "shell", "yaml", "elixir",
                                "perl", "r"})

_PROP = re.compile(
    r"([A-Za-z_$][\w$]*)\s*\??\.\s*([A-Za-z_$][\w$]*)(?![\w$])(?!\s*\()")
# Receivers whose agreement means nothing: every method in a class shares
# them, and the rest are throwaway names for whatever a call returned.
MEANINGLESS_RECEIVERS = frozenset({
    "self", "this", "cls", "super", "result", "res", "resp", "response",
    "proc", "r", "e", "err", "ex", "data", "obj", "item", "x", "args",
    "opts", "options", "ctx", "req", "out", "ret", "val", "tmp", "m",
    # language-global namespaces, never imported, so module_names misses them
    "Math", "JSON", "Object", "Array", "Number", "String", "Promise",
    "Symbol", "Reflect", "os", "sys", "typing",
})
_IMPORT_LINE = re.compile(r"^\s*(?:import|from|export\s+\{|.*\brequire\s*\(|"
                          r"use\s|using\s|#include)")
_COND = re.compile(
    r"\b(if|elif|elsif|while|case|when|switch|unless|match)\b"
    r"|===|!==|==|!=|<=|>=| < | > |&&|\|\||\band\b|\bor\b|\bnot\b"
    r"|\?[^?:.]+:|\.(includes|endsWith|startsWith|endswith|startswith|match|"
    r"test|has|contains)\s*\(|\bin\b")
_DEF_PATTERNS = (
    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*"
               r"([A-Za-z_$][\w$]*)"),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*"
               r"(?::[^=]+)?=\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*"
               r"(?::[^=]+)?=>"),
    re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\("),
    re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\("),
    re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)"),
    re.compile(r"^\s*(?:(?:public|private|protected|static|final|abstract|"
               r"synchronized|override|internal)\s+)+[\w<>\[\],.?]+\s+"
               r"([A-Za-z_]\w*)\s*\("),
)

# A line this close to a changed line is in the diff's own context hunk, so a
# diff-scoped reviewer already sees it. "Outside the diff" starts beyond it.
DIFF_CONTEXT = 3
_WORD = re.compile(r"[A-Za-z][a-z]+|[A-Z]+(?![a-z])")


@dataclass
class Line:
    number: int
    raw: str
    code: str              # strings replaced by "", comments removed
    literals: tuple        # string literal contents on this line
    props: tuple           # ((receiver, prop), ...) property READS
    is_condition: bool


@dataclass
class Finding:
    kind: str              # same_predicate | caller | stale_message
    severity: str          # high | medium | low
    path: str
    line: int
    snippet: str
    reason: str
    because_of: str        # "path:line" of the change that triggered it
    key: str               # the property / symbol / words that matched
    ambiguous: bool = False
    also: list = field(default_factory=list)   # further "path:line" sites


@dataclass
class OutsideDiffResult:
    findings: list = field(default_factory=list)
    ambiguous_keys: dict = field(default_factory=dict)   # key -> site count
    files_scanned: int = 0
    files_unreadable: int = 0
    predicate_keys: tuple = ()
    changed_symbols: tuple = ()


# ─── line analysis ──────────────────────────────────────────────────────────

def analyse_lines(text: str, language: str) -> list:
    """Split source into Line records, masking strings and comments.

    A small scanner, not a parser: tracks quote state and block comments across
    lines so that `"if (x.y)"` inside a string, or `// file.type` in a comment,
    never counts as code.
    """
    hash_comments = language in HASH_COMMENT_LANGS
    out = []
    in_block = False
    # A string still open at end of line: Python triple quotes and JS template
    # literals span lines, and their CONTENTS must never be read as code.
    open_quote = None
    for number, raw in enumerate(text.splitlines(), 1):
        code_chars = []
        literals = []
        i, n = 0, len(raw)
        quote = open_quote
        buf = []
        while i < n:
            ch = raw[i]
            nxt = raw[i + 1] if i + 1 < n else ""
            if in_block:
                if ch == "*" and nxt == "/":
                    in_block = False
                    i += 2
                    continue
                i += 1
                continue
            if quote:
                if ch == "\\":
                    buf.append(raw[i:i + 2])
                    i += 2
                    continue
                if raw.startswith(quote, i):
                    literals.append("".join(buf))
                    code_chars.append('""')
                    i += len(quote)
                    quote = None
                    buf = []
                    continue
                buf.append(ch)
                i += 1
                continue
            if raw.startswith(('"""', "'''"), i):
                quote = raw[i:i + 3]
                i += 3
                continue
            if ch in "\"'`":
                quote = ch
                i += 1
                continue
            if not hash_comments and ch == "/" and nxt == "/":
                break
            if not hash_comments and ch == "/" and nxt == "*":
                in_block = True
                i += 2
                continue
            if hash_comments and ch == "#":
                break
            code_chars.append(ch)
            i += 1
        if quote:   # still open at end of line
            literals.append("".join(buf))
            # Only multi-line forms carry over. A single ' or " left open is a
            # tokenizer miss (an apostrophe in a shell line, say), and carrying
            # it would swallow the rest of the file.
            open_quote = quote if (len(quote) == 3 or quote == "`") else None
        else:
            open_quote = None
        code = "".join(code_chars)
        props = tuple((r, p) for r, p in _PROP.findall(code)
                      if not p[0].isupper() or r[0].islower())
        out.append(Line(
            number=number, raw=raw, code=code, literals=tuple(literals),
            props=props, is_condition=bool(_COND.search(code)),
        ))
    return out


def is_test_path(path: str) -> bool:
    """True for test code. Tests neither SOURCE keys nor RECEIVE ranked
    findings: on 15 real OSS commits, test files produced the only HIGH (a
    test fixture line as the trigger), callers-of-test-helpers, and assertion
    messages flagged as stale prose. A test that depends on changed behaviour
    is the test suite's job to surface, not this report's."""
    p = "/" + path.replace("\\", "/").lower()
    base = p.rsplit("/", 1)[-1]
    return (any(seg in p for seg in ("/test/", "/tests/", "/__tests__/",
                                     "/spec/", "/testdata/", "/fixtures/"))
            or base.startswith("test_") or base == "conftest.py"
            or base.endswith(("_test.go", "_test.py", "_spec.rb"))
            or re.search(r"\.(test|spec)\.[cm]?[jt]sx?$", base) is not None)


_PY_IMPORT = re.compile(r"^\s*import\s+(.+)$")
_GO_IMPORT = re.compile(r'^\s*(?:import\s+)?(?:([A-Za-z_]\w*|\.)\s+)?"([\w./-]+)"\s*$')
_JS_NS_IMPORT = re.compile(
    r"^\s*import\s+(?:\*\s+as\s+([A-Za-z_$][\w$]*)|([A-Za-z_$][\w$]*))\s*"
    r"(?:,\s*\{[\w$\s,]*\})?\s+from\b")
_JS_REQUIRE = re.compile(
    r"^\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\s*\(")


def module_names(lines: list, language: str) -> set:
    """Names bound to whole MODULES in this file (`import typing`, Go `"os"`,
    `import * as path`). `typing.TYPE_CHECKING` in two files is not "the same
    receiver" in any sense that matters; it is the same module."""
    found = set()
    for ln in lines:
        raw = ln.raw
        if language == "python":
            m = _PY_IMPORT.match(raw)
            if m:
                for part in m.group(1).split(","):
                    bits = part.split()
                    if not bits:
                        continue
                    found.add(bits[-1] if "as" in bits else bits[0].split(".")[0])
        elif language == "go":
            m = _GO_IMPORT.match(raw)
            if m:
                found.add(m.group(1) or m.group(2).rsplit("/", 1)[-1])
        else:
            m = _JS_NS_IMPORT.match(raw) or _JS_REQUIRE.match(raw)
            if m:
                found.add(next(g for g in m.groups() if g))
    return found


def enclosing_definition(lines: list, line_no: int, max_up: int = 200,
                         language: str = ""):
    """(name, def_line) of the definition whose BODY contains `line_no`.

    Not merely the nearest definition above it: on real commits a changed
    comment, doc block, interface field or new top-level `const` sat BELOW a
    function that had already ended, and every caller of that untouched
    function was reported. Enclosure is checked by indentation for Python and
    by brace depth for brace languages; anything else keeps the old rule.
    """
    for idx in range(min(line_no, len(lines)) - 1,
                     max(-1, line_no - 1 - max_up), -1):
        name = _defined_name(lines[idx].raw)
        if not name:
            continue
        def_no = lines[idx].number
        if _encloses(lines, idx, line_no, language):
            return name, def_no
        return None, 0
    return None, 0


def _indent(s: str) -> int:
    return len(s) - len(s.lstrip(" \t"))


def _encloses(lines: list, def_idx: int, line_no: int, language: str) -> bool:
    if line_no == lines[def_idx].number:
        return True
    target = line_no - 1
    if language == "python":
        base = _indent(lines[def_idx].raw)
        # The body starts after the signature, which may wrap: a closing
        # `) -> ssl.SSLContext:` at the def's own indent is still signature.
        start, depth = def_idx, 0
        for idx in range(def_idx, min(target, len(lines))):
            code = lines[idx].code
            depth += sum(code.count(c) for c in "([{")
            depth -= sum(code.count(c) for c in ")]}")
            start = idx
            if depth <= 0 and code.rstrip().endswith(":"):
                break
        for idx in range(start + 1, min(target, len(lines) - 1) + 1):
            ln = lines[idx]
            if not ln.raw.strip():
                continue
            if _indent(ln.raw) <= base and ln.code.strip():
                return False
        return True
    if language in ("ruby", "yaml", "shell", languages.UNKNOWN, ""):
        return True
    depth = 0
    opened = False
    for idx in range(def_idx, min(target, len(lines))):
        for ch in lines[idx].code:
            if ch == "{":
                depth += 1
                opened = True
            elif ch == "}":
                depth -= 1
        if opened and depth <= 0:
            return False          # the body closed before the target line
    # A signature can wrap over a few lines before its `{`; a definition that
    # never opened a body within that window is a one-line arrow or a
    # declaration, and does not enclose a line further down.
    return opened or (target - def_idx) <= 3


def _defined_name(raw: str):
    for pat in _DEF_PATTERNS:
        m = pat.match(raw)
        if m:
            return m.group(1)
    return None


def words_of(text: str) -> set:
    """Lowercased content words, crude-stemmed, from identifiers or prose."""
    found = set()
    for w in _WORD.findall(text):
        w = w.lower()
        if len(w) < 3 or w in STOPWORDS:
            continue
        found.add(w[:-1] if w.endswith("s") and len(w) > 3 else w)
    return found


def _is_prose(literal: str) -> bool:
    return (" " in literal.strip() and len(literal) >= 8
            and sum(c.isalpha() for c in literal) >= 6
            and not literal.strip().startswith(("/", "http", "<", "{", "$")))


# ─── the analysis ───────────────────────────────────────────────────────────

@dataclass
class _ChangeFacts:
    # prop -> list of (receiver, literals on that line, "path:line")
    predicate_keys: dict = field(default_factory=dict)
    # symbol -> "path:line" of the def it belongs to
    changed_symbols: dict = field(default_factory=dict)
    # path -> set of changed line numbers (new side)
    changed_lines: dict = field(default_factory=dict)
    # (path, line, words) for each changed line, for stale-message windows
    changed_words: list = field(default_factory=list)


def _facts_from_change(changes, new_lines_by_path: dict, old_text_by_path: dict):
    facts = _ChangeFacts()
    for fc in changes:
        lang = languages.detect(fc.path)
        if lang == languages.UNKNOWN or languages.is_spec(fc.path):
            continue
        facts.changed_lines[fc.path] = set(fc.added_lines)
        # A brand-new file changes no behaviour anything else could already
        # depend on: nothing outside the diff can be relying on code that did
        # not exist. Its lines are still EXCLUDED from matching (above), but
        # they contribute no keys. Without this, a new module's every
        # `proc.returncode` check became a "predicate the change altered".
        if fc.status == "added":
            continue
        # A changed TEST does not change behaviour anything else depends on.
        if is_test_path(fc.path):
            continue
        new_lines = new_lines_by_path.get(fc.path) or []
        old_lines = analyse_lines(old_text_by_path.get(fc.path) or "", lang)
        by_no_new = {ln.number: ln for ln in new_lines}
        by_no_old = {ln.number: ln for ln in old_lines}
        modules = module_names(new_lines, lang) | module_names(old_lines, lang)

        added = [(n, by_no_new.get(n), new_lines) for n in fc.added_lines]
        removed = [(n, by_no_old.get(n), old_lines) for n in fc.removed_lines]
        added = [s for s in added if s[1] is not None and s[1].code.strip()]
        removed = [s for s in removed if s[1] is not None and s[1].code.strip()]

        # Only a literal the change actually ALTERED is evidence. zod 7989d07
        # rewrote a condition that kept `"backward"` on both sides, and every
        # `=== "backward"` in the codebase was promoted to MEDIUM.
        def cond_lits(side):
            return {l for _, ln, _ in side if ln.is_condition for l in ln.literals}
        changed_lits = cond_lits(added) ^ cond_lits(removed)
        # A condition that appears verbatim on both sides was moved or
        # re-indented, not changed. Compared on the RAW text: the masked code
        # blanks string literals, so `== 'x'` -> `== 'y'` would look unmoved.
        removed_conds = {ln.raw.strip() for _, ln, _ in removed if ln.is_condition}
        added_conds = {ln.raw.strip() for _, ln, _ in added if ln.is_condition}
        moved = removed_conds & added_conds

        for number, ln, side_lines in added + removed:
            where = f"{fc.path}:{number}"
            if ln.is_condition and ln.raw.strip() not in moved:
                for recv, prop in ln.props:
                    # A module attribute (`typing.TYPE_CHECKING`, `os.Args`)
                    # is the same module everywhere, not related logic.
                    if prop in GENERIC_PROPS or recv in modules:
                        continue
                    facts.predicate_keys.setdefault(prop, []).append(
                        (recv, tuple(l for l in ln.literals if l in changed_lits),
                         where))
            if side_lines is new_lines:
                name, def_line = enclosing_definition(side_lines, number,
                                                      language=lang)
                if name:
                    facts.changed_symbols.setdefault(name, f"{fc.path}:{def_line}")
            facts.changed_words.append((fc.path, number, words_of(ln.code)))
    return facts


def find_outside_diff(changes, files: dict) -> OutsideDiffResult:
    """Run the three detectors.

    `changes`  FileChange records (see app.local_diff).
    `files`    path -> CURRENT text for every scannable file in the repo,
               changed files included. Lines the change ADDED are excluded
               from matching; everything else in the repo is "outside the diff".
    The OLD text of a changed file is passed as files[("__old__", path)], so
    removed condition lines contribute predicate keys too.
    """
    result = OutsideDiffResult()
    analysed = {}
    for path, text in files.items():
        if isinstance(path, tuple):
            continue
        analysed[path] = analyse_lines(text, languages.detect(path))
    result.files_scanned = len(analysed)
    old_text = {p[1]: t for p, t in files.items() if isinstance(p, tuple)}

    facts = _facts_from_change(changes, analysed, old_text)
    result.predicate_keys = tuple(sorted(facts.predicate_keys))
    result.changed_symbols = tuple(sorted(facts.changed_symbols))

    def outside(path, number):
        return number not in facts.changed_lines.get(path, ())

    def beyond_context(path, number):
        changed = facts.changed_lines.get(path, ())
        return not any(abs(number - c) <= DIFF_CONTEXT for c in changed)

    raw_findings = []
    modules_in = {p: module_names(ls, languages.detect(p))
                  for p, ls in analysed.items()}

    # 1. same_predicate
    for prop, origins in facts.predicate_keys.items():
        recvs = {r for r, _, _ in origins} - MEANINGLESS_RECEIVERS
        lits = {l for _, ls, _ in origins for l in ls}
        sites = []
        for path, lines in analysed.items():
            for ln in lines:
                if not ln.is_condition or not beyond_context(path, ln.number):
                    continue
                hit = [r for r, p in ln.props if p == prop]
                if not hit:
                    continue
                shared_recv = bool(recvs & (set(hit) - modules_in[path]))
                shared_lit = bool(lits & set(ln.literals))
                sites.append((path, ln, shared_recv, shared_lit))
        # A literal shared by many sites is a discriminator (an enum value),
        # so sharing it says little about any ONE of them.
        if sum(1 for s in sites if s[3]) > DISCRIMINATOR_AT:
            sites = [(p, ln, r, False) for p, ln, r, _ in sites]
        if len(sites) > AMBIGUOUS_AT:
            result.ambiguous_keys[f".{prop}"] = len(sites)
            sites = [s for s in sites if s[2] and s[3]][:5]
            ambiguous = True
        else:
            ambiguous = False
        for path, ln, shared_recv, shared_lit in sites:
            # Cite the change line this site agrees with MOST, so "triggered by"
            # points at the condition a human should compare it with.
            hit_recvs = {r for r, p in ln.props if p == prop}
            def agreement(o):
                return ((o[0] in hit_recvs) + bool(set(o[1]) & set(ln.literals)))
            origin = max(origins, key=agreement)[2]
            agree = [w for w, ok in (("same receiver", shared_recv),
                                     ("same literal", shared_lit)) if ok]
            # Agreement is what separates "branches on the same value" from
            # "branches on a property that happens to share a name".
            sev = ("low" if ambiguous else
                   {2: "high", 1: "medium", 0: "low"}[len(agree)])
            raw_findings.append(Finding(
                kind="same_predicate", severity=sev, path=path,
                line=ln.number, snippet=ln.raw.strip()[:160],
                reason=(f"branches on `.{prop}`, which the change also branches "
                        f"on" + (f" ({', '.join(agree)})" if agree else "")),
                because_of=origin, key=f".{prop}", ambiguous=ambiguous))

    # 2. caller
    defined_in = {p: {n for n in map(_defined_name, (l.raw for l in ls)) if n}
                  for p, ls in analysed.items()}
    imports_in = {p: [l.raw for l in ls if _IMPORT_LINE.match(l.raw)]
                  for p, ls in analysed.items()}
    for name, origin in facts.changed_symbols.items():
        if len(name) < 3:
            continue
        call = re.compile(rf"(?<![\w$.]){re.escape(name)}\s*\(")
        def_path, def_line = origin.rsplit(":", 1)
        module = def_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        sees = re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])|"
                          rf"(?<![\w$]){re.escape(module)}(?![\w$])")
        sites = []
        for path, lines in analysed.items():
            if path != def_path:
                # Another file is a caller only if it can SEE this symbol: it
                # imports the name or the defining module, and it does not
                # define a same-named function of its own (fork_pr._git is
                # not local_diff._git).
                if name in defined_in[path]:
                    continue
                if not any(sees.search(raw) for raw in imports_in[path]):
                    continue
            for ln in lines:
                if not beyond_context(path, ln.number):
                    continue
                if path == def_path and ln.number == int(def_line):
                    continue
                if call.search(ln.code) and not any(
                        p.match(ln.raw) for p in _DEF_PATTERNS):
                    sites.append((path, ln))
        if len(sites) > AMBIGUOUS_AT:
            result.ambiguous_keys[f"{name}()"] = len(sites)
            continue
        if len(sites) > GROUP_CALLERS_AT:
            # One finding per edited function, not one per call site: a
            # refactor touched by 18 sync/async call pairs is ONE question
            # ("does any caller rely on the old behaviour?"), and 18 lines of
            # it buried everything else in the report.
            first_path, first_ln = sites[0]
            raw_findings.append(Finding(
                kind="caller", severity="medium", path=first_path,
                line=first_ln.number, snippet=first_ln.raw.strip()[:160],
                reason=(f"calls `{name}()`, whose body this change edited "
                        f"(+{len(sites) - 1} more call sites)"),
                because_of=origin, key=f"{name}()",
                also=[f"{p}:{ln.number}" for p, ln in sites[1:]]))
            continue
        for path, ln in sites:
            raw_findings.append(Finding(
                kind="caller", severity="medium", path=path, line=ln.number,
                snippet=ln.raw.strip()[:160],
                reason=f"calls `{name}()`, whose body this change edited",
                because_of=origin, key=f"{name}()"))

    # 3. stale_message
    # Only files holding a change or a caller can hold a stale message, so only
    # those are scanned -- the rest of the repository is not read twice.
    callers_by_path = {}
    for f in raw_findings:
        if f.kind == "caller":
            callers_by_path.setdefault(f.path, []).append(f.line)
    windows_by_path = {}
    for p, n, w in facts.changed_words:
        if w:
            windows_by_path.setdefault(p, []).append((n, w))
    all_change_words = set().union(
        *(w for _, _, w in facts.changed_words)) if facts.changed_words else set()
    for path in sorted(set(windows_by_path) | set(callers_by_path)):
        if is_test_path(path):
            continue        # test names and assertion messages are not UI copy
        for ln in analysed.get(path, ()):
            if not ln.literals or not outside(path, ln.number):
                continue
            for lit in ln.literals:
                if not _is_prose(lit):
                    continue
                lw = words_of(lit)
                candidates = [(path, n, w) for n, w in windows_by_path.get(path, ())
                              if abs(n - ln.number) <= MESSAGE_WINDOW]
                # Near a caller, the words to compare are those of the change
                # the caller depends on -- the whole change set's vocabulary.
                if any(abs(n - ln.number) <= 3
                       for n in callers_by_path.get(path, ())):
                    origin = next(f.because_of for f in raw_findings
                                  if f.kind == "caller" and f.path == path)
                    cpath, cline = origin.rsplit(":", 1)
                    candidates.append((cpath, int(cline), all_change_words))
                for cpath, cline, cwords in candidates:
                    overlap = sorted(lw & cwords)
                    if overlap:
                        raw_findings.append(Finding(
                            kind="stale_message", severity="low", path=path,
                            line=ln.number, snippet=ln.raw.strip()[:160],
                            reason=(f"user-facing text near changed logic shares "
                                    f"{', '.join(overlap)!s} with it -- check it "
                                    f"still describes the new behaviour"),
                            because_of=f"{cpath}:{cline}",
                            key=" ".join(overlap)))
                        break

    # Test files are listed for completeness, never ranked above LOW.
    for f in raw_findings:
        if is_test_path(f.path):
            f.severity = "low"

    # One finding per (path, line, kind): keep the most severe.
    order = {"high": 0, "medium": 1, "low": 2}
    best = {}
    for f in raw_findings:
        k = (f.path, f.line, f.kind)
        if k not in best or order[f.severity] < order[best[k].severity]:
            best[k] = f
    result.findings = sorted(
        best.values(), key=lambda f: (order[f.severity], f.path, f.line))
    return result
