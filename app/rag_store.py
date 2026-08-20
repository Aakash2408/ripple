from __future__ import annotations
"""
ripple/app/rag_store.py

The pattern store that rag_retriever.py imports.

WHY THIS FILE DID NOT EXIST
---------------------------
rag_retriever.py line 1 has always read:

    from app.rag_store import rag_store, FixPattern, StructuredPattern

and this module was never written. So rag_retriever could not be imported at
all, which means layers 1-2 of the four-layer fix stack -- roughly 1000 lines
of retrieval, clustering and confidence calibration -- had NEVER executed in
production. Every fix silently fell through to the deterministic template
layer, and the `except Exception: pass` around the RAG call meant nothing
reported it. It only surfaced once that handler started logging
`rag_unavailable`.

TWO STORES, DELIBERATELY
-----------------------
rag_engine.RagStore holds raw FixExamples -- one row per observed commit,
with embeddings, used for similarity search and populated by index_from_git /
index_from_propbench.

This module holds aggregated FixPatterns -- one row per
(change_type, language, field) strategy, carrying merge/reject counts so
confidence can be calibrated from real outcomes.

`ingest_examples()` is the bridge: it folds many FixExamples into the
patterns retrieval actually scores. Without it the indexed PropBench corpus
and scanned merged PRs could never influence a fix.

NAME COLLISION
--------------
`StructuredPattern` here is a CLUSTER ARCHETYPE (change_type, language,
example_count, avg_confidence) -- which is what rag_retriever expects. It is
NOT rag_engine.StructuredPattern (action, target, naming_pattern), which
describes the shape of a single diff. Same name, different concept; the
import in rag_retriever resolves to this one.
"""

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


# --------------------------------------------------------------- data model
#: Who taught us this, ordered. A LOWER rank may never replace a HIGHER one's
#: prescriptive content.
#:
#: WHY A RANK AND NOT A TIMESTAMP
#: add_pattern() merged by identity and only ever set `source_file` when empty, so
#: `strategy` was decided by whichever write happened FIRST and never revisited.
#: That is arbitrary rather than a rule: a guess folded in from the corpus could
#: permanently define the approach for a field a reviewer had already corrected by
#: hand, and the store would get less accurate as it saw more data.
#:
#:   human_edit    a reviewer changed Ripple's patch before merging it
#:   merged_clean  merged with zero human edits -- the world confirmed it
#:   rejected      closed unmerged -- confirmed negative
#:   inferred      derived from similarity/clustering; never observed
PROVENANCE_RANK = {
    "human_edit": 3,
    "merged_clean": 2,
    "rejected": 1,
    "inferred": 0,
}

#: A pattern this stale leaves retrieval. It is NOT deleted -- see `archived`.
#:
#: A pattern that worked in March against a codebase that has since been
#: refactored should not rank alongside a fresh one, and the scorer's 0.15
#: recency weight cannot achieve that alone: change_type (0.4) + language (0.25)
#: + the field-name boost (0.15) already clears the 0.7 retrieval floor with
#: zero evidence and zero recency. Age has to gate admission, not nudge ranking.
ARCHIVE_AFTER_DAYS = 90

#: Ceiling on ACTIVE patterns. Without it the store grows without limit on the
#: mounted volume -- the failure pr_ledger caps at 5000 rows. Eviction archives
#: the oldest rather than dropping them, because a counter here was earned by a
#: real merged PR and cannot be recomputed.
MAX_ACTIVE_PATTERNS = 2000

#: Provenance values exempt from ageing and from cap eviction.
#:
#: KiroCrew exempts pinned memories from decay and cap eviction; this is the
#: analogue. A reviewer's correction is the highest-authority row in the store,
#: and expiring it would both forget what we were most sure of and reopen the
#: slot to the inferred write the ladder exists to block.
PINNED_PROVENANCE = frozenset({"human_edit"})


def is_admissible(pattern, now: float = 0.0) -> tuple:
    """May this pattern be retrieved at all? Returns (bool, reason).

    ADMISSION IS SEPARATE FROM RANKING, for the same reason it is in consumer
    discovery: whether a candidate is eligible is a correctness question, and
    where it places among eligible candidates is a preference. Blending the two
    is what let a pattern rejected five times and never merged still be used --
    the evidence term is weighted 0.2 and identity alone reaches 0.80, so no
    track record however bad could pull it under the floor.

    Two grounds for refusal, both facts rather than thresholds:

      never worked   tried at least once, merged zero times. Not a weak
                     candidate -- a known-bad one. A pattern with even ONE merge
                     stays eligible and is ranked on its rate, because vetoing
                     on a ratio would invent a cutoff nobody can defend.

      dormant        last used more than ARCHIVE_AFTER_DAYS ago, unless pinned.

    NOTE the deliberate asymmetry between the two lifecycle mechanisms, which
    are easy to conflate because both remove a pattern from retrieval:

      DORMANCY is derived here, at query time, and is REVERSIBLE. The row stays
      in `patterns`, so if a new merge arrives for the same identity,
      add_pattern() bumps last_used and the pattern silently revives. That is
      the right behaviour -- a pattern is dormant because nothing needed it, not
      because it was wrong.

      CAP EVICTION physically moves rows into `archived` and does not revive.

    So a dormant row is NOT in `archived`, and the reason string must not claim
    it is.
    """
    import time as _t

    now = now or _t.time()

    if (pattern.merge_count or 0) == 0 and (pattern.reject_count or 0) > 0:
        return False, (f"tried {pattern.reject_count} time(s), never merged")

    if pattern.provenance in PINNED_PROVENANCE:
        return True, "pinned"

    if pattern.last_used:
        age_days = (now - pattern.last_used) / 86400
        if age_days > ARCHIVE_AFTER_DAYS:
            return False, (f"dormant: last used {age_days:.0f}d ago "
                           f"(> {ARCHIVE_AFTER_DAYS}d), revives on a new merge")

    return True, ""


@dataclass
class FixPattern:
    """An aggregated fix strategy, scored by retrieve_fix_pattern().

    Field set is dictated by rag_retriever's usage: _multi_signal_score reads
    change_type / language / merge_count / reject_count / last_used / repo,
    generate_explanation reads strategy / source_file, and _apply_pattern_fix
    reads new_field_name / new_type.
    """
    pattern_id: str
    change_type: str
    language: str
    field_name: str = ""
    new_field_name: str = ""
    new_type: str = ""
    strategy: str = ""          # human-readable description of the approach
    source_file: str = ""       # a representative file the pattern came from
    repo: str = ""
    merge_count: int = 0        # PRs using this pattern that were merged
    reject_count: int = 0       # PRs using this pattern that were closed
    last_used: float = 0.0      # epoch seconds
    example_count: int = 1      # how many observed examples folded in
    #: Who taught us this -- see PROVENANCE_RANK. Defaults to the LOWEST rank so an
    #: unlabelled write can never outrank an observed one by omission.
    provenance: str = "inferred"


@dataclass
class StructuredPattern:
    """A cluster archetype: the fallback when no exact pattern matches.

    NOTE: distinct from rag_engine.StructuredPattern -- see module docstring.
    """
    cluster_id: str
    change_type: str
    language: str
    example_count: int = 0
    avg_confidence: float = 0.0
    strategy: str = ""


# ------------------------------------------------------------------- store
_DATA_DIR_CANDIDATES = [
    os.environ.get("RIPPLE_DATA_DIR", ""),
    "/app/data",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"),
    "/tmp/ripple-data",
]


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
        except (IOError, OSError):
            continue
    return Path("/tmp")


class PatternStore:
    """Holds FixPatterns and cluster archetypes, persisted to disk."""

    def __init__(self, collection_name: str = "default"):
        self.collection_name = collection_name
        self.patterns: list = []
        self.structured_patterns: list = []
        #: Rows aged out or evicted by the cap. Retained, never retrieved.
        #:
        #: Deleting would destroy evidence that a real merged PR earned and that
        #: cannot be recomputed from anything on disk. Archiving keeps it
        #: auditable and leaves the door open to reviving a pattern if the same
        #: change_type/language/field comes back.
        self.archived: list = []
        self._lock = threading.Lock()
        self._loaded = False

    # ---- persistence
    @property
    def _path(self) -> Path:
        return _store_dir() / f"rag_patterns_{self.collection_name}.json"

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text())
                self.patterns = [FixPattern(**p) for p in data.get("patterns", [])]
                self.structured_patterns = [
                    StructuredPattern(**s) for s in data.get("structured_patterns", [])
                ]
                # `.get` with a default, so a store written before archiving
                # existed loads as "nothing archived yet" rather than crashing
                # into the corrupt-store branch and silently emptying itself.
                self.archived = [FixPattern(**p) for p in data.get("archived", [])]
        except (IOError, OSError, ValueError, TypeError):
            # A corrupt store must degrade to empty, not crash the webhook.
            self.patterns = []
            self.structured_patterns = []
            self.archived = []

    def save(self) -> None:
        """Called by learn_from_merged_pr / learn_from_rejected_pr."""
        try:
            self._path.write_text(json.dumps({
                "patterns": [asdict(p) for p in self.patterns],
                "structured_patterns": [asdict(s) for s in self.structured_patterns],
                "archived": [asdict(p) for p in self.archived],
            }))
        except (IOError, OSError):
            pass

    # ---- lifecycle
    def _enforce_cap(self) -> int:
        """Archive the oldest non-pinned rows until the store is under the cap.

        Caller must hold the lock. Returns how many rows moved.

        Oldest-by-last_used first, which is the same ordering pr_ledger evicts
        by. Pinned rows are skipped entirely -- the cap test deliberately makes
        the human correction the OLDEST row in the store, because that is
        precisely the case the pin exists for.
        """
        overflow = len(self.patterns) - MAX_ACTIVE_PATTERNS
        if overflow <= 0:
            return 0

        evictable = [p for p in self.patterns
                     if p.provenance not in PINNED_PROVENANCE]
        evictable.sort(key=lambda p: p.last_used or 0.0)
        doomed = {id(p) for p in evictable[:overflow]}
        if not doomed:
            # Every row is pinned. Refuse to evict rather than break the pin --
            # an unbounded store of human corrections is a smaller problem than
            # silently discarding one, and the count is visible in stats().
            return 0

        self.archived.extend(p for p in self.patterns if id(p) in doomed)
        self.patterns = [p for p in self.patterns if id(p) not in doomed]
        return len(doomed)

    def stats(self) -> dict:
        """Lifecycle counts, so archiving is visible rather than inferred."""
        import time as _t

        self.load()
        now = _t.time()
        admissible = sum(1 for p in self.patterns if is_admissible(p, now)[0])
        return {
            "active": len(self.patterns),
            "retrievable": admissible,
            "aged_or_vetoed": len(self.patterns) - admissible,
            "archived": len(self.archived),
            "pinned": sum(1 for p in self.patterns
                          if p.provenance in PINNED_PROVENANCE),
            "cap": MAX_ACTIVE_PATTERNS,
        }

    # ---- mutation
    @staticmethod
    def make_pattern_id(change_type: str, language: str, field_name: str) -> str:
        raw = f"{change_type}|{language}|{field_name}"
        return hashlib.sha1(raw.encode()).hexdigest()[:16]

    @staticmethod
    def _merge_ratio(p) -> float:
        total = (p.merge_count or 0) + (p.reject_count or 0)
        return (p.merge_count / total) if total else 0.0

    @classmethod
    def _may_replace_strategy(cls, incoming, existing) -> bool:
        """May `incoming` redefine `existing`'s prescriptive content?

        Rank decides first. At EQUAL rank the better merge ratio wins, except that
        ratios within 0.1 count as equal and the newer write takes it -- lifted from
        the semantic-conflict rule in KiroCrew's memory store, and worth copying
        because without the epsilon two near-identical confidences flip the stored
        strategy back and forth on every webhook.

        THE RATIO ONLY DISCRIMINATES WHEN BOTH SIDES HAVE OBSERVATIONS. A fresh
        write carries no counters, so comparing its 0.0 against an accumulated
        0.5 rejected it -- which meant a NEW human correction could not replace an
        older one, and the ladder's top rank was effectively frozen after its first
        write. A correction is an instruction, not a statistical claim: with no
        evidence on the incoming side the ranks are equal and the newer wins.
        """
        new_rank = PROVENANCE_RANK.get(incoming.provenance, 0)
        old_rank = PROVENANCE_RANK.get(existing.provenance, 0)
        if new_rank != old_rank:
            return new_rank > old_rank

        incoming_total = (incoming.merge_count or 0) + (incoming.reject_count or 0)
        existing_total = (existing.merge_count or 0) + (existing.reject_count or 0)
        if not incoming_total or not existing_total:
            return True        # no comparable evidence -> equal -> newer wins

        delta = cls._merge_ratio(incoming) - cls._merge_ratio(existing)
        return True if abs(delta) <= 0.1 else delta > 0

    def add_pattern(self, pattern) -> str:
        """Add or merge a pattern by identity (change_type/language/field).

        COUNTERS AND STRATEGY ARE TREATED DIFFERENTLY, ON PURPOSE.

        merge_count / reject_count / example_count are OBSERVATIONS of the world and
        accumulate from any source, including a lower-ranked one -- a rejection is a
        fact regardless of who noticed it. `strategy` (and the rename/retype fields
        that go with it) is an OPINION, and only an equal-or-higher provenance may
        replace it.

        Conflating the two would mean either discarding real outcome evidence or
        letting a corpus guess redefine a fix a human already corrected.
        """
        with self._lock:
            self.load()
            for existing in self.patterns:
                if existing.pattern_id == pattern.pattern_id:
                    # --- evidence: always accumulates -------------------------
                    existing.example_count += pattern.example_count
                    existing.merge_count += pattern.merge_count
                    existing.reject_count += pattern.reject_count
                    existing.last_used = max(existing.last_used, pattern.last_used)

                    # --- opinion: gated by the ladder -------------------------
                    if self._may_replace_strategy(pattern, existing):
                        if pattern.strategy:
                            existing.strategy = pattern.strategy
                        if pattern.new_field_name:
                            existing.new_field_name = pattern.new_field_name
                        if pattern.new_type:
                            existing.new_type = pattern.new_type
                        if pattern.source_file:
                            existing.source_file = pattern.source_file
                        if PROVENANCE_RANK.get(pattern.provenance, 0) >= \
                                PROVENANCE_RANK.get(existing.provenance, 0):
                            existing.provenance = pattern.provenance
                    elif not existing.source_file:
                        # A representative file is not prescriptive, so a lower rank
                        # may still fill it in when nothing is there.
                        existing.source_file = pattern.source_file
                    return existing.pattern_id
            self.patterns.append(pattern)
            self._enforce_cap()
            return pattern.pattern_id

    def ingest_examples(self, examples) -> dict:
        """Fold rag_engine FixExamples into aggregated FixPatterns.

        This is the connection between indexing and retrieval. Without it the
        indexed PropBench corpus and scanned merged PRs sit in a store that
        retrieval never reads.

        Examples whose change_type can never produce a fix are SKIPPED rather
        than stored: rag_engine's diff heuristic emits 'field_added' (adding an
        optional field is not breaking) and 'modified' (unclassifiable), and
        wire-only breaks have no source fix. Storing those would let them win a
        retrieval score against a real change and then produce nothing.

        Returns counts so the filtering is visible instead of silent.
        """
        from .change_types import is_fixable, category as _category

        stats = {"added": 0, "skipped_unfixable": 0, "skipped_incomplete": 0,
                 "skipped_reasons": {}}
        for ex in examples or []:
            change_type = getattr(ex, "change_type", "") or ""
            language = getattr(ex, "language", "") or ""
            field_name = getattr(ex, "field_name", "") or ""
            if not change_type or not language:
                stats["skipped_incomplete"] += 1
                continue
            if not is_fixable(change_type):
                stats["skipped_unfixable"] += 1
                reason = _category(change_type) or "unclassified"
                stats["skipped_reasons"][reason] = \
                    stats["skipped_reasons"].get(reason, 0) + 1
                continue
            pid = self.make_pattern_id(change_type, language, field_name)
            self.add_pattern(FixPattern(
                pattern_id=pid,
                change_type=change_type,
                language=language,
                field_name=field_name,
                strategy=f"{change_type} in {language}",
                source_file=getattr(ex, "fix_file", "") or "",
                repo=getattr(ex, "repo_name", "") or "",
                last_used=getattr(ex, "added_at", 0.0) or 0.0,
                example_count=1,
            ))
            stats["added"] += 1
        self._rebuild_clusters()
        return stats

    def _rebuild_clusters(self) -> None:
        """Derive cluster archetypes from the RETRIEVABLE patterns.

        Uses is_admissible(), the same predicate retrieval uses, rather than a
        second notion of "counts". A cluster's avg_confidence is consulted to
        decide something now, so it should reflect the patterns that could
        actually be retrieved now -- otherwise a pattern that has aged out of
        retrieval keeps depressing (or inflating) the cell it left, and a
        never-merged pattern that admission refuses still votes on confidence.

        The consequence, stated because it is a real trade: archiving a row
        changes the cell's avg_confidence. That is intended -- confidence is a
        claim about present behaviour, not a permanent historical average -- and
        the archived evidence remains on disk and in stats().
        """
        import time as _t

        now = _t.time()
        groups = {}
        for p in self.patterns:
            if not is_admissible(p, now)[0]:
                continue
            groups.setdefault((p.change_type, p.language), []).append(p)

        clusters = []
        for (change_type, language), members in groups.items():
            total = sum(m.merge_count + m.reject_count for m in members)
            merged = sum(m.merge_count for m in members)
            clusters.append(StructuredPattern(
                cluster_id=self.make_pattern_id(change_type, language, "*"),
                change_type=change_type,
                language=language,
                example_count=sum(m.example_count for m in members),
                avg_confidence=(merged / total) if total else 0.0,
                strategy=f"{change_type} in {language}",
            ))
        self.structured_patterns = clusters

    def count(self) -> int:
        self.load()
        return len(self.patterns)


# Module-level singleton that rag_retriever imports.
rag_store = PatternStore("default")
rag_store.load()


def get_store(collection_name: str = "default") -> PatternStore:
    """Per-org store. Falls back to the singleton for the default collection."""
    if collection_name in ("", "default"):
        return rag_store
    store = PatternStore(collection_name)
    store.load()
    return store
