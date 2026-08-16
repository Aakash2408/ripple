from __future__ import annotations
"""
ripple/app/activity.py

One activity store, shared by the writer (webhook) and readers (dashboard,
/logs/recent).

WHY THIS EXISTS
---------------
There were TWO independent activity logs:

    app/webhook.py:173   _activity_log = []     <- written on every event
    app/dashboard.py:22  _activity_log = []     <- read by the dashboard

dashboard.py also exposed `log_activity()` and `register_repo()` to populate
its copy, and NOTHING in the codebase ever called either one. So the
dashboard was structurally incapable of showing anything but zero, no matter
how much work the pipeline did -- "0 Repos monitored / 0 PRs created /
0 Breaks detected" while three PRs sat open on GitHub.

Even sharing a list would not have been enough: the dashboard counted
actions named `pr_created` and `breaking_change`, while the pipeline emits
`pr_result` and `breaking_changes_detected`. Two mismatched vocabularies on
top of two disconnected stores.

Both problems are structural, so the fix is structural: a single store with
one vocabulary, and counters derived from the action names the pipeline
actually emits (see _COUNTERS).

PERSISTENCE
-----------
Events are also written to disk, because an in-memory-only log resets on
every Railway redeploy -- which is exactly what erased the successful
08:49 run before it could be inspected, and would leave any visitor to the
dashboard seeing zeros moments after a deploy.
"""

import json
import os
import threading
import time
from pathlib import Path

# Keep the in-memory ring small enough to render fast, large enough to
# cover a full multi-repo pipeline run (which emits ~40 events).
_MAX_EVENTS = 300

_DATA_DIR_CANDIDATES = [
    os.environ.get("RIPPLE_DATA_DIR", ""),
    "/app/data",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"),
    "/tmp/ripple-data",
]

_lock = threading.Lock()
_events: list = []
_loaded = False


def _store_dir() -> Path:
    """First writable candidate directory (same strategy as token_store)."""
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


def _store_path() -> Path:
    return _store_dir() / "activity.json"


def _load() -> None:
    """Load persisted events once, on first use."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        path = _store_path()
        if path.exists():
            data = json.loads(path.read_text())
            if isinstance(data, list):
                _events.extend(data[-_MAX_EVENTS:])
    except (IOError, OSError, ValueError):
        # A corrupt or unreadable log must not take down the webhook.
        pass


def _persist() -> None:
    try:
        _store_path().write_text(json.dumps(_events[-_MAX_EVENTS:]))
    except (IOError, OSError):
        pass


def record(action: str, details: dict = None) -> dict:
    """Record one event. Returns the stored event."""
    with _lock:
        _load()
        event = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "epoch": time.time(),
            "action": action,
        }
        if details:
            event.update(details)
        _events.append(event)
        if len(_events) > _MAX_EVENTS:
            del _events[:-_MAX_EVENTS]
        _persist()
        return event


def recent(limit: int = 50) -> list:
    """Most recent events, oldest first."""
    with _lock:
        _load()
        return list(_events[-limit:])


def all_events() -> list:
    with _lock:
        _load()
        return list(_events)


# Action names the PIPELINE actually emits, mapped to what the dashboard
# wants to show. Previously the dashboard invented its own names and matched
# nothing.
_COUNTERS = {
    "breaks_detected": ("breaking_changes_detected",),
    "prs_created": ("pr_result", "pr_updated_existing"),
    "partial_fixes": ("residual_refs_flagged",),
    "fixes_generated": ("fix_generated",),
}


def counters() -> dict:
    """Derived totals for the dashboard."""
    with _lock:
        _load()
        events = list(_events)

    out = {}

    # breaks_detected sums the per-event `count`, since one push can carry
    # several breaking changes.
    breaks = 0
    for e in events:
        if e.get("action") == "breaking_changes_detected":
            breaks += int(e.get("count", 1) or 1)
    out["breaks_detected"] = breaks

    # Only count PRs that actually produced a URL.
    prs = set()
    for e in events:
        if e.get("action") in _COUNTERS["prs_created"]:
            url = e.get("url", "")
            if url and url != "FAILED":
                prs.add(url)
    out["prs_created"] = len(prs)

    out["partial_fixes"] = sum(
        1 for e in events if e.get("action") in _COUNTERS["partial_fixes"]
    )
    out["fixes_generated"] = sum(
        1 for e in events
        if e.get("action") in _COUNTERS["fixes_generated"] and e.get("changed")
    )
    out["repos_monitored"] = len(monitored_repos())
    return out


def monitored_repos() -> list:
    """Repos Ripple has actually seen or acted on.

    Derived from observed events rather than a register_repo() call nobody
    ever made. Prefers the authoritative installation scope when available.
    """
    with _lock:
        _load()
        events = list(_events)

    repos = set()
    for e in events:
        repo = e.get("repo")
        if repo and "/" in str(repo):
            repos.add(repo)
        # consumer_scope / consumer_repos_found carry a repo list
        for candidate in (e.get("repos") or []):
            if isinstance(candidate, str) and "/" in candidate:
                repos.add(candidate)
    return sorted(repos)


def reset() -> None:
    """Clear the store. Tests only."""
    global _loaded
    with _lock:
        _events.clear()
        _loaded = True
        _persist()
