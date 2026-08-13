"""
Impact Prediction module for Ripple.
Predicts consequences of breaking changes BEFORE they are pushed.
"""

from dataclasses import dataclass, field
from typing import Optional

from .consumer_graph import ConsumerGraph
from .diff_engine import BreakingChange
from .expand_contract import suggest_expand_contract_path


@dataclass
class ImpactDetail:
    """Per-consumer impact breakdown."""
    repo: str
    file: str
    language: str
    confidence: float
    auto_fixable: bool
    reason: str


@dataclass
class ImpactReport:
    """Full impact prediction report."""
    total_consumers: int
    affected_repos: list[str]
    risk_level: str  # 'low', 'medium', 'high', 'critical'
    estimated_fix_time: str
    auto_fixable_count: int
    manual_fix_count: int
    breakdown: list[ImpactDetail] = field(default_factory=list)


def _calculate_risk_level(total_consumers: int, has_production_services: bool = False) -> str:
    """Determine risk level based on consumer count and production status."""
    if total_consumers > 10 or has_production_services:
        return "critical"
    elif total_consumers >= 5:
        return "high"
    elif total_consumers >= 2:
        return "medium"
    else:
        return "low"


def _estimate_fix_time(breakdown: list[ImpactDetail]) -> str:
    """Estimate total fix time based on auto-fixable vs manual breakdown."""
    auto_count = sum(1 for d in breakdown if d.auto_fixable)
    manual_count = len(breakdown) - auto_count

    # Auto fixes: ~2 min each, manual: ~30 min each
    total_minutes = (auto_count * 2) + (manual_count * 30)

    if total_minutes < 60:
        return f"~{total_minutes} minutes"
    hours = total_minutes / 60
    return f"~{hours:.1f} hours"


def _detect_production_services(repos: list[str]) -> bool:
    """Heuristic to detect if any affected repos are production services."""
    production_indicators = [
        "prod", "production", "service", "api-gateway",
        "platform", "core", "infra", "backend"
    ]
    for repo in repos:
        repo_lower = repo.lower()
        if any(indicator in repo_lower for indicator in production_indicators):
            return True
    return False


def predict_impact(
    spec_file: str,
    proposed_changes: list[BreakingChange],
    graph: ConsumerGraph
) -> ImpactReport:
    """
    Predict the impact of proposed breaking changes before push.

    Args:
        spec_file: Path to the API spec file being changed.
        proposed_changes: List of detected breaking changes.
        graph: Consumer dependency graph.

    Returns:
        ImpactReport with full risk assessment and per-consumer breakdown.
    """
    affected_repos = graph.get_consumers(spec_file)
    has_production = _detect_production_services(affected_repos)

    breakdown: list[ImpactDetail] = []

    for repo in affected_repos:
        consumer_files = graph.get_consumer_files(repo, spec_file)
        for consumer_file in consumer_files:
            language = graph.detect_language(consumer_file)
            # Determine if each affected file can be auto-fixed
            auto_fixable = _is_auto_fixable(language, proposed_changes)
            confidence = _calculate_confidence(language, proposed_changes)
            reason = _build_reason(proposed_changes, auto_fixable)

            breakdown.append(ImpactDetail(
                repo=repo,
                file=consumer_file,
                language=language,
                confidence=confidence,
                auto_fixable=auto_fixable,
                reason=reason,
            ))

    auto_fixable_count = sum(1 for d in breakdown if d.auto_fixable)
    manual_fix_count = len(breakdown) - auto_fixable_count
    total_consumers = len(affected_repos)
    risk_level = _calculate_risk_level(total_consumers, has_production)
    estimated_fix_time = _estimate_fix_time(breakdown)

    return ImpactReport(
        total_consumers=total_consumers,
        affected_repos=affected_repos,
        risk_level=risk_level,
        estimated_fix_time=estimated_fix_time,
        auto_fixable_count=auto_fixable_count,
        manual_fix_count=manual_fix_count,
        breakdown=breakdown,
    )


def _is_auto_fixable(language: str, changes: list[BreakingChange]) -> bool:
    """Determine if changes can be auto-fixed for a given language."""
    supported_languages = {
        "python", "typescript", "javascript", "java", "go",
        "rust", "ruby", "kotlin", "csharp", "swift", "php", "scala"
    }
    if language.lower() not in supported_languages:
        return False
    # Simple renames and type changes are auto-fixable
    for change in changes:
        if change.kind in ("removed_field", "removed_endpoint", "semantic_change"):
            return False
    return True


def _calculate_confidence(language: str, changes: list[BreakingChange]) -> float:
    """Calculate confidence score for the fix prediction."""
    base_confidence = 0.85
    # Well-typed languages get higher confidence
    typed_languages = {"typescript", "java", "kotlin", "rust", "go", "csharp", "swift", "scala"}
    if language.lower() in typed_languages:
        base_confidence += 0.10
    # Complex changes reduce confidence
    complex_kinds = {"semantic_change", "removed_endpoint", "type_restructure"}
    complex_count = sum(1 for c in changes if c.kind in complex_kinds)
    base_confidence -= complex_count * 0.05
    return max(0.1, min(1.0, base_confidence))


def _build_reason(changes: list[BreakingChange], auto_fixable: bool) -> str:
    """Build a human-readable reason for the impact."""
    change_descriptions = [c.description for c in changes[:3]]
    summary = "; ".join(change_descriptions)
    if auto_fixable:
        return f"Auto-fixable: {summary}"
    return f"Manual review needed: {summary}"


def format_impact_report(report: ImpactReport) -> str:
    """Format a full markdown impact report."""
    risk_emoji = {
        "low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴"
    }
    emoji = risk_emoji.get(report.risk_level, "⚪")

    lines = [
        f"# Impact Prediction Report",
        f"",
        f"## {emoji} Risk Level: {report.risk_level.upper()}",
        f"",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total consumers affected | {report.total_consumers} |",
        f"| Auto-fixable | {report.auto_fixable_count} |",
        f"| Manual review needed | {report.manual_fix_count} |",
        f"| Estimated fix time | {report.estimated_fix_time} |",
        f"",
        f"## Affected Services",
        f"",
    ]

    for repo in report.affected_repos:
        lines.append(f"- `{repo}`")

    lines.append("")
    lines.append("## Breakdown")
    lines.append("")
    lines.append("| Repo | File | Language | Auto-fix | Confidence |")
    lines.append("|------|------|----------|----------|------------|")

    for detail in report.breakdown:
        fix_icon = "✅" if detail.auto_fixable else "❌"
        conf_pct = f"{detail.confidence * 100:.0f}%"
        lines.append(
            f"| {detail.repo} | `{detail.file}` | {detail.language} "
            f"| {fix_icon} | {conf_pct} |"
        )

    lines.append("")
    lines.append("---")
    lines.append(f"*Prediction generated by Ripple Impact Engine*")

    return "\n".join(lines)


def format_impact_summary(report: ImpactReport) -> str:
    """One-line impact summary for PR bodies and notifications."""
    return (
        f"{report.risk_level.upper()} RISK: "
        f"{report.total_consumers} services affected, "
        f"{report.auto_fixable_count} auto-fixable, "
        f"{report.manual_fix_count} need manual review"
    )


def suggest_safe_alternative(changes: list[BreakingChange]) -> str:
    """
    Suggest expand+contract or deprecation path if risk is high.

    Returns markdown-formatted suggestion for safer migration.
    """
    suggestions = []

    for change in changes:
        ec_suggestion = suggest_expand_contract_path(change)
        if ec_suggestion:
            suggestions.append(ec_suggestion)

    if not suggestions:
        return (
            "**No safe alternative found.** Consider:\n"
            "1. Deprecation notice with sunset date (minimum 2 weeks)\n"
            "2. Versioned endpoint (e.g., `/v2/resource`)\n"
            "3. Feature flag to gradually migrate consumers"
        )

    lines = ["**Recommended safe migration path:**", ""]
    for i, suggestion in enumerate(suggestions, 1):
        lines.append(f"{i}. {suggestion}")

    lines.extend([
        "",
        "This allows consumers to migrate at their own pace "
        "without breaking existing integrations.",
    ])

    return "\n".join(lines)
