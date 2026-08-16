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
        except (IOError, OSError, ValueError, TypeError):
            # A corrupt store must degrade to empty, not crash the webhook.
            self.patterns = []
            self.structured_patterns = []

    def save(self) -> None:
        """Called by learn_from_merged_pr / learn_from_rejected_pr."""
        try:
            self._path.write_text(json.dumps({
                "patterns": [asdict(p) for p in self.patterns],
                "structured_patterns": [asdict(s) for s in self.structured_patterns],
            }))
        except (IOError, OSError):
            pass

    # ---- mutation
    @staticmethod
    def make_pattern_id(change_type: str, language: str, field_name: str) -> str:
        raw = f"{change_type}|{language}|{field_name}"
        return hashlib.sha1(raw.encode()).hexdigest()[:16]

    def add_pattern(self, pattern) -> str:
        """Add or merge a pattern by identity (change_type/language/field)."""
        with self._lock:
            self.load()
            for existing in self.patterns:
                if existing.pattern_id == pattern.pattern_id:
                    existing.example_count += pattern.example_count
                    existing.merge_count += pattern.merge_count
                    existing.reject_count += pattern.reject_count
                    existing.last_used = max(existing.last_used, pattern.last_used)
                    if not existing.source_file:
                        existing.source_file = pattern.source_file
                    return existing.pattern_id
            self.patterns.append(pattern)
            return pattern.pattern_id

    def ingest_examples(self, examples) -> int:
        """Fold rag_engine FixExamples into aggregated FixPatterns.

        This is the connection between indexing and retrieval. Without it the
        indexed PropBench corpus and scanned merged PRs sit in a store that
        retrieval never reads.
        """
        added = 0
        for ex in examples or []:
            change_type = getattr(ex, "change_type", "") or ""
            language = getattr(ex, "language", "") or ""
            field_name = getattr(ex, "field_name", "") or ""
            if not change_type or not language:
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
            added += 1
        self._rebuild_clusters()
        return added

    def _rebuild_clusters(self) -> None:
        """Derive cluster archetypes from the current patterns."""
        groups = {}
        for p in self.patterns:
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
