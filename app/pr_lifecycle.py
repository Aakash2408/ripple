"""
ripple/app/pr_lifecycle.py

PR Lifecycle Labels — tracks upstream source CR status and updates
fix PRs accordingly.

States:
  PENDING  — source CR pushed but not merged (fix PR is a heads-up)
  MERGED   — source CR merged (fix PR is safe to merge now)
  REVERTED — source CR was reverted (fix PR should be closed)

How it works:
  1. When Ripple opens a fix PR, it tags it with `ripple:pending-upstream`
  2. A webhook listener watches the source repo for merge/revert events
  3. On merge: updates fix PRs to `ripple:ready-to-merge` + adds comment
  4. On revert: closes fix PRs + adds `ripple:upstream-reverted` label

The PR body includes an "Upstream Status" section that gets updated.
"""

from __future__ import annotations
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


class UpstreamStatus(str, Enum):
    PENDING = "pending"
    MERGED = "merged"
    REVERTED = "reverted"


@dataclass
class SourceChange:
    """Tracks the source commit/PR that triggered fix PRs."""
    repo: str                    # e.g. "org/api-repo"
    commit_sha: str              # the breaking change commit
    pr_number: Optional[int]     # source PR number (if available)
    pr_url: Optional[str]        # source PR URL
    title: str                   # PR/commit title
    status: UpstreamStatus = UpstreamStatus.PENDING
    merged_at: Optional[datetime] = None
    reverted_at: Optional[datetime] = None


@dataclass
class TrackedFixPR:
    """A fix PR that Ripple opened, linked to its source change."""
    repo: str                    # consumer repo where PR was opened
    pr_number: int
    pr_url: str
    source: SourceChange
    labels: list[str] = field(default_factory=list)


# === Labels ===

LABEL_PENDING = "ripple:pending-upstream"
LABEL_READY = "ripple:ready-to-merge"
LABEL_REVERTED = "ripple:upstream-reverted"
LABEL_AUTO_FIX = "ripple:auto-fix"


def get_labels_for_status(status: UpstreamStatus) -> list[str]:
    """Get the labels to apply based on upstream status."""
    base = [LABEL_AUTO_FIX]
    if status == UpstreamStatus.PENDING:
        return base + [LABEL_PENDING]
    elif status == UpstreamStatus.MERGED:
        return base + [LABEL_READY]
    elif status == UpstreamStatus.REVERTED:
        return base + [LABEL_REVERTED]
    return base


# === PR Body Sections ===

def format_upstream_status_section(source: SourceChange) -> str:
    """Generate the upstream status section for a PR body."""
    if source.status == UpstreamStatus.PENDING:
        icon = "⏳"
        action = "Review this fix now, merge after upstream lands."
        status_text = "PENDING (source not yet merged)"
    elif source.status == UpstreamStatus.MERGED:
        icon = "✅"
        action = "Safe to merge this fix now."
        status_text = "MERGED"
    elif source.status == UpstreamStatus.REVERTED:
        icon = "🔄"
        action = "Upstream was reverted — this fix PR is no longer needed."
        status_text = "REVERTED (fix no longer needed)"
    else:
        icon = "❓"
        action = ""
        status_text = "UNKNOWN"

    pr_ref = f"[PR #{source.pr_number}]({source.pr_url})" if source.pr_url else f"commit `{source.commit_sha[:8]}`"

    return f"""### {icon} Upstream Status: {status_text}

| Field | Value |
|-------|-------|
| **Source** | `{source.repo}` {pr_ref} |
| **Change** | {source.title} |
| **Status** | {status_text} |

**{action}**
"""


def format_status_update_comment(source: SourceChange) -> str:
    """Generate a comment to post when upstream status changes."""
    if source.status == UpstreamStatus.MERGED:
        return (
            f"✅ **Upstream merged!** The source change in `{source.repo}` "
            f"has been merged. This fix PR is now safe to merge.\n\n"
            f"_Updated by [Ripple](https://ripple-cnn.pages.dev)_"
        )
    elif source.status == UpstreamStatus.REVERTED:
        return (
            f"🔄 **Upstream reverted.** The source change in `{source.repo}` "
            f"was reverted. This fix PR is no longer needed and will be closed.\n\n"
            f"_Updated by [Ripple](https://ripple-cnn.pages.dev)_"
        )
    return ""


# === State Machine ===

def on_source_merged(source: SourceChange, fix_prs: list[TrackedFixPR]) -> list[dict]:
    """
    Called when the source CR is merged.
    Returns list of actions to take on fix PRs.
    """
    source.status = UpstreamStatus.MERGED
    source.merged_at = datetime.utcnow()

    actions = []
    for pr in fix_prs:
        actions.append({
            "action": "update_labels",
            "repo": pr.repo,
            "pr_number": pr.pr_number,
            "remove_labels": [LABEL_PENDING],
            "add_labels": [LABEL_READY],
        })
        actions.append({
            "action": "add_comment",
            "repo": pr.repo,
            "pr_number": pr.pr_number,
            "body": format_status_update_comment(source),
        })

    return actions


def on_source_reverted(source: SourceChange, fix_prs: list[TrackedFixPR]) -> list[dict]:
    """
    Called when the source CR is reverted.
    Returns list of actions to take on fix PRs (close them).
    """
    source.status = UpstreamStatus.REVERTED
    source.reverted_at = datetime.utcnow()

    actions = []
    for pr in fix_prs:
        actions.append({
            "action": "add_comment",
            "repo": pr.repo,
            "pr_number": pr.pr_number,
            "body": format_status_update_comment(source),
        })
        actions.append({
            "action": "update_labels",
            "repo": pr.repo,
            "pr_number": pr.pr_number,
            "remove_labels": [LABEL_PENDING],
            "add_labels": [LABEL_REVERTED],
        })
        actions.append({
            "action": "close_pr",
            "repo": pr.repo,
            "pr_number": pr.pr_number,
        })

    return actions


# === Storage (in-memory, swap to DB for production) ===

# Maps source commit SHA -> SourceChange + fix PRs
_tracked_sources: dict[str, tuple[SourceChange, list[TrackedFixPR]]] = {}


def track_fix_pr(source: SourceChange, fix_pr: TrackedFixPR):
    """Register a new fix PR for tracking."""
    key = source.commit_sha
    if key not in _tracked_sources:
        _tracked_sources[key] = (source, [])
    _tracked_sources[key][1].append(fix_pr)


def get_tracked_source(commit_sha: str) -> Optional[tuple[SourceChange, list[TrackedFixPR]]]:
    """Look up tracked source by commit SHA."""
    return _tracked_sources.get(commit_sha)


def get_all_pending() -> list[tuple[SourceChange, list[TrackedFixPR]]]:
    """Get all sources with pending fix PRs."""
    return [
        (source, prs)
        for source, prs in _tracked_sources.values()
        if source.status == UpstreamStatus.PENDING
    ]
