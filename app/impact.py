"""Cross-repo impact analysis: which other repos does a removed symbol break?

THE ARCHITECTURE RULE THIS MODULE ENFORCES
------------------------------------------
The deterministic index decides WHAT is affected. The model decides only HOW IT
READS -- the ordering, and a sentence about why a file matters.

That split is not stylistic. A model asked "which files use `phone_number`?" will
answer with plausible paths, and a plausible path that does not exist is a false
positive delivered with full confidence -- the failure mode that would embarrass a
live demo. So `rank()` intersects the model's answer with the candidate set and
records anything invented as a VIOLATION rather than as a result. The model can
reorder and annotate; it cannot extend, and it cannot silently delete.

WHAT "NOT FOUND" IS ALLOWED TO MEAN
-----------------------------------
Three different things look identical if you only return a list:

  1. the symbol is genuinely unused elsewhere        -> a fact
  2. the repo was never indexed                      -> UNKNOWN
  3. the repo was indexed but truncated at the cap    -> PARTIALLY unknown

`ImpactResult` keeps them apart. `unsearchable` names the repos in case 2, and
`partial` is set for case 3, so "no consumers" is only ever reported when every
searchable repo was actually searched to the end.

AMBIGUITY IS A PROPERTY OF THE SYMBOL
-------------------------------------
`phone_number` matching in four files is evidence. `get` matching in four hundred
is not. A bare-name index cannot tell a reference from a coincidence, so a symbol
that is short, or that many repos define independently, is reported with its
ambiguity stated rather than as a confident hit list.
"""

import re

from dataclasses import dataclass, field as _field

#: Hedge-adverb followed by a reference-verb, e.g. "may reference", "likely contains",
#: "appears to use". A family, not a list of spellings -- see _checked_note.
_HEDGED_REFERENCE = re.compile(
    r"\b(?:likely|may|might|possibly|probably|perhaps|appears\s+to|seems\s+to|"
    r"could)\b(?:\W+\w+){0,3}?\W+"
    r"(?:contain|contains|reference|references|referencing|use|uses|using|"
    r"includ\w*|import\w*|call|calls)\b",
    re.IGNORECASE)

#: Padding: a note whose content is the candidate row read back. Matches an
#: exact/approximate-match phrase, which the row already carries. See _checked_note.
_RESTATES_THE_ROW = re.compile(
    r"\b(?:an?\s+)?(?:exact|approximate|partial)\s+match\b",
    re.IGNORECASE)

#: Below this length a bare name match is coincidence more often than reference.
#: `id`, `get`, `db` appear everywhere and mean something different each time.
MIN_UNAMBIGUOUS_LENGTH = 4

#: A symbol independently DEFINED in more than this many places is a common name
#: (`main`, `handler`, `Config`) rather than one repo's export.
MAX_INDEPENDENT_DEFINERS = 3

#: Candidates carried forward per symbol. The model is asked to rank, and a prompt
#: listing 400 paths costs ~6s of prompt eval per 400 tokens on the measured local
#: engine -- see MODEL_MEASUREMENTS. A cap that is hit is stated, never silent.
MAX_CANDIDATES = 40


@dataclass(frozen=True)
class Candidate:
    """One file that may need a change, and how sure the index is about it.

    `confidence` mirrors the extractor that found it: "exact" for an AST parse,
    "approximate" for a regex match. Collapsing the two would present a pattern
    guess as a parse result.
    """
    repo: str
    path: str
    language: str
    method: str
    kind: str                 # "reference" | "definition"
    note: str = ""            # model-authored, or "" -- never load-bearing

    @property
    def confidence(self) -> str:
        return "exact" if self.method == "ast" else "approximate"

    def key(self) -> str:
        return f"{self.repo}:{self.path}"


@dataclass
class ImpactResult:
    """The answer, plus everything that stops it from being the whole answer."""
    symbol: str
    candidates: list = _field(default_factory=list)
    unsearchable: list = _field(default_factory=list)   # [{repo, reason}]
    partial: bool = False
    refusals: list = _field(default_factory=list)
    ranked_by: str = "deterministic"
    violations: list = _field(default_factory=list)
    ambiguity: str = ""

    @property
    def searched_everything(self) -> bool:
        return not self.partial and not self.unsearchable

    def is_confident_none(self) -> bool:
        """True only when 'nothing is affected' is a FACT rather than a silence."""
        return (not self.candidates
                and self.searched_everything
                and not self.ambiguity)

    def to_dict(self) -> dict:
        return {
            "affected": [
                {"repo": c.repo, "path": c.path, "language": c.language,
                 "kind": c.kind, "confidence": c.confidence, "note": c.note}
                for c in self.candidates
            ],
            "refusals": list(self.refusals),
            "unsearchable": list(self.unsearchable),
            "partial": self.partial,
            "ranked_by": self.ranked_by,
            "violations": list(self.violations),
            "ambiguity": self.ambiguity,
        }


def _ambiguity(symbol: str, indexes) -> str:
    """Why a bare-name match for this symbol may not mean what it looks like."""
    if len(symbol) < MIN_UNAMBIGUOUS_LENGTH:
        return (f"{symbol!r} is {len(symbol)} characters, so a name match is as "
                f"likely to be coincidence as a reference")
    definers = 0
    for idx in indexes:
        if idx.definers(symbol):
            definers += 1
    if definers > MAX_INDEPENDENT_DEFINERS:
        return (f"{symbol!r} is defined independently in {definers} repos, so it is "
                f"a common name rather than one repo's export")
    return ""


def find(symbol, origin_repo, origin_path, *, indexes, known_repos=None):
    """Every indexed file that references `symbol`, excluding the changed file.

    Deterministic and exhaustive over what was indexed. `known_repos` is the set of
    repos Ripple has been GRANTED; any of them without an index is reported as
    unsearchable, because a repo nobody read must not look like a repo with no
    consumers.
    """
    result = ImpactResult(symbol=symbol)
    indexed_names = {idx.repo for idx in indexes}

    for repo in sorted(set(known_repos or ()) - indexed_names):
        result.unsearchable.append(
            {"repo": repo, "reason": "granted but never indexed"})

    for idx in sorted(indexes, key=lambda i: i.repo):
        if idx.truncated:
            result.partial = True
            result.refusals.append(
                f"{idx.repo} was indexed up to its file cap, so files beyond it were "
                f"never read and may reference {symbol!r}")
        for facts in idx.referencers(symbol):
            if idx.repo == origin_repo and facts.path == origin_path:
                continue        # the file whose change started this
            kind = "definition" if symbol in facts.defines else "reference"
            result.candidates.append(Candidate(
                repo=idx.repo, path=facts.path, language=facts.language,
                method=facts.method, kind=kind))

    # Exact hits first, then approximate: a reviewer reading top-down should meet
    # the parse results before the pattern guesses.
    result.candidates.sort(key=lambda c: (c.confidence != "exact", c.repo, c.path))

    if len(result.candidates) > MAX_CANDIDATES:
        dropped = len(result.candidates) - MAX_CANDIDATES
        result.candidates = result.candidates[:MAX_CANDIDATES]
        result.partial = True
        result.refusals.append(
            f"{dropped} further candidate file(s) matched and were not carried "
            f"forward; this list is the top {MAX_CANDIDATES}, not all of them")

    result.ambiguity = _ambiguity(symbol, indexes)
    if result.ambiguity:
        result.refusals.append(
            f"ranking is reported but not trusted: {result.ambiguity}")

    if not result.candidates and not result.searched_everything:
        result.refusals.append(
            f"no indexed file references {symbol!r}, but the search was incomplete, "
            f"so 'nothing is affected' is NOT established")

    return result


def _prompt(symbol, origin_repo, candidates) -> str:
    lines = "\n".join(
        f"{i + 1}. {c.repo}:{c.path}  [{c.language}, {c.confidence} match, {c.kind}]"
        for i, c in enumerate(candidates))
    return f"""A symbol was removed from an upstream repository and other repositories
reference it. A deterministic index -- not you -- found the files below.

REMOVED SYMBOL: {symbol}
REMOVED FROM:   {origin_repo}

CANDIDATE FILES, found by parsing or pattern-matching every indexed repo:
{lines}

YOUR ONLY JOB is to order these by how likely each is to need a change, and to add
one short clause saying why each matters.

ABSOLUTE RULES:
- Output ONLY files from the numbered list above, by their exact "repo:path" text.
- Do NOT invent a file. Do NOT guess at a file you think should exist. If you name
  a path that is not in the list it will be discarded and recorded as an error.
- Do NOT omit any file. Every one must appear exactly once. If you think a file is
  a false positive, put it LAST and say so in its clause.
- Do NOT claim you have read these files. You have not. You are seeing paths.
- Do NOT say anything is verified, tested or safe to merge.

THE CLAUSE IS THE HARD PART. Say something a reader could not already see:
- The list ALREADY says each file references the symbol. Repeating that is noise.
  "This file contains references to the removed symbol" will be DISCARDED.
- Do NOT hedge about the reference itself. It was established by parsing. "likely
  contains" or "may reference" understates a fact and reads as a guess.
- Do NOT invent consequences you cannot see -- nothing about reliability, integrity,
  customers, or "ensuring the application functions correctly". You have a path and a
  language. That is all.
- What IS worth saying: what the file's PATH and LANGUAGE suggest about the KIND of
  change, and whether an exact or approximate match makes it more or less certain.
  If you have nothing beyond the obvious, write a short clause about the match kind
  rather than padding.

FORMAT -- one per line, nothing else, no preamble, no numbering, no bullets:
repo:path | one short clause

ORDERED LIST:"""


def _parse_ranking(text, candidates):
    """Map model output back onto the candidate set. Extra paths are violations."""
    by_key = {c.key(): c for c in candidates}
    ordered, notes, invented = [], {}, []

    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("-*0123456789. ").strip()
        if not line:
            continue
        key, _, note = line.partition("|")
        key, note = key.strip().rstrip(":"), note.strip()
        if key in by_key:
            if key not in notes:
                ordered.append(key)
                notes[key] = note
        elif ":" in key or "/" in key:
            invented.append(key)

    return ordered, notes, invented


def _checked_note(note: str):
    """A model-authored clause, or ("", reason) if it makes an unearned claim.

    Ranking notes are PROSE ON A PULL REQUEST, so they go through the same claim
    check as the narrative. Building a second prose surface that bypassed
    `pr_prose.check` is exactly how a guardrail stops applying: the gate was written,
    then a new writer was added beside it.

    Notes are DROPPED, not rewritten. Editing a model's sentence to remove a claim
    leaves prose that reads as though the model stood behind the edited version.
    """
    if not note:
        return "", ""
    try:
        from .pr_prose import check
    except ImportError:
        # pr_prose is the claim authority; if it cannot be imported there is nothing
        # to check against, and an UNCHECKED note must not be presented as checked.
        return "", "the claim check was unavailable, so the note was dropped"

    violations = check(note)
    if violations:
        return "", f"note dropped: {violations[0]}"

    # The index established the reference EXACTLY. A note that hedges about it
    # understates a fact and reads as a guess.
    #
    # Matched as a FAMILY rather than a list of spellings, because the first version
    # listed "may contain" and a real run immediately produced "may reference" -- the
    # same lesson as FORBIDDEN_CLAIMS, where enumerating phrasings catches only the
    # ones you thought of. This still cannot catch every hedge a model can write; it
    # catches hedge-adverb + reference-verb, which is the shape they take. Note
    # quality remains bounded by model quality, not by this check.
    if _HEDGED_REFERENCE.search(note):
        return "", ("note dropped: hedged about a reference the index established "
                    "exactly")

    # PADDING. The candidate line already states the language and whether the match
    # was exact or approximate, so a note that says those things back adds nothing and
    # makes the pull request read like a form.
    #
    # This is not a prompt problem -- the prompt forbids it explicitly and a real 16B
    # run produced "This file is written in Python and contains an exact match to the
    # removed symbol" for all five candidates anyway. It is an INFORMATION problem: the
    # model is given a path and a language and has nothing else to say. The real fix is
    # to show it the referencing LINE, which the index does not yet store. Until then a
    # note that only restates the row is dropped rather than published.
    if _RESTATES_THE_ROW.search(note):
        return "", ("note dropped: restates the language and match kind, which the "
                    "candidate row already states")
    return note, ""


def rank(result, *, call_chain=None):
    """Ask a model to order the candidates. It cannot add, and cannot delete.

    Returns a new ImpactResult. On any failure the deterministic order stands and
    the reason is stated -- a ranking that silently reverts to alphabetical while
    claiming to be model-ranked is the misattribution this codebase keeps removing.
    """
    if call_chain is None or not result.candidates:
        return result

    chain_result = call_chain(_prompt(result.symbol, "", result.candidates))
    if not getattr(chain_result, "ok", False):
        result.refusals.append(
            "ranked deterministically, not by a model: "
            + getattr(chain_result, "stated_outcome", lambda: "no model answered")())
        return result

    ordered, notes, invented = _parse_ranking(
        getattr(chain_result, "text", ""), result.candidates)

    for key in invented:
        result.violations.append(
            f"the model named {key!r}, which the index never found -- discarded")

    by_key = {c.key(): c for c in result.candidates}
    ranked = []
    for k in ordered:
        note, why = _checked_note(notes.get(k, ""))
        if why:
            result.violations.append(f"{k}: {why}")
        src = by_key[k]
        ranked.append(Candidate(repo=src.repo, path=src.path, language=src.language,
                                method=src.method, kind=src.kind, note=note))

    # Anything the model left out is APPENDED, not dropped. A deterministic hit is
    # evidence; a model forgetting to mention it is not counter-evidence.
    missing = [c for c in result.candidates if c.key() not in set(ordered)]
    if missing:
        result.violations.append(
            f"the model omitted {len(missing)} candidate(s); they are kept, ranked "
            f"last, because the index found them and the model cannot unfind them")
    ranked.extend(missing)

    result.candidates = ranked
    result.ranked_by = "model" if ordered else "deterministic"
    if not ordered:
        result.refusals.append(
            "the model returned no usable line, so the order is deterministic")
    return result


def analyse(symbol, origin_repo, origin_path, *, indexes,
            known_repos=None, call_chain=None):
    """find() then rank(). The only entry point callers need."""
    return rank(find(symbol, origin_repo, origin_path,
                     indexes=indexes, known_repos=known_repos),
                call_chain=call_chain)
