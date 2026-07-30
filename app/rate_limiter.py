from __future__ import annotations
"""
ripple/app/rate_limiter.py

Rate Limiting & Job Queue — prevents API abuse and manages concurrent processing.

Features:
1. Per-org rate limiting (max N webhook events per minute)
2. Per-org concurrent PR limits (don't open 50 PRs simultaneously)
3. Job queue for high-throughput orgs (process sequentially when overloaded)
4. GitHub API rate limit awareness (respect X-RateLimit-Remaining)

Architecture:
- In-memory for now (single-process Railway deployment)
- Can swap to Redis when scaling to multiple workers

Limits (configurable via .ripple.yaml settings):
- max_prs_per_push: 10 (default) — don't overwhelm with PRs
- max_events_per_minute: 30 — per org
- max_concurrent_prs: 5 — per org, across all pushes
- cooldown_after_burst: 60s — wait after hitting limit
"""

import time
from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock


@dataclass
class RateLimitState:
    """Per-org rate limit tracking."""
    events: list[float] = field(default_factory=list)  # timestamps
    prs_opened: int = 0
    last_reset: float = field(default_factory=time.time)
    is_cooling_down: bool = False
    cooldown_until: float = 0.0


class RateLimiter:
    """
    In-memory rate limiter for webhook events.
    
    Thread-safe for concurrent FastAPI requests.
    """
    
    def __init__(
        self,
        max_events_per_minute: int = 30,
        max_concurrent_prs: int = 5,
        cooldown_seconds: int = 60,
    ):
        self.max_events_per_minute = max_events_per_minute
        self.max_concurrent_prs = max_concurrent_prs
        self.cooldown_seconds = cooldown_seconds
        self._states: dict[str, RateLimitState] = defaultdict(RateLimitState)
        self._lock = Lock()
    
    def check(self, org: str) -> tuple[bool, str]:
        """
        Check if an org is allowed to process a webhook event.
        
        Returns: (allowed: bool, reason: str)
        """
        with self._lock:
            state = self._states[org]
            now = time.time()
            
            # Check cooldown
            if state.is_cooling_down and now < state.cooldown_until:
                remaining = int(state.cooldown_until - now)
                return False, f"Rate limited. Cooling down for {remaining}s."
            elif state.is_cooling_down and now >= state.cooldown_until:
                state.is_cooling_down = False
            
            # Clean old events (older than 60s)
            state.events = [t for t in state.events if now - t < 60]
            
            # Check events per minute
            if len(state.events) >= self.max_events_per_minute:
                state.is_cooling_down = True
                state.cooldown_until = now + self.cooldown_seconds
                return False, f"Rate limit: {self.max_events_per_minute} events/minute exceeded."
            
            # Check concurrent PRs
            if state.prs_opened >= self.max_concurrent_prs:
                return False, f"Concurrent PR limit: {self.max_concurrent_prs} PRs in flight."
            
            # Allowed — record the event
            state.events.append(now)
            return True, "ok"
    
    def record_pr_opened(self, org: str):
        """Record that a PR was opened for rate tracking."""
        with self._lock:
            self._states[org].prs_opened += 1
    
    def record_pr_completed(self, org: str):
        """Record that a PR creation finished (success or failure)."""
        with self._lock:
            state = self._states[org]
            state.prs_opened = max(0, state.prs_opened - 1)
    
    def reset(self, org: str):
        """Reset rate limit state for an org."""
        with self._lock:
            self._states[org] = RateLimitState()
    
    def stats(self, org: str) -> dict:
        """Get rate limit stats for an org."""
        with self._lock:
            state = self._states[org]
            now = time.time()
            recent_events = [t for t in state.events if now - t < 60]
            return {
                "org": org,
                "events_last_minute": len(recent_events),
                "max_events_per_minute": self.max_events_per_minute,
                "prs_in_flight": state.prs_opened,
                "max_concurrent_prs": self.max_concurrent_prs,
                "is_cooling_down": state.is_cooling_down,
                "cooldown_remaining": max(0, int(state.cooldown_until - now)) if state.is_cooling_down else 0,
            }


class GitHubRateLimitTracker:
    """
    Tracks GitHub API rate limit from response headers.
    
    Pauses requests when approaching the limit.
    """
    
    def __init__(self, buffer: int = 100):
        self.remaining: int = 5000  # GitHub default
        self.limit: int = 5000
        self.reset_at: float = 0
        self.buffer = buffer  # Stop when this many remaining
        self._lock = Lock()
    
    def update_from_headers(self, headers: dict):
        """Update state from GitHub API response headers."""
        with self._lock:
            if "X-RateLimit-Remaining" in headers:
                self.remaining = int(headers["X-RateLimit-Remaining"])
            if "X-RateLimit-Limit" in headers:
                self.limit = int(headers["X-RateLimit-Limit"])
            if "X-RateLimit-Reset" in headers:
                self.reset_at = float(headers["X-RateLimit-Reset"])
    
    def can_proceed(self) -> tuple[bool, str]:
        """Check if we have enough rate limit budget to proceed."""
        with self._lock:
            if self.remaining <= self.buffer:
                wait_time = max(0, int(self.reset_at - time.time()))
                return False, f"GitHub rate limit low ({self.remaining} remaining). Resets in {wait_time}s."
            return True, "ok"
    
    def stats(self) -> dict:
        """Get current rate limit stats."""
        with self._lock:
            return {
                "remaining": self.remaining,
                "limit": self.limit,
                "reset_at": self.reset_at,
                "buffer": self.buffer,
                "can_proceed": self.remaining > self.buffer,
            }


# Singleton instances
_rate_limiter = RateLimiter()
_github_rate_tracker = GitHubRateLimitTracker()


def get_rate_limiter() -> RateLimiter:
    """Get the global rate limiter instance."""
    return _rate_limiter


def get_github_rate_tracker() -> GitHubRateLimitTracker:
    """Get the global GitHub rate limit tracker."""
    return _github_rate_tracker
