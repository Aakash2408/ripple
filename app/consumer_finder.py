from __future__ import annotations
"""
ripple/app/consumer_finder.py

Consumer Finder — scan repos/directories for code that calls a changed API endpoint.

Strategy (V0 — simple and fast):
1. Grep for the endpoint path ("/users")
2. Grep for HTTP method usage near that path (POST, .post(, etc.)
3. Look for the field name being constructed in payloads
4. Return list of (file, line_number, code_snippet)

No AST parsing. No dependency resolution. Just grep.
That's enough for the demo.
"""

import os
from dataclasses import dataclass

from .diff_engine import BreakingChange


@dataclass
class ConsumerMatch:
    """A file that likely consumes the broken API endpoint."""
    file_path: str
    line_number: int
    code_snippet: str
    confidence: str       # "high", "medium", "low"
    match_reason: str     # why we think this file is a consumer
    language: str         # detected language


def find_consumers(
    search_dirs: list[str],
    breaking_change: BreakingChange,
    exclude_patterns: list[str] = None,
) -> list[ConsumerMatch]:
    """Find files that consume the thing a breaking change affected.

    DELEGATES matching to smart_consumer_finder, which is what the webhook and
    the PropBench replay harness use. This function owns only the directory walk
    and the ConsumerMatch conversion.

    WHY THIS CHANGED
    It used to match on the ENDPOINT PATH and HTTP METHOD while production
    matched on the FIELD SYMBOL -- a different question, not a different
    implementation of the same one. So `ripple scan` and `ripple analyze` could
    report a different consumer set than the webhook would for the identical
    change, and neither was wrong on its own terms. That is the
    production-vs-CLI divergence: the local command was not a preview of what
    the service would do.

    Now there is one matcher. The directory walk stays here because the webhook
    walks a GitHub tree instead of a filesystem -- different traversal, same
    matching.
    """
    from app.languages import detect as _detect, is_scannable
    from app.smart_consumer_finder import find_matches_in_file
    from app.change_types import vector_for

    if exclude_patterns is None:
        exclude_patterns = [
            "node_modules", ".git", "dist", "build", "__pycache__",
            "vendor", ".venv", "target", ".gradle",
        ]

    # The symbol production searches for, and the vector derived FROM the change
    # rather than passed in -- the same rule the webhook uses.
    target = breaking_change.field_name or breaking_change.path
    try:
        vector = vector_for(breaking_change.change_type)
    except Exception:
        vector = "symbol"

    matches: list[ConsumerMatch] = []

    for search_dir in search_dirs:
        for root, dirs, files in os.walk(search_dir):
            dirs[:] = [d for d in dirs if d not in exclude_patterns]
            for filename in files:
                filepath = os.path.join(root, filename)
                # is_scannable() is the canonical decision, shared with the
                # webhook: it also rejects vendored and generated files, which
                # the old extension-only check did not.
                if not is_scannable(filepath):
                    continue
                try:
                    with open(filepath, "r", errors="ignore") as fh:
                        content = fh.read()
                except (IOError, OSError):
                    continue
                language = _detect(filepath)
                for m in find_matches_in_file(content, filepath, target,
                                              language, vector=vector):
                    matches.append(ConsumerMatch(
                        file_path=m.file_path,
                        line_number=m.line_number,
                        code_snippet=m.line_content.strip(),
                        # SmartMatch scores 0.0-1.0; ConsumerMatch is a band.
                        confidence=("high" if m.confidence >= 0.85
                                    else "medium" if m.confidence >= 0.6
                                    else "low"),
                        match_reason=m.match_type,
                        language=language,
                    ))

    confidence_order = {"high": 0, "medium": 1, "low": 2}
    matches.sort(key=lambda m: confidence_order.get(m.confidence, 3))
    return matches


# Was a second _is_code_file with a THIRD extension set, disagreeing with
# both webhook's filter and every detector. Canonical now.




# Re-exported, not used here: tests/test_regression.py asserts
# `consumer_finder._detect_language is languages.detect`, which is what stops a
# module quietly re-adding its own detector. Removing this would make that gate
# silently weaker rather than fail.
from .languages import detect as _detect_language  # noqa: E402,F401




def format_consumers(matches: list[ConsumerMatch]) -> str:
    """Format consumer matches for display."""
    if not matches:
        return "  No consumers found."
    
    lines = [f"  Found {len(matches)} consumer(s):", ""]
    
    for i, m in enumerate(matches, 1):
        icon = "🔴" if m.confidence == "high" else "🟡" if m.confidence == "medium" else "⚪"
        lines.append(f"  {icon} [{i}] {m.file_path}:{m.line_number}")
        lines.append(f"       Language: {m.language}")
        lines.append(f"       Confidence: {m.confidence}")
        lines.append(f"       Reason: {m.match_reason}")
        lines.append(f"       Code: {m.code_snippet[:80]}")
        lines.append("")
    
    return "\n".join(lines)
