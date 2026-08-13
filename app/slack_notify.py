from __future__ import annotations
"""
ripple/app/slack_notify.py

Slack Notifications for Ripple.

Sends rich Block Kit messages when Ripple takes action:
- Breaking change detected
- Fix PRs opened
- Consumer registry updated
- CI/CD gate blocked a merge

Requires SLACK_WEBHOOK_URL or SLACK_BOT_TOKEN env var.
Supports both incoming webhooks (simple) and Bot API (rich).
"""

import json
import os
from dataclasses import dataclass
from typing import Optional

import requests


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_DEFAULT_CHANNEL = os.environ.get("SLACK_CHANNEL", "#ripple-alerts")


@dataclass
class SlackConfig:
    """Per-org Slack configuration."""
    webhook_url: str = ""
    bot_token: str = ""
    channel: str = "#ripple-alerts"
    notify_on_breaking: bool = True
    notify_on_fix_pr: bool = True
    notify_on_gate_block: bool = True
    notify_on_consumer_update: bool = False  # noisy, off by default


# ---------------------------------------------------------------------------
# Message builders (Block Kit)
# ---------------------------------------------------------------------------

def build_breaking_change_message(
    repo: str,
    spec_file: str,
    changes: list[dict],
    commit_sha: str,
    commit_url: str = "",
) -> dict:
    """Build a Slack Block Kit message for breaking changes detected."""
    change_lines = []
    for c in changes[:10]:  # limit to 10
        emoji = "🔴" if c.get("severity") == "high" else "🟡"
        change_lines.append(
            f"{emoji} `{c.get('method', 'GET')} {c.get('path', '/')}` — "
            f"{c.get('change_type', 'unknown')}: `{c.get('field_name', '?')}`"
        )

    changes_text = "\n".join(change_lines) or "No details available"
    overflow = f"\n_...and {len(changes) - 10} more_" if len(changes) > 10 else ""

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Breaking Changes Detected",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Repo:*\n`{repo}`"},
                {"type": "mrkdwn", "text": f"*Spec:*\n`{spec_file}`"},
                {"type": "mrkdwn", "text": f"*Changes:*\n{len(changes)}"},
                {"type": "mrkdwn", "text": f"*Commit:*\n`{commit_sha[:7]}`"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Changes:*\n{changes_text}{overflow}",
            },
        },
    ]

    if commit_url:
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "View Commit"},
                    "url": commit_url,
                },
            ],
        })

    return {"blocks": blocks}


def build_fix_pr_message(
    repo: str,
    breaking_change: dict,
    fix_prs: list[dict],
    confidence: float = 0.0,
) -> dict:
    """Build a Slack Block Kit message for fix PRs opened."""
    pr_lines = []
    for pr in fix_prs[:5]:
        pr_lines.append(
            f"• <{pr.get('url', '#')}|{pr.get('consumer_repo', '?')}/{pr.get('file', '?')}> "
            f"({pr.get('language', '?')})"
        )

    prs_text = "\n".join(pr_lines) or "No PRs opened"
    overflow = f"\n_...and {len(fix_prs) - 5} more_" if len(fix_prs) > 5 else ""

    conf_emoji = "🟢" if confidence >= 0.8 else "🟡" if confidence >= 0.5 else "🔴"

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Ripple Opened {len(fix_prs)} Fix PR(s)",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Source:*\n`{repo}`"},
                {
                    "type": "mrkdwn",
                    "text": (
                        f"*Change:*\n`{breaking_change.get('method', '')} "
                        f"{breaking_change.get('path', '')}` — "
                        f"{breaking_change.get('change_type', '')}"
                    ),
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Confidence:*\n{conf_emoji} {confidence:.0%}",
                },
                {"type": "mrkdwn", "text": f"*Consumers Fixed:*\n{len(fix_prs)}"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Fix PRs:*\n{prs_text}{overflow}"},
        },
    ]

    return {"blocks": blocks}


def build_gate_block_message(
    repo: str,
    pr_number: int,
    pr_url: str,
    breaking_count: int,
    unfixed_consumers: int,
) -> dict:
    """Build a Slack Block Kit message for CI/CD gate blocking a merge."""
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "🚦 Ripple Check: Merge Blocked",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Repo:*\n`{repo}`"},
                {"type": "mrkdwn", "text": f"*PR:*\n<{pr_url}|#{pr_number}>"},
                {"type": "mrkdwn", "text": f"*Breaking Changes:*\n{breaking_count}"},
                {
                    "type": "mrkdwn",
                    "text": f"*Unfixed Consumers:*\n{unfixed_consumers}",
                },
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "⚠️ This PR introduces breaking changes with consumers that "
                    "haven't been updated yet. Merge is blocked until all consumers "
                    "have accepted fix PRs."
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "View PR"},
                    "url": pr_url,
                },
            ],
        },
    ]

    return {"blocks": blocks}


def build_consumer_update_message(
    endpoint: str,
    method: str,
    new_consumers: int,
    total_consumers: int,
    org: str,
) -> dict:
    """Build a Slack message for consumer registry updates."""
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"📊 *Consumer Registry Updated*\n"
                    f"`{method.upper()} {endpoint}` now has "
                    f"*{total_consumers}* known consumers "
                    f"(+{new_consumers} new)"
                ),
            },
        },
    ]

    return {"blocks": blocks}


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def send_slack_notification(
    message: dict,
    config: Optional[SlackConfig] = None,
    channel: Optional[str] = None,
) -> bool:
    """
    Send a Slack notification via webhook or Bot API.

    Returns True if sent successfully, False otherwise.
    """
    cfg = config or SlackConfig(
        webhook_url=SLACK_WEBHOOK_URL,
        bot_token=SLACK_BOT_TOKEN,
        channel=SLACK_DEFAULT_CHANNEL,
    )

    target_channel = channel or cfg.channel

    # Prefer webhook (simpler, no OAuth needed)
    if cfg.webhook_url:
        return _send_via_webhook(cfg.webhook_url, message)

    # Fall back to Bot API
    if cfg.bot_token:
        return _send_via_bot_api(cfg.bot_token, target_channel, message)

    # No config -- silently skip (common in dev/test)
    return False


def _send_via_webhook(webhook_url: str, message: dict) -> bool:
    """Send via incoming webhook."""
    try:
        resp = requests.post(
            webhook_url,
            json=message,
            timeout=5,
            headers={"Content-Type": "application/json"},
        )
        return resp.status_code == 200
    except Exception:
        return False


def _send_via_bot_api(bot_token: str, channel: str, message: dict) -> bool:
    """Send via Slack Web API (chat.postMessage)."""
    try:
        payload = {
            "channel": channel,
            "blocks": message.get("blocks", []),
            "text": message.get("text", "Ripple notification"),
        }

        resp = requests.post(
            "https://slack.com/api/chat.postMessage",
            json=payload,
            timeout=5,
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json",
            },
        )
        data = resp.json()
        return data.get("ok", False)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# High-level notification functions (called from webhook.py)
# ---------------------------------------------------------------------------

def notify_breaking_changes(
    repo: str,
    spec_file: str,
    changes: list[dict],
    commit_sha: str,
    commit_url: str = "",
    config: Optional[SlackConfig] = None,
) -> bool:
    """Send notification for breaking changes detected."""
    cfg = config or SlackConfig(webhook_url=SLACK_WEBHOOK_URL)
    if not cfg.notify_on_breaking:
        return False

    message = build_breaking_change_message(
        repo, spec_file, changes, commit_sha, commit_url
    )
    return send_slack_notification(message, cfg)


def notify_fix_prs_opened(
    repo: str,
    breaking_change: dict,
    fix_prs: list[dict],
    confidence: float = 0.0,
    config: Optional[SlackConfig] = None,
) -> bool:
    """Send notification for fix PRs opened."""
    cfg = config or SlackConfig(webhook_url=SLACK_WEBHOOK_URL)
    if not cfg.notify_on_fix_pr:
        return False

    message = build_fix_pr_message(repo, breaking_change, fix_prs, confidence)
    return send_slack_notification(message, cfg)


def notify_gate_blocked(
    repo: str,
    pr_number: int,
    pr_url: str,
    breaking_count: int,
    unfixed_consumers: int,
    config: Optional[SlackConfig] = None,
) -> bool:
    """Send notification for CI/CD gate blocking a merge."""
    cfg = config or SlackConfig(webhook_url=SLACK_WEBHOOK_URL)
    if not cfg.notify_on_gate_block:
        return False

    message = build_gate_block_message(
        repo, pr_number, pr_url, breaking_count, unfixed_consumers
    )
    return send_slack_notification(message, cfg)


def notify_consumer_update(
    endpoint: str,
    method: str,
    new_consumers: int,
    total_consumers: int,
    org: str,
    config: Optional[SlackConfig] = None,
) -> bool:
    """Send notification for consumer registry updates."""
    cfg = config or SlackConfig(webhook_url=SLACK_WEBHOOK_URL)
    if not cfg.notify_on_consumer_update:
        return False

    message = build_consumer_update_message(
        endpoint, method, new_consumers, total_consumers, org
    )
    return send_slack_notification(message, cfg)
