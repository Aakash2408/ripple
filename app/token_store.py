"""
ripple/app/token_store.py

Persistent token storage for OAuth connections.
Stores tokens in a JSON file so they survive Railway container restarts/redeploys.

Railway persistent storage: use /app/data/ directory (persists between deploys
if Railway Volume is attached) or fall back to local file in the app directory.
"""

import json
import os
import threading
from pathlib import Path
from typing import Optional

# Storage location — Railway Volume mount preferred, falls back to app directory
_DATA_DIR_CANDIDATES = [
    os.environ.get("RIPPLE_DATA_DIR", ""),
    "/app/data",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"),
    "/tmp/ripple-data",
]

def _find_store_dir() -> Path:
    """Find a writable storage directory."""
    for candidate in _DATA_DIR_CANDIDATES:
        if not candidate:
            continue
        p = Path(candidate)
        try:
            p.mkdir(parents=True, exist_ok=True)
            # Test write
            test_file = p / ".write_test"
            test_file.write_text("ok")
            test_file.unlink()
            return p
        except (IOError, OSError):
            continue
    # Last resort
    return Path("/tmp/ripple-data")

_STORE_DIR = _find_store_dir()
_STORE_FILE = _STORE_DIR / "tokens.json"
_lock = threading.Lock()

# In-memory cache (loaded from disk on startup)
_store: dict = {
    "gitlab_users": {},       # user_id → {token, username, refresh_token}
    "gitlab_projects": {},    # project_id → {token, webhook_id, name}
    "bitbucket_users": {},    # user_id → {token, username, refresh_token}
    "bitbucket_repos": {},    # repo_slug → {token, webhook_uuid, name}
    "github_installations": {},  # installation_id → {repos, ...}
}


def _ensure_dir():
    """Create data directory if it doesn't exist."""
    _STORE_DIR.mkdir(parents=True, exist_ok=True)


def _load():
    """Load store from disk into memory."""
    global _store
    try:
        if _STORE_FILE.exists():
            with open(_STORE_FILE, "r") as f:
                loaded = json.load(f)
                # Merge with defaults (in case new keys added)
                for key in _store:
                    if key in loaded:
                        _store[key] = loaded[key]
    except (json.JSONDecodeError, IOError):
        pass  # Keep defaults if file is corrupted


def _save():
    """Persist current store to disk."""
    try:
        _ensure_dir()
        # Write atomically (write to temp, then rename)
        tmp_file = _STORE_FILE.with_suffix(".tmp")
        with open(tmp_file, "w") as f:
            json.dump(_store, f, indent=2)
        tmp_file.rename(_STORE_FILE)
    except IOError as e:
        print(f"[token_store] WARNING: Failed to save tokens: {e}")


# === Public API ===

def init():
    """Initialize store — call on app startup."""
    _ensure_dir()
    _load()
    count = sum(len(v) for v in _store.values() if isinstance(v, dict))
    print(f"[token_store] Loaded {count} entries from {_STORE_FILE}")


# --- GitLab ---

def save_gitlab_user(user_id: str, token: str, username: str, refresh_token: str = ""):
    """Save a GitLab user's OAuth token."""
    with _lock:
        _store["gitlab_users"][user_id] = {
            "token": token,
            "username": username,
            "refresh_token": refresh_token,
        }
        _save()


def save_gitlab_project(project_id: int, token: str, webhook_id: int, name: str):
    """Save a monitored GitLab project."""
    with _lock:
        _store["gitlab_projects"][str(project_id)] = {
            "token": token,
            "webhook_id": webhook_id,
            "name": name,
        }
        _save()


def get_gitlab_token_for_project(project_id: int) -> str:
    """Get stored OAuth token for a monitored GitLab project."""
    info = _store["gitlab_projects"].get(str(project_id))
    return info["token"] if info else ""


def get_gitlab_users() -> dict:
    """Get all connected GitLab users."""
    return _store["gitlab_users"]


def get_gitlab_projects() -> dict:
    """Get all monitored GitLab projects."""
    return _store["gitlab_projects"]


# --- Bitbucket ---

def save_bitbucket_user(user_id: str, token: str, username: str, refresh_token: str = ""):
    """Save a Bitbucket user's OAuth token."""
    with _lock:
        _store["bitbucket_users"][user_id] = {
            "token": token,
            "username": username,
            "refresh_token": refresh_token,
        }
        _save()


def save_bitbucket_repo(repo_slug: str, token: str, webhook_uuid: str, name: str):
    """Save a monitored Bitbucket repo."""
    with _lock:
        _store["bitbucket_repos"][repo_slug] = {
            "token": token,
            "webhook_uuid": webhook_uuid,
            "name": name,
        }
        _save()


def get_bitbucket_token_for_repo(repo_slug: str) -> str:
    """Get stored OAuth token for a monitored Bitbucket repo."""
    info = _store["bitbucket_repos"].get(repo_slug)
    return info["token"] if info else ""


def get_bitbucket_users() -> dict:
    """Get all connected Bitbucket users."""
    return _store["bitbucket_users"]


def get_bitbucket_repos() -> dict:
    """Get all monitored Bitbucket repos."""
    return _store["bitbucket_repos"]


# --- Stats ---

def get_stats() -> dict:
    """Get connection stats for dashboard."""
    return {
        "gitlab_users": len(_store["gitlab_users"]),
        "gitlab_projects": len(_store["gitlab_projects"]),
        "bitbucket_users": len(_store["bitbucket_users"]),
        "bitbucket_repos": len(_store["bitbucket_repos"]),
    }
