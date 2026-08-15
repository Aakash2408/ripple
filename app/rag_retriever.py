"""RAG-based fix pattern retrieval with multi-signal scoring and cluster fallback."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from app.rag_store import rag_store, FixPattern, StructuredPattern


@dataclass
class RagFixResult:
    """Result of a RAG-based fix generation."""
    fixed_code: str
    explanation: str
    confidence: float
    source_type: str  # rag_exact | rag_cluster | template | llm_fallback
    pattern_id: Optional[str] = None


def _multi_signal_score(
    pattern: FixPattern,
    change_type: str,
    language: str,
    repo: Optional[str] = None,
) -> float:
    """Score a pattern using similarity + language + recency + success_rate."""
    score = 0.0

    # Base similarity: change_type match
    if pattern.change_type == change_type:
        score += 0.4
    elif pattern.change_type in change_type or change_type in pattern.change_type:
        score += 0.2

    # Language match
    if pattern.language == language:
        score += 0.25
    elif pattern.language in ("generic", "*"):
        score += 0.1

    # Success rate (merge_count / total attempts)
    total = pattern.merge_count + pattern.reject_count
    if total > 0:
        success_rate = pattern.merge_count / total
        score += 0.2 * success_rate
    else:
        score += 0.1  # no data = neutral

    # Recency bonus (decays over 30 days)
    if pattern.last_used:
        age_days = (time.time() - pattern.last_used) / 86400
        recency = max(0.0, 1.0 - (age_days / 30))
        score += 0.15 * recency
    else:
        score += 0.05

    # Cross-repo penalty
    if repo and pattern.repo and pattern.repo != repo:
        score *= 0.8

    return min(score, 1.0)


def retrieve_fix_pattern(
    change_type: str,
    language: str,
    field_name: str,
    repo: Optional[str] = None,
) -> Optional[tuple[FixPattern, float]]:
    """Retrieve best fix pattern using multi-signal scoring. Returns top candidate."""
    candidates: list[tuple[FixPattern, float]] = []

    for pattern in rag_store.patterns:
        score = _multi_signal_score(pattern, change_type, language, repo)

        # Boost if field name appears in pattern context
        if field_name and pattern.field_name and pattern.field_name == field_name:
            score = min(score + 0.15, 1.0)

        candidates.append((pattern, score))

    # Sort by score descending, take top 3
    candidates.sort(key=lambda x: x[1], reverse=True)
    top3 = candidates[:3]

    # Return best applicable candidate
    for pattern, score in top3:
        if score >= 0.7:
            return pattern, score

    return None


def retrieve_cluster_archetype(
    change_type: str,
    language: str,
) -> Optional[tuple[StructuredPattern, float]]:
    """Fall back to cluster archetypes when no exact match found."""
    best: Optional[tuple[StructuredPattern, float]] = None
    best_score = 0.0

    for sp in rag_store.structured_patterns:
        score = 0.0
        if sp.change_type == change_type:
            score += 0.5
        if sp.language == language:
            score += 0.3
        # Cluster reliability
        if sp.example_count > 5:
            score += 0.1
        if sp.avg_confidence > 0.8:
            score += 0.1

        if score > best_score:
            best_score = score
            best = (sp, score)

    if best and best_score >= 0.5:
        return best
    return None


def generate_explanation(pattern: FixPattern) -> str:
    """Generate human-readable explanation of why this pattern was chosen."""
    total = pattern.merge_count + pattern.reject_count
    merge_rate = (pattern.merge_count / total * 100) if total > 0 else 0

    parts = [
        f"Applied pattern seen in {total} prior commit{'s' if total != 1 else ''}"
        f" ({merge_rate:.0f}% merge rate).",
    ]

    if pattern.strategy:
        parts.append(f"Strategy: {pattern.strategy}.")

    if pattern.source_file:
        source_desc = pattern.source_file
        if pattern.last_used:
            from datetime import datetime
            dt = datetime.fromtimestamp(pattern.last_used)
            source_desc += f" on {dt.strftime('%b %d')}"
        parts.append(f"Similar to fix applied in {source_desc}.")

    return " ".join(parts)


def _apply_pattern_fix(
    pattern: FixPattern,
    consumer_code: str,
    field_name: str,
) -> str:
    """Apply a pattern's fix strategy to consumer code."""
    # Delegate to fix_templates for actual code transformation
    from app.fix_templates import apply_fix_template

    result = apply_fix_template(
        change_type=pattern.change_type,
        language=pattern.language,
        field_name=field_name,
        source_code=consumer_code,
        new_field_name=pattern.new_field_name,
        new_type=pattern.new_type,
    )
    return result if result else consumer_code


def _apply_cluster_fix(
    cluster: StructuredPattern,
    consumer_code: str,
    field_name: str,
    language: str,
) -> str:
    """Apply cluster archetype strategy via fix_templates."""
    from app.fix_templates import apply_fix_template

    result = apply_fix_template(
        change_type=cluster.change_type,
        language=language,
        field_name=field_name,
        source_code=consumer_code,
    )
    return result if result else consumer_code


def generate_fix_rag(
    change_type: str,
    language: str,
    field_name: str,
    consumer_code: str,
    repo: Optional[str] = None,
) -> RagFixResult:
    """
    Main entry point. Tries RAG retrieval first (threshold 0.7),
    then cluster archetype (threshold 0.5), then fix_templates as final fallback.
    NEVER calls LLM.
    """
    # 1. Try exact RAG pattern match
    rag_result = retrieve_fix_pattern(change_type, language, field_name, repo)
    if rag_result:
        pattern, confidence = rag_result
        fixed = _apply_pattern_fix(pattern, consumer_code, field_name)
        explanation = generate_explanation(pattern)
        return RagFixResult(
            fixed_code=fixed,
            explanation=explanation,
            confidence=confidence,
            source_type="rag_exact",
            pattern_id=pattern.pattern_id,
        )

    # 2. Try cluster archetype
    cluster_result = retrieve_cluster_archetype(change_type, language)
    if cluster_result:
        cluster, confidence = cluster_result
        fixed = _apply_cluster_fix(cluster, consumer_code, field_name, language)
        explanation = (
            f"No exact match found. Using cluster archetype for "
            f"{change_type} in {language} "
            f"(based on {cluster.example_count} examples, "
            f"avg confidence {cluster.avg_confidence:.0%})."
        )
        return RagFixResult(
            fixed_code=fixed,
            explanation=explanation,
            confidence=confidence * 0.9,  # slight penalty for cluster
            source_type="rag_cluster",
            pattern_id=cluster.cluster_id,
        )

    # 3. Final fallback: fix_templates (deterministic, no LLM)
    from app.fix_templates import apply_fix_template

    fixed = apply_fix_template(
        change_type=change_type,
        language=language,
        field_name=field_name,
        source_code=consumer_code,
    )
    if fixed and fixed != consumer_code:
        return RagFixResult(
            fixed_code=fixed,
            explanation=(
                f"No RAG pattern or cluster match. Applied deterministic "
                f"template for {change_type} in {language}."
            ),
            confidence=0.6,
            source_type="template",
            pattern_id=None,
        )

    # Nothing worked -- return original code with low confidence
    return RagFixResult(
        fixed_code=consumer_code,
        explanation="No applicable fix pattern found. Code unchanged.",
        confidence=0.0,
        source_type="template",
        pattern_id=None,
    )


def learn_from_merged_pr(pattern_id: str) -> None:
    """Update pattern stats when a fix PR is merged. Also updates cluster stats."""
    for p in rag_store.patterns:
        if p.pattern_id == pattern_id:
            p.merge_count += 1
            p.last_used = time.time()
            break

    # Update structured pattern cluster stats
    for sp in rag_store.structured_patterns:
        if sp.cluster_id == pattern_id or any(
            p.pattern_id == pattern_id
            for p in rag_store.patterns
            if p.change_type == sp.change_type and p.language == sp.language
        ):
            total = sum(
                p.merge_count + p.reject_count
                for p in rag_store.patterns
                if p.change_type == sp.change_type and p.language == sp.language
            )
            merged = sum(
                p.merge_count
                for p in rag_store.patterns
                if p.change_type == sp.change_type and p.language == sp.language
            )
            if total > 0:
                sp.avg_confidence = merged / total
            sp.example_count = sum(
                1 for p in rag_store.patterns
                if p.change_type == sp.change_type and p.language == sp.language
            )
            break

    rag_store.save()


def learn_from_rejected_pr(pattern_id: str) -> None:
    """Mark a pattern as less reliable when its fix PR is rejected."""
    for p in rag_store.patterns:
        if p.pattern_id == pattern_id:
            p.reject_count += 1
            p.last_used = time.time()
            break

    # Recalculate cluster stats
    for sp in rag_store.structured_patterns:
        matching = [
            p for p in rag_store.patterns
            if p.change_type == sp.change_type and p.language == sp.language
        ]
        if matching:
            total = sum(p.merge_count + p.reject_count for p in matching)
            merged = sum(p.merge_count for p in matching)
            if total > 0:
                sp.avg_confidence = merged / total

    rag_store.save()
