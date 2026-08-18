"""What commit is actually running. Computed once, at import.

WHY THIS EXISTS
After pushing 8 commits, `/` reported a hardcoded `"version": "0.1.0"` and
`/health/storage` was byte-identical to before the push. So "is the fix deployed?"
was unanswerable -- not "no", not "yes", *unanswerable*. That is the same
absent-vs-unreachable ambiguity that has now cost this project four times:

  * bitbucket_support.get_file returned "" for 404 AND for 503, so "the spec is
    not there" and "we could not look" were the same value.
  * PropBench cached 403 as "unreachable" and published a false claim.
  * /health/storage could not distinguish zero submissions from wiped state.
  * And here: a live service that cannot say what code it is.

UNKNOWN IS NOT A VERSION
If the SHA cannot be determined, this reports `sha: null` with a `source` of
"unavailable" rather than inventing one or falling back to something that looks
plausible. `source` is part of the answer, not decoration: a SHA read from the
local working tree means something completely different from one Railway injected
into the container, and a reader must be able to tell which they are looking at.
"""
from __future__ import annotations

import os
import subprocess

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_APP_DIR)

# Platform-injected commit SHA, in the order we trust it. Railway sets the first;
# the others are here so a different host does not silently report "unavailable".
_ENV_KEYS = (
    "RAILWAY_GIT_COMMIT_SHA",
    "RENDER_GIT_COMMIT",
    "SOURCE_VERSION",          # Heroku
    "GIT_COMMIT_SHA",
    "GITHUB_SHA",
)


def _from_env() -> tuple:
    for key in _ENV_KEYS:
        value = (os.environ.get(key) or "").strip()
        if value:
            return value, f"env:{key}"
    return "", ""


def _from_git() -> tuple:
    """Only meaningful OUTSIDE a container -- a deploy artifact has no .git.

    Deliberately reported as source "git:working-tree" rather than merged with the
    env case: locally this is the tree you are editing, which may be dirty and is
    NOT evidence about anything deployed.
    """
    try:
        out = subprocess.run(
            ["git", "-C", _REPO, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5)
        if out.returncode != 0:
            return "", ""
        sha = out.stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", _REPO, "status", "--porcelain"],
            capture_output=True, text=True, timeout=5).stdout.strip()
        return sha, "git:working-tree" + ("+dirty" if dirty else "")
    except (OSError, subprocess.SubprocessError):
        return "", ""


def _resolve() -> dict:
    sha, source = _from_env()
    if not sha:
        sha, source = _from_git()
    if not sha:
        return {
            "sha": None,
            "short": None,
            "source": "unavailable",
            "detail": "no platform commit env var is set and there is no git "
                      "checkout, so the running revision cannot be determined. "
                      "This is NOT a claim that the deploy is stale -- it is a "
                      "refusal to guess. Set RAILWAY_GIT_COMMIT_SHA.",
        }
    return {
        "sha": sha,
        "short": sha[:8],
        "source": source,
        "branch": (os.environ.get("RAILWAY_GIT_BRANCH")
                   or os.environ.get("GITHUB_REF_NAME") or None),
        "deployment_id": os.environ.get("RAILWAY_DEPLOYMENT_ID") or None,
    }


# Resolved ONCE. A per-request subprocess in a health check is how a monitor
# becomes the thing that falls over, and the answer cannot change without a
# restart anyway.
BUILD = _resolve()


def build_info() -> dict:
    """The single source for 'what is running'. Callers never assemble their own."""
    return dict(BUILD)


def is_determinable() -> bool:
    return BUILD["sha"] is not None
