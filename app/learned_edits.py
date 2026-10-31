"""
ripple/app/learned_edits.py

Learn from what a reviewer DID to a Ripple PR, and do it next time.

WHAT WAS MISSING
----------------
The RAG layer counted merges and rejections per strategy, but every strategy
ended in the same `apply_fix_template` call, so no amount of evidence could
change the CODE Ripple wrote. And a merge a reviewer had to edit -- the most
informative outcome there is -- only raised a provenance flag, because "the
edit content was not captured (needs the PR diff)".

This module captures it. When a fix PR merges after a human edited it, the
consumer file before the PR and the file as merged are diffed, and every change
the reviewer made AROUND THE FIELD becomes a rule:

    site     the line(s) referencing the field, with the field abstracted
             (`{ name, email, ⟦camel⟧ }`)
    outcome  what the reviewer turned them into (`{ name, email }`), or the
             same line unchanged, which means "leave this alone"

The next fix for the same change type and language, in the same org, looks
for those sites first. A matched site gets the reviewer's outcome; everything
else still goes to the template.

THE RULES ARE NARROW ON PURPOSE
-------------------------------
  * Only lines that reference the field are learned. A reviewer's unrelated
    tidy-up in the same file is not a fix strategy.
  * A site matches only on the EXACT normalised text (indentation stripped,
    the field abstracted). No fuzzy matching: a near-miss is a different shape
    and the reviewer has not ruled on it.
  * Rules are scoped to the org (the repo owner) that taught them. A rule
    contains that org's code, so applying it to another tenant would leak it.
  * `field_removed` rules generalise across fields, because "drop the shorthand
    property, keep the parameter" is a statement about the SHAPE. Every other
    change type stays field-specific: what to SEND for a newly required field,
    or what a rename becomes, is a statement about that one field.
  * Insertions with no field reference in the original (a reviewer adding a
    comment or a default somewhere else) are reported as not learned.

HOW THE TEMPLATE AND THE RULES COMBINE
--------------------------------------
Matched sites are replaced with a sentinel COMMENT before the template runs.
Every template in fix_templates leaves comments alone and the sentinel carries
no field name, so it passes through; after the template, each sentinel is
swapped for the reviewer's outcome. If any sentinel does not come back exactly
once, the learned result is DISCARDED and the caller is told why -- a half-
applied rule set is worse than none.

A learned outcome of "leave unchanged" is a real answer. `apply()` returns it
even when the resulting code equals the input, and the caller must not fall
through to a template that would make the very edit the reviewer reverted.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .fix_templates import name_variants

#: Same candidate ladder as pr_ledger / rag_store / activity. These rules are
#: earned from real merges and cannot be recomputed, so they must live on the
#: mounted volume -- the reason RagStore's /tmp default is a bug.
_DATA_DIR_CANDIDATES = [
    os.environ.get("RIPPLE_DATA_DIR", ""),
    "/app/data",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"),
    "/tmp/ripple-data",
]

#: A replaced or deleted block longer than this is a rewrite, not a fix
#: strategy for one reference. Its field lines are learned one by one when the
#: block can be paired line for line, and reported as skipped otherwise.
MAX_BLOCK = 4

#: Generous but finite. Eviction is least-recently-confirmed first.
MAX_RULES = 2000

#: Placeholder styles, in the order a variant is claimed when two styles spell
#: the field the same way (`email` is snake, camel and literal at once). The
#: order is fixed so learning and applying always pick the same style.
_STYLES = ("snake", "camel", "pascal", "upper_snake", "literal")

#: Change types whose rules apply to ANY field. See module docstring.
_FIELD_GENERIC = {"field_removed"}

_CANONICAL = {
    "removed_field": "field_removed",
    "field_removed": "field_removed",
    "renamed_field": "field_renamed",
    "field_renamed": "field_renamed",
    "type_changed": "field_type_changed",
    "field_type_changed": "field_type_changed",
}

_LINE_COMMENT = {
    "python": "#", "ruby": "#",
    "typescript": "//", "javascript": "//", "go": "//", "java": "//",
    "kotlin": "//", "csharp": "//", "rust": "//", "swift": "//", "scala": "//",
    "php": "//", "dart": "//", "cpp": "//", "c": "//",
}

_SENTINEL = "__RIPPLE_LEARNED_{}__"


def canonical_change_type(change_type: str) -> str:
    return _CANONICAL.get(change_type or "", change_type or "")


def _ph(style: str) -> str:
    return f"\u27e6{style}\u27e7"


_PH_RE = re.compile("\u27e6(" + "|".join(_STYLES) + ")\u27e7")


def _variant_table(field_name: str) -> list:
    """[(variant, style)] longest first, each spelling claimed by one style."""
    variants = name_variants(field_name)
    claimed: dict = {}
    for style in _STYLES:
        v = variants.get(style) or ""
        if v and v not in claimed:
            claimed[v] = style
    return sorted(claimed.items(), key=lambda kv: -len(kv[0]))


def _abstract(text: str, table: list) -> str:
    for variant, style in table:
        text = re.sub(r"(?<![A-Za-z0-9_])" + re.escape(variant) + r"(?![A-Za-z0-9_])",
                      _ph(style), text)
    return text


def _instantiate(text: str, field_name: str) -> str:
    variants = name_variants(field_name)
    return _PH_RE.sub(lambda m: variants.get(m.group(1)) or field_name, text)


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _has_ph(texts) -> bool:
    return any(_PH_RE.search(t) for t in texts)


def _split(code: str):
    nl = "\r\n" if "\r\n" in code else "\n"
    return code.split(nl), nl


# ------------------------------------------------------------------ learning
def _outcome_lines(site_indent: str, lines: list, table: list) -> list:
    """[[mode, indent, text]]: mode 'rel' keeps the outcome's depth relative to
    the site, so a rule learned at one nesting level applies at another."""
    out = []
    for ln in lines:
        ind = _indent(ln)
        text = _abstract(ln.strip(), table)
        if ind.startswith(site_indent):
            out.append(["rel", ind[len(site_indent):], text])
        else:
            out.append(["abs", ind, text])
    return out


def extract_rules(base: str, final: str, field_name: str) -> tuple:
    """(rules, skipped). Each rule is {"site": [...], "outcome": [...]}.

    Pure: no store, no I/O. `skipped` names every reviewer change around the
    field that was NOT turned into a rule, so the caller can report it instead
    of implying the whole edit was learned.
    """
    table = _variant_table(field_name)
    b, _ = _split(base)
    f, _ = _split(final)
    is_site = [_has_ph([_abstract(ln, table)]) for ln in b]

    rules, skipped = [], []

    def add(i1, i2, out_lines):
        site = [_abstract(ln.strip(), table) for ln in b[i1:i2]]
        rules.append({"site": site,
                      "outcome": _outcome_lines(_indent(b[i1]), out_lines, table)})

    sm = difflib.SequenceMatcher(None, b, f, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for i in range(i1, i2):
                if is_site[i]:
                    add(i, i + 1, [b[i]])          # the reviewer kept it
            continue
        if tag == "insert":
            if _has_ph([_abstract(ln, table) for ln in f[j1:j2]]):
                skipped.append(f"insertion of {j2 - j1} line(s) at final line "
                               f"{j1 + 1} has no field reference in the original "
                               f"to attach to")
            continue
        if not any(is_site[i1:i2]):
            continue                                # not about the field
        if i2 - i1 <= MAX_BLOCK:
            add(i1, i2, f[j1:j2])
        elif tag == "replace" and i2 - i1 == j2 - j1:
            for k in range(i2 - i1):
                if is_site[i1 + k]:
                    add(i1 + k, i1 + k + 1, [f[j1 + k]])
        elif tag == "delete":
            for i in range(i1, i2):
                if is_site[i]:
                    add(i, i + 1, [])
        else:
            skipped.append(f"base lines {i1 + 1}-{i2} were rewritten into "
                           f"{j2 - j1} line(s) -- too large to learn as one rule")
    return rules, skipped


# --------------------------------------------------------------------- store
def _store_dir() -> Path:
    for candidate in _DATA_DIR_CANDIDATES:
        if not candidate:
            continue
        p = Path(candidate)
        try:
            p.mkdir(parents=True, exist_ok=True)
            probe = p / ".write_test"
            probe.write_text("ok")
            probe.unlink()
            return p
        except OSError:
            continue
    raise OSError("no writable data directory for learned edits")


class LearnedEditStore:
    """Rules keyed by (scope, change type, language, field-or-*, site).

    Each key holds every outcome reviewers chose for that site, with the PRs
    that voted for it. A PR votes once per rule however often its webhook is
    redelivered, and the outcome with the most votes wins (ties: most recent).
    """

    def __init__(self, path: Optional[Path] = None):
        self._explicit = path
        self._lock = threading.Lock()
        self._rules: dict = {}
        self._loaded = False
        #: Strictly increasing, so "most recent wins a tie" never depends on
        #: two learns landing in the same clock tick.
        self._clock = 0.0

    @property
    def path(self) -> Path:
        return self._explicit or (_store_dir() / "learned_edits.json")

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        p = self.path
        if not p.exists():
            self._rules = {}
            return
        # A corrupt store RAISES. Starting empty would silently forget every
        # rule reviewers taught, and the next fix would redo what they undid.
        self._rules = json.loads(p.read_text()).get("rules", {})

    def _save(self) -> None:
        p = self.path
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"rules": self._rules}))
        os.replace(tmp, p)

    @staticmethod
    def _key(scope, change_type, language, field_key, site) -> str:
        return json.dumps([scope, change_type, language, field_key, site],
                          ensure_ascii=False)

    def learn(self, *, scope: str, change_type: str, language: str,
              field_name: str, base: str, final: str, source: str) -> dict:
        ct = canonical_change_type(change_type)
        field_key = "*" if ct in _FIELD_GENERIC else field_name
        rules, skipped = extract_rules(base, final, field_name)
        learned = reinforced = duplicate = 0
        with self._lock:
            self._load()
            now = self._clock = max(time.time(), self._clock + 1e-6)
            for r in rules:
                key = self._key(scope, ct, language, field_key, r["site"])
                entry = self._rules.setdefault(key, {"outcomes": {}})
                okey = json.dumps(r["outcome"], ensure_ascii=False)
                slot = entry["outcomes"].get(okey)
                if slot is None:
                    entry["outcomes"][okey] = {"outcome": r["outcome"], "votes": 1,
                                               "sources": [source], "last": now}
                    learned += 1
                elif source in slot["sources"]:
                    duplicate += 1
                else:
                    slot["votes"] += 1
                    slot["sources"] = (slot["sources"] + [source])[-10:]
                    slot["last"] = now
                    reinforced += 1
                entry["last"] = now
            self._evict()
            self._save()
        return {"rules_learned": learned, "rules_reinforced": reinforced,
                "already_counted": duplicate, "skipped": skipped,
                "change_type": ct, "language": language, "scope": scope}

    def _evict(self) -> None:
        if len(self._rules) <= MAX_RULES:
            return
        for k in sorted(self._rules, key=lambda k: self._rules[k].get("last", 0)
                        )[: len(self._rules) - MAX_RULES]:
            del self._rules[k]

    def rules_for(self, *, scope: str, change_type: str, language: str,
                  field_name: str) -> list:
        """[(site, outcome, sources)] for the winning outcome of each site."""
        ct = canonical_change_type(change_type)
        field_key = "*" if ct in _FIELD_GENERIC else field_name
        out = []
        with self._lock:
            self._load()
            for key, entry in self._rules.items():
                k_scope, k_ct, k_lang, k_field, site = json.loads(key)
                if (k_scope, k_ct, k_lang, k_field) != (scope, ct, language, field_key):
                    continue
                best = max(entry["outcomes"].values(),
                           key=lambda s: (s["votes"], s["last"]))
                out.append((site, best["outcome"], list(best["sources"])))
        # Longest site first, so a multi-line rule wins over a one-line rule
        # that matches its first line.
        out.sort(key=lambda r: -len(r[0]))
        return out

    def stats(self) -> dict:
        with self._lock:
            self._load()
            return {"rules": len(self._rules), "path": str(self.path)}


# ------------------------------------------------------------------ applying
@dataclass
class LearnedResult:
    """A learned fix, or the reason none could be applied.

    `applied` is False when no rule matched (`reason` empty) or when rules
    matched but had to be discarded (`reason` says why). Only an applied
    result may be used, and it may equal the input: see module docstring.
    """
    applied: bool
    code: str = ""
    explanation: str = ""
    sites: int = 0
    sources: list = field(default_factory=list)
    reason: str = ""


def apply(code: str, *, store: LearnedEditStore, scope: str, change_type: str,
          language: str, field_name: str,
          template_fn: Callable[[str], tuple]) -> LearnedResult:
    """Apply learned rules to `code`; `template_fn(code) -> (code, expl)` fixes
    whatever no rule covers."""
    prefix = _LINE_COMMENT.get(language)
    if not prefix or not scope:
        return LearnedResult(applied=False)
    rules = store.rules_for(scope=scope, change_type=change_type,
                            language=language, field_name=field_name)
    if not rules:
        return LearnedResult(applied=False)

    table = _variant_table(field_name)
    lines, nl = _split(code)
    norm = [_abstract(ln.strip(), table) for ln in lines]

    matches = []          # (start, length, outcome, sources)
    i = 0
    while i < len(lines):
        for site, outcome, sources in rules:
            k = len(site)
            if k and norm[i:i + k] == site and _has_ph(site):
                matches.append((i, k, outcome, sources))
                i += k
                break
        else:
            i += 1
    if not matches:
        return LearnedResult(applied=False)

    masked, cursor = [], 0
    for n, (start, k, _o, _s) in enumerate(matches):
        masked.extend(lines[cursor:start])
        masked.append(f"{_indent(lines[start])}{prefix} {_SENTINEL.format(n)}")
        cursor = start + k
    masked.extend(lines[cursor:])

    tmpl_code, tmpl_expl = template_fn(nl.join(masked))
    out_lines, _ = _split(tmpl_code)

    positions = {}
    for idx, ln in enumerate(out_lines):
        m = re.fullmatch(r"\s*" + re.escape(prefix) + r" __RIPPLE_LEARNED_(\d+)__\s*", ln)
        if m:
            positions.setdefault(int(m.group(1)), []).append(idx)
    if (sorted(positions) != list(range(len(matches)))
            or any(len(v) != 1 for v in positions.values())):
        return LearnedResult(
            applied=False,
            reason=f"the template moved or dropped {len(matches)} learned site "
                   f"marker(s), so the reviewer's edits could not be placed safely")

    restored = []
    for idx, ln in enumerate(out_lines):
        m = re.fullmatch(r"\s*" + re.escape(prefix) + r" __RIPPLE_LEARNED_(\d+)__\s*", ln)
        if not m:
            restored.append(ln)
            continue
        start, _k, outcome, _s = matches[int(m.group(1))]
        site_indent = _indent(lines[start])
        for mode, ind, text in outcome:
            prefix_ws = site_indent + ind if mode == "rel" else ind
            restored.append(prefix_ws + _instantiate(text, field_name))

    sources = sorted({s for _a, _b, _c, srcs in matches for s in srcs})
    shown = ", ".join(sources[:3]) + (f" (+{len(sources) - 3} more)" if len(sources) > 3 else "")
    explanation = (f"[learned] {len(matches)} site(s) fixed the way a reviewer "
                   f"fixed them before (learned from {shown}). "
                   f"Remaining references: {tmpl_expl}")
    return LearnedResult(applied=True, code=nl.join(restored),
                         explanation=explanation, sites=len(matches),
                         sources=sources)


_default_store: Optional[LearnedEditStore] = None


def default_store() -> LearnedEditStore:
    global _default_store
    if _default_store is None:
        _default_store = LearnedEditStore()
    return _default_store
