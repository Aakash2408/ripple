"""
ripple/app/consumer_graph.py

Persistent Consumer Dependency Graph.

THIS IS THE MOAT. Instead of grepping on every change, we maintain a learned
graph of who depends on what. The graph improves with every PR observed.

After 3 months of usage, Ripple knows:
- Which repos call which endpoints
- How often (confidence = call frequency)
- When the relationship was last confirmed
- Which relationships are stale (no calls in 90 days)

This is proprietary data that grows with usage — can't be replicated by
a competitor without the same install base.
"""

import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


@dataclass
class ConsumerEdge:
    """A known dependency: consumer_file calls endpoint."""
    consumer_repo: str
    consumer_file: str
    language: str
    endpoint_path: str      # e.g., "/v1/payments"
    endpoint_method: str    # e.g., "post"
    confidence: float       # 0.0-1.0 (based on observation frequency)
    observation_count: int  # how many times we've confirmed this
    first_seen: float       # timestamp
    last_seen: float        # timestamp
    last_fix_pr: Optional[str] = None  # URL of last Ripple PR for this edge


@dataclass
class APINode:
    """A known API endpoint with its consumers."""
    spec_repo: str
    spec_file: str
    path: str               # e.g., "/v1/payments"
    method: str             # e.g., "post"
    consumers: list[ConsumerEdge] = field(default_factory=list)
    last_change: Optional[float] = None
    change_count: int = 0


class ConsumerGraph:
    """
    Persistent dependency graph for an org.
    
    Stored as JSON. Loaded on startup, updated on every event.
    This is the data asset that makes Ripple defensible.
    """
    
    def __init__(self, org_id: str, storage_dir: str = "/tmp/ripple_graphs"):
        self.org_id = org_id
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.graph_file = self.storage_dir / f"{org_id}.json"
        self.nodes: dict[str, APINode] = {}  # key = "method:path"
        self._load()
    
    def _key(self, method: str, path: str) -> str:
        return f"{method.upper()}:{path}"
    
    def register_api(self, spec_repo: str, spec_file: str, path: str, method: str):
        """Register a known API endpoint."""
        key = self._key(method, path)
        if key not in self.nodes:
            self.nodes[key] = APINode(
                spec_repo=spec_repo, spec_file=spec_file,
                path=path, method=method
            )
        self._save()
    
    def register_consumer(
        self, path: str, method: str,
        consumer_repo: str, consumer_file: str, language: str
    ):
        """Register or strengthen a consumer relationship."""
        key = self._key(method, path)
        now = time.time()
        
        if key not in self.nodes:
            self.nodes[key] = APINode(
                spec_repo="", spec_file="", path=path, method=method
            )
        
        node = self.nodes[key]
        
        # Find existing edge or create new one
        existing = None
        for edge in node.consumers:
            if edge.consumer_repo == consumer_repo and edge.consumer_file == consumer_file:
                existing = edge
                break
        
        if existing:
            existing.observation_count += 1
            existing.last_seen = now
            existing.confidence = min(1.0, existing.confidence + 0.05)
        else:
            node.consumers.append(ConsumerEdge(
                consumer_repo=consumer_repo,
                consumer_file=consumer_file,
                language=language,
                endpoint_path=path,
                endpoint_method=method,
                confidence=0.5,  # starts at 50%, grows with observations
                observation_count=1,
                first_seen=now,
                last_seen=now,
            ))
        
        self._save()
    
    def get_consumers(self, path: str, method: str, min_confidence: float = 0.3) -> list[ConsumerEdge]:
        """Get all known consumers of an endpoint, ranked by confidence."""
        key = self._key(method, path)
        node = self.nodes.get(key)
        if not node:
            return []
        
        # Filter by confidence and sort descending
        consumers = [c for c in node.consumers if c.confidence >= min_confidence]
        consumers.sort(key=lambda c: c.confidence, reverse=True)
        
        # Decay stale relationships
        now = time.time()
        for c in consumers:
            days_since_seen = (now - c.last_seen) / 86400
            if days_since_seen > 90:
                c.confidence *= 0.8  # decay old relationships
        
        return consumers
    
    def record_change(self, path: str, method: str):
        """Record that an API endpoint changed."""
        key = self._key(method, path)
        if key in self.nodes:
            self.nodes[key].last_change = time.time()
            self.nodes[key].change_count += 1
            self._save()
    
    def record_fix_pr(self, path: str, method: str, consumer_repo: str, consumer_file: str, pr_url: str):
        """Record that we created a fix PR for a consumer."""
        key = self._key(method, path)
        node = self.nodes.get(key)
        if node:
            for edge in node.consumers:
                if edge.consumer_repo == consumer_repo and edge.consumer_file == consumer_file:
                    edge.last_fix_pr = pr_url
                    edge.confidence = min(1.0, edge.confidence + 0.1)  # confirmed relationship
                    self._save()
                    break
    
    def stats(self) -> dict:
        """Return graph statistics."""
        total_edges = sum(len(n.consumers) for n in self.nodes.values())
        high_conf = sum(1 for n in self.nodes.values() for c in n.consumers if c.confidence > 0.7)
        return {
            "org": self.org_id,
            "endpoints": len(self.nodes),
            "consumer_edges": total_edges,
            "high_confidence_edges": high_conf,
            "repos": len(set(c.consumer_repo for n in self.nodes.values() for c in n.consumers)),
        }
    
    def _save(self):
        """Persist graph to disk."""
        data = {}
        for key, node in self.nodes.items():
            data[key] = {
                "spec_repo": node.spec_repo,
                "spec_file": node.spec_file,
                "path": node.path,
                "method": node.method,
                "last_change": node.last_change,
                "change_count": node.change_count,
                "consumers": [asdict(c) for c in node.consumers],
            }
        with open(self.graph_file, "w") as f:
            json.dump(data, f, indent=2)
    
    def _load(self):
        """Load graph from disk."""
        if not self.graph_file.exists():
            return
        try:
            with open(self.graph_file) as f:
                data = json.load(f)
            for key, node_data in data.items():
                consumers = [ConsumerEdge(**c) for c in node_data.pop("consumers", [])]
                self.nodes[key] = APINode(**node_data, consumers=consumers)
        except (json.JSONDecodeError, TypeError):
            pass  # start fresh if corrupted
