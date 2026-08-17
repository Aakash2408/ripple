from __future__ import annotations
"""
ripple/app/history_learner.py

History Learner — integrates PropBench's Historian insight into Yoke.

On install, scans a repo's git history and builds co-change relationships:
"When file A changed, file B also changed N times."

This powers smarter consumer finding:
- Instead of just grepping for endpoint paths,
- Yoke ALSO checks: "historically, when this spec changed, which other files changed?"

Based on PropBench research findings:
- Co-change history provides 25% file-level recall (3x better than naming alone)
- Knowledge is repo-specific (must learn per-repo, doesn't transfer)
- Needs 100+ commits before becoming useful
- Temporal order doesn't matter — co-occurrence IS the signal
"""

import os
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass
from .languages import UNKNOWN

from .consumer_graph import ConsumerGraph


@dataclass
class CoChangeRelationship:
    """A learned co-change relationship between two files."""
    source_file: str
    target_file: str
    co_change_count: int
    total_source_changes: int
    confidence: float  # co_change_count / total_source_changes
    last_seen: float


class HistoryLearner:
    """
    Learns co-change patterns from git history.
    
    When installed on a repo, scans git log and builds:
    "When spec file X changed, files Y and Z also changed."
    
    This directly feeds into the ConsumerGraph with high-confidence edges.
    
    Based on PropBench findings:
    - 25% file-level recall from co-change alone
    - Needs 100+ commits to be useful
    - Temporal order irrelevant (just count co-occurrences)
    """
    
    def __init__(self, min_confidence: float = 0.2, min_co_changes: int = 2):
        self.min_confidence = min_confidence
        self.min_co_changes = min_co_changes
        self.relationships: dict[str, list[CoChangeRelationship]] = defaultdict(list)
        self.file_change_counts: dict[str, int] = defaultdict(int)
    
    def learn_from_repo(self, repo_path: str, since: str = "12 months ago", spec_patterns: list[str] = None) -> dict:
        """
        Scan a repo's git history and learn co-change patterns.
        
        Focuses on changes that involve spec/contract files:
        - openapi.yaml, swagger.json, *.proto, schema.graphql, etc.
        
        Returns stats about what was learned.
        """
        if spec_patterns is None:
            spec_patterns = [
                "openapi", "swagger", ".proto", "schema.graphql",
                "schema.prisma", "migration", "api-spec",
            ]
        
        # Get commits with their file lists
        commits = self._get_commits(repo_path, since)
        
        spec_commits = 0
        total_relationships = 0
        
        for files in commits:
            if len(files) < 2:
                continue
            
            # Update change counts
            for f in files:
                self.file_change_counts[f] += 1
            
            # Check if any file is a spec/contract file
            spec_files = [f for f in files if self._is_spec_file(f, spec_patterns)]
            
            if spec_files:
                spec_commits += 1
                # Record co-change: spec file → every other file in the commit
                for spec_file in spec_files:
                    non_spec_files = [f for f in files if f != spec_file]
                    for target in non_spec_files:
                        self._record_co_change(spec_file, target)
                        total_relationships += 1
            else:
                # Even without spec files, record all co-changes
                # (useful for finding related files within the same domain)
                for i, source in enumerate(files):
                    for j, target in enumerate(files):
                        if i != j:
                            self._record_co_change(source, target)
        
        return {
            "total_commits": len(commits),
            "spec_commits": spec_commits,
            "files_tracked": len(self.file_change_counts),
            "relationships_learned": total_relationships,
            "high_confidence_edges": sum(
                1 for rels in self.relationships.values()
                for r in rels if r.confidence >= 0.5
            ),
        }
    
    def predict_consumers(self, changed_file: str, top_n: int = 10) -> list[tuple[str, float, str]]:
        """
        Given a changed file, predict what other files likely need updating.
        
        Returns: [(file_path, confidence, reason)]
        """
        relationships = self.relationships.get(changed_file, [])
        
        # Filter by minimum thresholds
        valid = [
            r for r in relationships
            if r.co_change_count >= self.min_co_changes
            and r.confidence >= self.min_confidence
        ]
        
        # Sort by confidence descending
        valid.sort(key=lambda r: r.confidence, reverse=True)
        
        return [
            (
                r.target_file,
                r.confidence,
                f"Co-changed {r.co_change_count}/{r.total_source_changes} times ({r.confidence:.0%})"
            )
            for r in valid[:top_n]
        ]
    
    def feed_into_consumer_graph(self, graph: ConsumerGraph, endpoint_path: str, method: str):
        """
        Feed learned relationships into the ConsumerGraph.
        
        For each spec file that mentions this endpoint, find its
        co-change partners and register them as consumers.
        """
        # Find spec files that likely define this endpoint
        for source_file, relationships in self.relationships.items():
            if not self._is_spec_file(source_file, ["openapi", "swagger", ".proto", "schema"]):
                continue
            
            # Register high-confidence co-change partners as consumers
            for rel in relationships:
                if rel.confidence >= self.min_confidence and rel.co_change_count >= self.min_co_changes:
                    # Infer language from file extension
                    lang = self._detect_language(rel.target_file)
                    # Was `if lang:` -- correct only while a miss returned None.
                    # "unknown" is truthy, so that guard silently stopped
                    # filtering the moment the sentinel was unified.
                    if lang != UNKNOWN:
                        graph.register_consumer(
                            path=endpoint_path,
                            method=method,
                            consumer_repo="",  # same repo
                            consumer_file=rel.target_file,
                            language=lang,
                        )
    
    def _record_co_change(self, source: str, target: str):
        """Record a co-change between source and target."""
        # Find existing relationship or create new
        existing = None
        for rel in self.relationships[source]:
            if rel.target_file == target:
                existing = rel
                break
        
        if existing:
            existing.co_change_count += 1
            existing.total_source_changes = self.file_change_counts[source]
            existing.confidence = existing.co_change_count / max(1, existing.total_source_changes)
            existing.last_seen = time.time()
        else:
            self.relationships[source].append(CoChangeRelationship(
                source_file=source,
                target_file=target,
                co_change_count=1,
                total_source_changes=self.file_change_counts[source],
                confidence=1.0 / max(1, self.file_change_counts[source]),
                last_seen=time.time(),
            ))
    
    def _get_commits(self, repo_path: str, since: str) -> list[list[str]]:
        """Get list of commits, each as a list of changed files."""
        try:
            result = subprocess.run(
                ["git", "-C", repo_path, "log", f"--since={since}",
                 "--no-merges", "--name-only", "--pretty=format:---COMMIT---"],
                capture_output=True, text=True, timeout=30,
            )
            
            commits = []
            current_files = []
            
            for line in result.stdout.split("\n"):
                if line == "---COMMIT---":
                    if current_files:
                        commits.append(current_files)
                    current_files = []
                elif line.strip():
                    current_files.append(line.strip())
            
            if current_files:
                commits.append(current_files)
            
            return commits
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []
    
    def _is_spec_file(self, filepath: str, patterns: list[str]) -> bool:
        """Check if a file is a spec/contract file."""
        lower = filepath.lower()
        return any(p in lower for p in patterns)
    
    def _detect_language(self, filepath: str) -> str:
        """Delegates to app/languages.py.

        Returned None for a miss where every other implementation returned
        "unknown", so the caller's `if lang:` guard meant something different
        here than the same line would elsewhere. One sentinel now, and the
        caller asks is_known() rather than relying on truthiness.
        """
        from .languages import detect
        return detect(filepath)
    
    def stats(self) -> str:
        """Return human-readable stats."""
        total_rels = sum(len(rels) for rels in self.relationships.values())
        high_conf = sum(
            1 for rels in self.relationships.values()
            for r in rels if r.confidence >= 0.5
        )
        return (
            f"HistoryLearner: {len(self.file_change_counts)} files, "
            f"{total_rels} relationships, {high_conf} high-confidence"
        )
