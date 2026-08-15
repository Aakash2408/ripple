"""RAG retrieval + pattern application layer for fix generation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.fix_templates import apply_fix_template
from app.rag_store import RagStore, FixExample


@dataclass
class FixPattern:
    """A retrieved fix pattern from the RAG store."""
    source_example: FixExample
    similarity: float
    applicable: bool
    fix_strategy: str


def retrieve_fix_pattern(
    change_description: str,
    field_name: str,
    language: str,
    store: RagStore,
) -> Optional[FixPattern]:
    """Search the RAG store for similar past changes and return the best match.

    Boosts results matching the target language. Returns None if no
    sufficiently similar pattern is found.
    """
    candidates = store.search(change_description, field_name)
    if not candidates:
        return None

    scored: list[tuple[FixExample, float]] = []
    for example, base_similarity in candidates:
        score = base_similarity
        # Boost same-language matches
        if example.language.lower() == language.lower():
            score = min(1.0, score + 0.15)
        scored.append((example, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    best_example, best_score = scored[0]

    # Extract strategy from the fix diff
    strategy = _extract_strategy(best_example)

    return FixPattern(
        source_example=best_example,
        similarity=best_score,
        applicable=best_score > 0.5,
        fix_strategy=strategy,
    )


def _extract_strategy(example: FixExample) -> str:
    """Infer the fix strategy from a stored example's metadata."""
    change_type = example.change_type
    strategies = {
        "field_removed": "remove struct field + remove param + remove usage",
        "removed_field": "remove struct field + remove param + remove usage",
        "field_renamed": "find old name variants + replace with new name",
        "field_type_changed": "update type annotations + update casts",
        "type_changed": "update type annotations + update casts",
        "added_required_field": "add field declaration + initialize in constructors",
    }
    return strategies.get(change_type, f"apply {change_type} pattern")


def apply_pattern(
    code: str,
    pattern: FixPattern,
    field_name: str,
    language: str,
) -> tuple[str, str]:
    """Apply a retrieved pattern's strategy to the current code.

    Uses fix_templates for the actual code transformation.
    Returns (fixed_code, explanation).
    """
    change_type = pattern.source_example.change_type
    # Normalize change_type aliases
    if change_type == "removed_field":
        change_type = "field_removed"

    fixed_code = apply_fix_template(
        code=code,
        field_name=field_name,
        change_type=change_type,
        language=language,
    )

    explanation = (
        f"Applied pattern from similar change (similarity={pattern.similarity:.2f}): "
        f"{pattern.fix_strategy}. "
        f"Based on prior fix in {pattern.source_example.language} "
        f"for field '{pattern.source_example.field_name}'."
    )
    return fixed_code, explanation


def generate_fix_rag(
    code: str,
    file_path: str,
    field_name: str,
    change_type: str,
    change_description: str,
    store: RagStore,
) -> tuple[str, str]:
    """Main entry point: RAG retrieval + pattern application. Never calls an LLM.

    If RAG finds a similar pattern with similarity > 0.7, applies it.
    Otherwise falls back to fix_templates directly.
    Returns (fixed_code, explanation).
    """
    # Detect language from file extension
    language = _detect_language(file_path)

    # Try RAG retrieval
    pattern = retrieve_fix_pattern(change_description, field_name, language, store)

    if pattern and pattern.similarity > 0.7 and pattern.applicable:
        fixed_code, explanation = apply_pattern(code, pattern, field_name, language)
        return fixed_code, f"[RAG] {explanation}"

    # Fallback: direct template application
    # Normalize change_type aliases
    normalized_type = change_type
    if normalized_type == "removed_field":
        normalized_type = "field_removed"

    fixed_code = apply_fix_template(
        code=code,
        field_name=field_name,
        change_type=normalized_type,
        language=language,
    )

    explanation = (
        f"[Template] Applied {normalized_type} fix for '{field_name}' in {language}. "
        f"No sufficiently similar RAG pattern found"
        f"{f' (best={pattern.similarity:.2f})' if pattern else ''}."
    )
    return fixed_code, explanation


def learn_from_merged_pr(
    trigger_diff: str,
    fix_diff: str,
    language: str,
    field_name: str,
    change_type: str,
    store: RagStore,
) -> None:
    """Learn from a merged PR -- adds a confirmed-good fix as a new example.

    Called when a Ripple-generated PR gets merged, confirming the fix was correct.
    The system gets smarter with every merged PR.
    """
    example = FixExample(
        trigger_diff=trigger_diff,
        fix_diff=fix_diff,
        language=language,
        field_name=field_name,
        change_type=change_type,
    )
    store.add(example)


def _detect_language(file_path: str) -> str:
    """Detect programming language from file extension."""
    ext_map = {
        ".go": "go",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".js": "javascript",
        ".jsx": "javascript",
        ".py": "python",
        ".java": "java",
        ".rs": "rust",
        ".rb": "ruby",
        ".kt": "kotlin",
        ".kts": "kotlin",
        ".cs": "csharp",
        ".swift": "swift",
        ".php": "php",
        ".scala": "scala",
        ".dart": "dart",
    }
    for ext, lang in ext_map.items():
        if file_path.endswith(ext):
            return lang
    return "unknown"
