"""What each repo DEFINES and what it REFERENCES, built by parsing, not by asking a model.

WHY THIS IS DETERMINISTIC AND THE MODEL IS NOWHERE IN IT
The demo has to be able to say "this pull request affects these seven files in these
three repos" and be right, in front of an audience. Measured 2026-08-26, three local
models were asked to repair a field removal whose reference was a function parameter:

    qwen2.5-coder:3b        deleted the parameter, breaking every caller
    deepseek-coder-v2:16b   BYTE-IDENTICAL wrong answer at 5x the size
    qwen3-coder:30b         declined to edit -- the only one that got it right

A model that gets that wrong will equally confidently name a file that does not exist.
So the question "which files are affected" is answered here, by reading the code, and
the model is used only to RANK and EXPLAIN what this found. Every claim the demo makes
traces back to a parse result.

EXACT VERSUS APPROXIMATE, RECORDED PER FILE
Python is parsed with `ast`, which is exact. TypeScript, JavaScript and Go are matched
with regexes, which is not -- a symbol inside a template literal or a comment can be
missed or over-matched. That difference is stored on every file as `method`, because a
downstream ranker that cannot tell an exact hit from an approximate one will present
both with the same confidence. Same reasoning as `source_regions.SCANNED` declaring
which languages have a real scanner rather than pretending all of them do.

THE SCALE LIMIT IS DECLARED, NOT DISCOVERED
Indexing at install time inverts the cost curve: the work becomes
`installs x repo_size` rather than `changes`, so an org with 200 repos would pay for all
of them on day one and most of that index would never be read. The caps below are
therefore small and deliberate, and `RepoIndex.truncated` says so out loud. A silently
partial index is worse than a refused one, because Stage 4 would report "no consumers
found" for a repo it simply stopped reading.
"""
from __future__ import annotations

import ast
import json
import os
import re
import time
from dataclasses import dataclass, field as _field

from . import languages

#: Deliberate demo-scale ceilings. See the module docstring: these exist because
#: install-time indexing scales with repo size rather than with change volume.
MAX_FILES_PER_REPO = 2000
MAX_FILE_BYTES = 256 * 1024
MAX_REPOS = 25

#: Languages with an EXACT extractor. Anything else is regex-matched and says so.
EXACT_LANGUAGES = ("python",)


@dataclass(frozen=True)
class FileFacts:
    """What one file defines and references.

    `method` is "ast" or "regex" and it is not decoration -- Stage 4 uses it to decide
    how much weight a hit carries, and a ranker that treats a regex guess as an AST
    fact will present a false positive with full confidence.
    """
    path: str
    language: str
    method: str
    defines: tuple = _field(default_factory=tuple)
    references: tuple = _field(default_factory=tuple)
    imports: tuple = _field(default_factory=tuple)

    #: symbol -> 1-based line where it is DEFINED in this revision.
    #:
    #: Required to anchor a review comment. GitHub will only accept an inline comment
    #: on a line that is part of the pull request's diff, so a comment about a removed
    #: symbol has to point at the line it occupied in the BASE revision. Without this
    #: the only options were a fabricated line number or a detached comment, and a
    #: fabricated anchor either 422s or lands on unrelated code.
    #:
    #: Absent for a symbol whose line could not be established -- callers must treat a
    #: missing entry as "no anchor" and fall back, never as line 1.
    defines_at: dict = _field(default_factory=dict)


# --- extractors -------------------------------------------------------------

def _python_facts(path: str, src: str) -> FileFacts:
    """Exact, via ast. Raises nothing: a file that does not parse is recorded as such.

    An unparseable file is reported with method "unparsed" rather than silently
    yielding no symbols, because "this file defines nothing" and "I could not read
    this file" are different facts and Stage 4 must not confuse them.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return FileFacts(path=path, language="python", method="unparsed")

    defines, references, imports = [], [], []

    # MODULE-LEVEL AND CLASS-LEVEL ASSIGNMENTS ARE EXPORTED SYMBOLS TOO.
    #
    # The first version collected only FunctionDef/AsyncFunctionDef/ClassDef, and a
    # self-test against this repo caught it: `FORBIDDEN_CLAIMS` reported "defined in 0
    # files, referenced in 1" while living in app/pr_prose.py as a module-level dict.
    # A removed or renamed CONSTANT is exactly the breaking change this index exists to
    # trace, and the regex extractors already captured `export const NAME` -- so Python,
    # the one language with an EXACT parser, was the weakest of the set.
    #
    # THE SAME GAP THEN REAPPEARED ONE LEVEL DOWN. Collecting only `tree.body` missed
    # CLASS ATTRIBUTES: `class User: phone_number = ""` defined only `User`, so
    # removing a field from a model class -- the single most common breaking change
    # this tool exists to catch, and the one in every worked example -- resolved to
    # zero removed symbols and the pull request was reported as needing no analysis.
    # A class body is part of the module's surface; `u.phone_number` is what consumers
    # in other repos actually reference.
    #
    # Function bodies are still excluded: a local variable is not surface, and
    # including it would flood `defines` with noise that ranking must then rank down.
    at = {}

    def _note(name, node):
        defines.append(name)
        at.setdefault(name, getattr(node, "lineno", 0))

    def _surface(body):
        for node in body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        _note(target.id, target)
                    elif isinstance(target, (ast.Tuple, ast.List)):
                        for elt in target.elts:
                            if isinstance(elt, ast.Name):
                                _note(elt.id, elt)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                _note(node.target.id, node.target)

    _surface(tree.body)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            _surface(node.body)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _note(node.name, node)
        elif isinstance(node, ast.Import):
            for a in node.names:
                imports.append(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module.split(".")[0])
            for a in node.names:
                references.append(a.name)
        elif isinstance(node, ast.Attribute):
            references.append(node.attr)
        elif isinstance(node, ast.Name):
            references.append(node.id)
    return FileFacts(path=path, language="python", method="ast",
                     defines=tuple(sorted(set(defines))),
                     references=tuple(sorted(set(references))),
                     imports=tuple(sorted(set(imports))),
                     defines_at={k: v for k, v in at.items() if v})


#: Regex extractors. APPROXIMATE by construction -- see the module docstring.
_REGEX_DEFINES = {
    "typescript": (
        r"^\s*export\s+(?:default\s+)?(?:async\s+)?"
        r"(?:class|function|const|let|var|interface|type|enum)\s+([A-Za-z_$][\w$]*)"),
    "javascript": (
        r"^\s*export\s+(?:default\s+)?(?:async\s+)?"
        r"(?:class|function|const|let|var)\s+([A-Za-z_$][\w$]*)"),
    "go": r"^\s*(?:func|type)\s+(?:\([^)]*\)\s*)?([A-Z][\w]*)",
    "java": r"^\s*(?:public|protected)\s+(?:static\s+)?(?:final\s+)?"
            r"(?:class|interface|enum|record)\s+([A-Za-z_$][\w$]*)",
    "ruby": r"^\s*(?:class|module|def)\s+([A-Za-z_][\w?!]*)",
    "rust": r"^\s*(?:pub\s+)?(?:fn|struct|enum|trait|type)\s+([A-Za-z_][\w]*)",
    "kotlin": r"^\s*(?:public\s+)?(?:class|interface|object|fun)\s+([A-Za-z_][\w]*)",
    "csharp": r"^\s*(?:public|internal)\s+(?:static\s+)?(?:partial\s+)?"
              r"(?:class|interface|struct|enum|record)\s+([A-Za-z_][\w]*)",
}

_REGEX_IMPORTS = {
    "typescript": r"""(?:from\s+|require\(\s*)['"]([^'"]+)['"]""",
    "javascript": r"""(?:from\s+|require\(\s*)['"]([^'"]+)['"]""",
    "go": r"""^\s*(?:_\s+)?['"]([^'"]+)['"]""",
    "java": r"^\s*import\s+([\w.]+)\s*;",
    "ruby": r"""^\s*require(?:_relative)?\s+['"]([^'"]+)['"]""",
    "rust": r"^\s*use\s+([\w:]+)",
    "kotlin": r"^\s*import\s+([\w.]+)",
    "csharp": r"^\s*using\s+([\w.]+)\s*;",
}

#: Any capitalised or snake identifier -- the reference candidates a symbol search
#: would look for. Over-inclusive on purpose: this feeds a filter, not a claim.
_IDENT = re.compile(r"\b([A-Za-z_$][\w$]{2,})\b")


def _regex_facts(path: str, src: str, lang: str) -> FileFacts:
    defines, at = [], {}
    if lang in _REGEX_DEFINES:
        # finditer rather than findall so the line number travels with the match. The
        # line is as approximate as the match that produced it, which `method` already
        # says -- an anchor derived from a regex hit is a guess about WHERE as well as
        # about WHETHER.
        for m in re.finditer(_REGEX_DEFINES[lang], src, re.M):
            name = m.group(1) if m.groups() else m.group(0)
            defines.append(name)
            # The GROUP's start, not the match's. Every one of these patterns opens
            # with `^\s*`, and \s matches a newline, so a match preceded by a blank
            # line BEGINS on that blank line -- `export function bar` on line 3 was
            # reported as line 2. An anchor one line early either 422s (the line is
            # not in the diff) or lands the comment on unrelated code.
            start = m.start(1) if m.groups() else m.start()
            at.setdefault(name, src.count("\n", 0, start) + 1)
    imports = re.findall(_REGEX_IMPORTS[lang], src, re.M) if lang in _REGEX_IMPORTS else []
    return FileFacts(path=path, language=lang, method="regex",
                     defines=tuple(sorted(set(defines))),
                     references=tuple(sorted(set(_IDENT.findall(src)))),
                     imports=tuple(sorted(set(imports))),
                     defines_at=at)


def extract(path: str, src: str) -> FileFacts:
    """Facts for one file, using the exact extractor where one exists.

    Language detection goes through app/languages.py, which is the ONLY module allowed
    to define it -- a second detector is a second place to forget, and there is a gate
    that fails if one appears.
    """
    lang = languages.detect(path)
    if lang == "python":
        return _python_facts(path, src)
    if lang in _REGEX_DEFINES:
        return _regex_facts(path, src, lang)
    return FileFacts(path=path, language=lang, method="unsupported")


# --- the index --------------------------------------------------------------

@dataclass
class RepoIndex:
    """One repo's symbol map, and an honest account of what was left out."""
    repo: str
    indexed_at: float = 0.0
    files: dict = _field(default_factory=dict)      # path -> FileFacts
    truncated: bool = False
    skipped: dict = _field(default_factory=dict)    # reason -> count

    def definers(self, symbol: str) -> list:
        """Files that DEFINE this symbol, exact matches first."""
        hits = [f for f in self.files.values() if symbol in f.defines]
        return sorted(hits, key=lambda f: (f.method != "ast", f.path))

    def referencers(self, symbol: str) -> list:
        """Files that REFERENCE this symbol, exact matches first."""
        hits = [f for f in self.files.values()
                if symbol in f.references or symbol in f.defines]
        return sorted(hits, key=lambda f: (f.method != "ast", f.path))

    def stats(self) -> dict:
        by_method = {}
        for f in self.files.values():
            by_method[f.method] = by_method.get(f.method, 0) + 1
        return {
            "repo": self.repo,
            "files": len(self.files),
            "symbols": len({s for f in self.files.values() for s in f.defines}),
            "by_method": by_method,
            "truncated": self.truncated,
            "skipped": dict(self.skipped),
        }

    def to_dict(self) -> dict:
        return {
            "repo": self.repo,
            "indexed_at": self.indexed_at,
            "truncated": self.truncated,
            "skipped": dict(self.skipped),
            "files": {p: {"language": f.language, "method": f.method,
                          "defines": list(f.defines),
                          "references": list(f.references),
                          "imports": list(f.imports),
                          "defines_at": dict(f.defines_at)}
                      for p, f in self.files.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RepoIndex":
        idx = cls(repo=d.get("repo", ""), indexed_at=d.get("indexed_at", 0.0),
                  truncated=bool(d.get("truncated")), skipped=dict(d.get("skipped") or {}))
        for path, f in (d.get("files") or {}).items():
            idx.files[path] = FileFacts(
                path=path, language=f.get("language", ""), method=f.get("method", ""),
                defines=tuple(f.get("defines") or ()),
                references=tuple(f.get("references") or ()),
                imports=tuple(f.get("imports") or ()),
                defines_at={k: int(v) for k, v in (f.get("defines_at") or {}).items()})
        return idx


def build(repo: str, entries) -> RepoIndex:
    """Index a repo from an iterable of (path, content).

    `entries` is injected rather than fetched here so this module never touches the
    network: the same builder serves the GitHub API, a GitLab API, and a local
    directory, and the tests do not need a token.
    """
    idx = RepoIndex(repo=repo, indexed_at=time.time())
    for path, content in entries:
        if len(idx.files) >= MAX_FILES_PER_REPO:
            # Declared, not discovered. Stage 4 must be able to tell "no consumers"
            # from "stopped reading", so this is recorded rather than logged.
            idx.truncated = True
            idx.skipped["file_cap"] = idx.skipped.get("file_cap", 0) + 1
            continue
        if not languages.is_scannable(path):
            idx.skipped["not_scannable"] = idx.skipped.get("not_scannable", 0) + 1
            continue
        if content is None:
            idx.skipped["unreadable"] = idx.skipped.get("unreadable", 0) + 1
            continue
        if len(content) > MAX_FILE_BYTES:
            idx.skipped["too_large"] = idx.skipped.get("too_large", 0) + 1
            continue
        facts = extract(path, content)
        if facts.method == "unsupported":
            idx.skipped["unsupported_language"] = \
                idx.skipped.get("unsupported_language", 0) + 1
            continue
        idx.files[path] = facts
    return idx


# --- persistence ------------------------------------------------------------

_DATA_DIR_CANDIDATES = [
    os.environ.get("RIPPLE_DATA_DIR", ""),
    "/app/data",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"),
]


@dataclass(frozen=True)
class SymbolDelta:
    """What a single file's change did to the symbols it exports.

    `removed` is the interesting set: a symbol that existed at the base and is gone at
    the head is the thing that breaks other repos.

    `reliable` IS THE LOAD-BEARING FIELD. If either side could not be parsed, an empty
    `removed` means "I could not tell", not "nothing was removed" -- and those two must
    never collapse into the same value. That collapse is the exact failure this index
    was built to avoid: reporting a repo as clean when it was never read. A caller that
    ignores `reliable` will confidently announce a PR is safe because a syntax error
    made it unreadable.
    """
    path: str
    language: str
    method: str
    removed: tuple = _field(default_factory=tuple)
    added: tuple = _field(default_factory=tuple)
    reliable: bool = True
    reason: str = ""

    #: removed symbol -> the line it occupied in the BASE revision.
    #:
    #: This is what a review comment anchors to. It must come from the base side: the
    #: symbol does not exist at the head, so there is no head line to point at. A
    #: symbol missing from this map has NO anchor, and the comment must be posted
    #: unanchored rather than guessing a line.
    removed_at: dict = _field(default_factory=dict)

    @property
    def is_breaking(self) -> bool:
        """A removal we can actually vouch for."""
        return bool(self.removed) and self.reliable


def delta(base: FileFacts, head: FileFacts) -> SymbolDelta:
    """Symbols lost and gained between two versions of one file.

    `method` is the WEAKER of the two sides, because a delta is only as trustworthy as
    the less certain half of it. An AST base against a regex head is a regex-grade
    answer, and presenting it as exact would overstate it.
    """
    trustworthy = ("ast", "regex")
    if base.method not in trustworthy or head.method not in trustworthy:
        bad = base if base.method not in trustworthy else head
        side = "base" if bad is base else "head"
        return SymbolDelta(
            path=head.path or base.path,
            language=head.language or base.language,
            method="unknown", reliable=False,
            reason=f"{side} revision is {bad.method!r}, so the symbol set is unknown "
                   f"rather than empty -- a syntax error must not read as 'nothing "
                   f"was removed'")

    method = "regex" if "regex" in (base.method, head.method) else "ast"
    removed = tuple(sorted(set(base.defines) - set(head.defines)))
    added = tuple(sorted(set(head.defines) - set(base.defines)))
    return SymbolDelta(path=head.path or base.path,
                       language=head.language or base.language,
                       method=method, removed=removed, added=added, reliable=True,
                       removed_at={sym: base.defines_at[sym] for sym in removed
                                   if sym in base.defines_at})


def _data_dir() -> str:
    """Same resolution order as rag_store and pr_ledger, so all state lands together."""
    for candidate in _DATA_DIR_CANDIDATES:
        if not candidate:
            continue
        try:
            os.makedirs(candidate, exist_ok=True)
            return candidate
        except OSError:
            continue
    raise RuntimeError("no writable data directory for the repo index")


def _path_for(repo: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", repo or "unknown")
    return os.path.join(_data_dir(), f"repo_index_{safe}.json")


def save(idx: RepoIndex) -> str:
    path = _path_for(idx.repo)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(idx.to_dict(), fh, indent=1, sort_keys=True)
    return path


def load(repo: str):
    """The stored index for a repo, or None. Never raises on a corrupt file.

    A corrupt index is returned as None so the caller re-indexes, rather than crashing
    a webhook -- but it is NOT silently treated as an empty index, because that would
    make a broken file look like a repo with no symbols.
    """
    path = _path_for(repo)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return RepoIndex.from_dict(json.load(fh))
    except (json.JSONDecodeError, OSError):
        return None


def indexed_repos() -> list:
    """Every repo with a stored index, so Stage 4 can search across all of them."""
    out = []
    try:
        for name in sorted(os.listdir(_data_dir())):
            if name.startswith("repo_index_") and name.endswith(".json"):
                idx = load(name[len("repo_index_"):-len(".json")])
                if idx is not None:
                    out.append(idx)
    except OSError:
        pass
    return out
