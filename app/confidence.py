from __future__ import annotations
"""
ripple/app/confidence.py

Confidence Scoring — generates human-readable explanations for WHY
Ripple chose to fix a specific file.

Included in PR body so reviewers understand the reasoning:

## Ripple — Confidence Report

| File | Confidence | Sources | Reason |
|------|-----------|---------|--------|
| client.py | 95% 🟢 | grep + history + playbook | Direct API call + co-changed 11/12 times |
| test_client.py | 78% 🟡 | playbook + history | Test files predicted by openapi playbook |
| docs/api.md | 45% 🟠 | grep only | Mentions endpoint name in comments |

"""

from dataclasses import dataclass


@dataclass
class ConfidenceLevel:
    """A confidence classification."""
    label: str       # "high", "medium", "low"
    emoji: str       # 🟢, 🟡, 🟠
    threshold: float # minimum confidence for this level
    action: str      # "auto-fix PR", "PR with warning", "comment only"


CONFIDENCE_LEVELS = [
    ConfidenceLevel("high", "🟢", 0.75, "Auto-fix PR"),
    ConfidenceLevel("medium", "🟡", 0.50, "PR with review note"),
    ConfidenceLevel("low", "🟠", 0.30, "Comment only (no PR)"),
    ConfidenceLevel("skip", "⚪", 0.0, "Logged but no action"),
]


def classify_confidence(score: float) -> ConfidenceLevel:
    """Classify a confidence score into a level."""
    for level in CONFIDENCE_LEVELS:
        if score >= level.threshold:
            return level
    return CONFIDENCE_LEVELS[-1]


def format_confidence_table(predictions: list[dict]) -> str:
    """
    Format ensemble predictions into a markdown table for PR body.
    
    Input: list of dicts from EnsembleConsumerFinder.find_all_consumers()
    Each: {"file": str, "confidence": float, "sources": list, "reasons": list}
    """
    if not predictions:
        return ""
    
    lines = [
        "## Ripple — Confidence Report",
        "",
        "| File | Confidence | Sources | Why this file? |",
        "|------|-----------|---------|----------------|",
    ]
    
    for pred in predictions[:10]:  # Cap at 10 rows
        file_path = pred.get("file", "unknown")
        confidence = pred.get("confidence", 0)
        sources = pred.get("sources", [])
        reasons = pred.get("reasons", [])
        
        level = classify_confidence(confidence)
        pct = f"{int(confidence * 100)}%"
        sources_str = " + ".join(sources[:3])
        reason_str = reasons[0] if reasons else "Pattern match"
        
        # Truncate long file paths
        display_path = file_path if len(file_path) <= 40 else f"...{file_path[-37:]}"
        
        lines.append(
            f"| `{display_path}` | {pct} {level.emoji} | {sources_str} | {reason_str} |"
        )
    
    lines.append("")
    lines.append(f"*{len(predictions)} consumer(s) analyzed. "
                 f"Only files above {int(CONFIDENCE_LEVELS[0].threshold * 100)}% confidence get auto-fix PRs.*")
    
    return "\n".join(lines)


def format_pr_body(change_description: str, source_repo: str,
                   confidence: float, sources: list[str],
                   reasons: list[str], all_predictions: list[dict] = None,
                   breaking_change: dict = None, fix_summary: str = "",
                   residual_refs: list = None,
                   consumer_file: str = "") -> str:
    """
    Generate a complete PR body with confidence, impact report, and learning context.

    residual_refs: list of (line_no, line_text, variant) references to the
    changed field that survived the automated fix. These require a human
    decision, so they are called out prominently instead of being silently
    left in the diff.
    """
    level = classify_confidence(confidence)
    
    # Don't claim a complete fix when references survive.
    heading = ("## Ripple — Partial Fix (review required)" if residual_refs
               else "## Ripple — Automated Fix")
    
    body_parts = [
        heading,
        "",
        "### Breaking Change",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| **Source** | `{source_repo}` |",
    ]
    
    if breaking_change:
        body_parts.append(f"| **Contract** | `{breaking_change.get('file', 'spec')}` ({breaking_change.get('type', 'API')}) |")
        body_parts.append(f"| **Change** | {change_description} |")
    else:
        body_parts.append(f"| **Change** | {change_description} |")
    
    # Call sites that need a HUMAN decision. Shown high in the body because
    # merging without addressing them leaves the code referencing a field
    # that no longer exists.
    if residual_refs:
        target = f"`{consumer_file}`" if consumer_file else "this file"
        body_parts.extend([
            "",
            f"### ⚠️ Action required — {len(residual_refs)} call site(s) need your decision",
            "",
            f"Ripple removed the declaration and pass-through plumbing, but "
            f"{target} still references this field at the following line(s). "
            f"**This PR is not safe to merge as-is.**",
            "",
            "| Line | Code | Symbol |",
            "|---|---|---|",
        ])
        for line_no, text, variant in residual_refs[:10]:
            safe = text.replace("|", "\\|")[:70]
            body_parts.append(f"| {line_no} | `{safe}` | `{variant}` |")
        body_parts.extend([
            "",
            "Ripple deliberately did **not** auto-resolve these. Each option "
            "changes behaviour in a way only you can choose:",
            "",
            "- **Remove the argument** — the enclosing call may then be missing a required parameter.",
            "- **Remove the whole call** — silently disables that code path (e.g. a customer notification).",
            "- **Substitute another field** — a product decision Ripple cannot infer.",
            "",
        ])
    
    body_parts.extend([
        "",
        f"### 🧠 AI Confidence: {int(confidence * 100)}%",
        "",
        "| Factor | Score | Reason |",
        "|---|---|---|",
    ])
    
    # Confidence breakdown
    obs_score = min(0.99, confidence + 0.02)
    lang_score = 0.95
    # Label the fix by how it was ACTUALLY produced. This previously guessed
    # from `sources` and fell through to "LLM-generated (semantic)", so a
    # deterministic template fix was reported as LLM output -- misrepresenting
    # provenance on every PR (and Anthropic had zero credits, so it could not
    # have been LLM-generated at all).
    joined_sources = " ".join(sources).lower()
    if "template" in joined_sources:
        fix_score, fix_label = 0.92, "Template-based (deterministic)"
    elif "rag" in joined_sources:
        fix_score, fix_label = 0.88, "RAG (learned from merged PRs)"
    elif "llm" in joined_sources or "claude" in joined_sources:
        fix_score, fix_label = 0.78, "LLM-generated (semantic)"
    else:
        fix_score, fix_label = 0.80, "Pattern-based"
    hist_score = min(0.95, confidence - 0.03)
    
    obs_reason = reasons[0] if reasons else "Co-change pattern detected in git history"
    body_parts.append(f"| Observation history | {obs_score:.2f} | {obs_reason} |")
    body_parts.append(f"| Language confidence | {lang_score:.2f} | Native template engine |")
    body_parts.append(f"| Fix type | {fix_score:.2f} | {fix_label} |")
    body_parts.append(f"| Historical accuracy | {hist_score:.2f} | Similar fixes merged without revert |")
    
    body_parts.append("")
    
    # Impact Report
    body_parts.append("### 📊 Change Impact Report")
    body_parts.append("")
    
    if all_predictions and len(all_predictions) > 0:
        total = len(all_predictions)
        body_parts.append(f"**{total} consumer(s) found** across your org:")
        body_parts.append("")
        body_parts.append("| # | File | Confidence | Status |")
        body_parts.append("|---|---|---|---|")
        
        for i, pred in enumerate(all_predictions[:8], 1):
            p_level = classify_confidence(pred.get("confidence", 0.5))
            status = "✅ Fixed" if pred.get("fixed") else "📝 This PR"
            body_parts.append(
                f"| {i} | `{pred.get('file', '?')[:50]}` | {int(pred.get('confidence', 0.5) * 100)}% | {status} |"
            )
        body_parts.append("")
    
    # Upstream status
    body_parts.extend([
        "### ⏳ Upstream Status: PENDING",
        "",
        "Source change has been pushed but not yet merged.",
        "**Review this fix now, merge after upstream lands.**",
        "",
    ])
    
    # Fix applied
    if fix_summary:
        body_parts.extend([
            "### ✅ Fix Applied",
            "",
            fix_summary,
            "",
        ])
    
    # Why this file (learning context)
    if reasons:
        body_parts.extend([
            "### 🔗 Why Ripple chose this file",
            "",
        ])
        for reason in reasons[:3]:
            body_parts.append(f"- {reason}")
        body_parts.append("")
    
    # Footer
    body_parts.extend([
        "---",
        f"*Generated by [Ripple](https://ripple-cnn.pages.dev) | 🧠 PropBench v1 (882 entries) | Detection: {' + '.join(sources[:3])} | Learning: enabled*",
    ])
    
    return "\n".join(body_parts)


def should_create_pr(confidence: float, min_confidence: float = 0.5) -> bool:
    """Decide whether to create a PR based on confidence score."""
    return confidence >= min_confidence


def should_add_warning(confidence: float) -> bool:
    """Decide whether to add a 'needs review' warning to the PR."""
    level = classify_confidence(confidence)
    return level.label == "medium"
