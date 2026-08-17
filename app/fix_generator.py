from __future__ import annotations
"""
ripple/app/fix_generator.py

Fix Generator — generates code fixes for consumers affected by API breaking changes.

Uses Claude API to generate minimal, correct fixes.
Falls back to template-based fixes if no API key.

For the YC demo: the magic moment is seeing actual code diffs appear.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .diff_engine import BreakingChange
from .consumer_finder import ConsumerMatch


@dataclass
class GeneratedFix:
    """A generated fix for one consumer file."""
    consumer: ConsumerMatch
    original_code: str
    fixed_code: str
    explanation: str
    diff: str             # unified diff format


def generate_fix(
    consumer: ConsumerMatch,
    breaking_change: BreakingChange,
    use_llm: bool = True,
) -> Optional[GeneratedFix]:
    """
    Generate a fix for a consumer affected by a breaking change.
    
    Strategy:
    1. Read the full consumer file
    2. Send to Claude: "This API changed. Fix this consumer code."
    3. Return the fixed code + diff
    """
    # Read the consumer file
    try:
        with open(consumer.file_path, "r") as f:
            original_code = f.read()
    except IOError:
        return None
    
    # Gate on the SAME resolution the call site uses. Reading
    # ANTHROPIC_API_KEY here while _generate_with_llm builds its client from
    # llm_config.api_key() meant an ANTHROPIC_AUTH_TOKEN setup fell through to
    # the template silently -- the gate and the call disagreed about whether a
    # key existed.
    from .llm_config import api_key as _llm_key
    if use_llm and _llm_key():
        fixed_code, explanation = _generate_with_llm(
            original_code, consumer, breaking_change
        )
    else:
        fixed_code, explanation = _generate_with_template(
            original_code, consumer, breaking_change
        )
    
    if not fixed_code or fixed_code == original_code:
        return None
    
    diff = _compute_diff(original_code, fixed_code, consumer.file_path)
    
    return GeneratedFix(
        consumer=consumer,
        original_code=original_code,
        fixed_code=fixed_code,
        explanation=explanation,
        diff=diff,
    )


def generate_fixes(
    consumers: list[ConsumerMatch],
    breaking_change: BreakingChange,
    use_llm: bool = True,
    high_confidence_only: bool = True,
) -> list[GeneratedFix]:
    """Generate fixes for all consumers."""
    fixes = []
    
    for consumer in consumers:
        # Skip low-confidence matches
        if high_confidence_only and consumer.confidence != "high":
            continue
        
        fix = generate_fix(consumer, breaking_change, use_llm=use_llm)
        if fix:
            fixes.append(fix)
    
    return fixes


def _generate_with_llm(
    original_code: str,
    consumer: ConsumerMatch,
    breaking_change: BreakingChange,
) -> tuple[str, str]:
    """Use Claude API to generate the fix."""
    try:
        import anthropic
    except ImportError:
        print("  ⚠️  anthropic package not installed. Using template fix.")
        return _generate_with_template(original_code, consumer, breaking_change)
    
    from .llm_config import api_key as _llm_key, base_url as _llm_base, model as _llm_model
    # base_url passed explicitly rather than relying on the SDK reading the env,
    # so the configured backend is visible at the call site.
    client = anthropic.Anthropic(api_key=_llm_key(), base_url=_llm_base())
    
    prompt = f"""You are a code assistant. An API has a breaking change. Fix the consumer code.

BREAKING CHANGE:
- Endpoint: {breaking_change.method.upper()} {breaking_change.path}
- Change: {breaking_change.change_type}
- Field: "{breaking_change.field_name}" (type: {breaking_change.field_type})
- Description: {breaking_change.description}

CONSUMER CODE ({consumer.language}):
```
{original_code}
```

INSTRUCTIONS:
1. Add the new required field "{breaking_change.field_name}" to the API call.
2. Add it as a parameter/argument that callers must provide.
3. Keep the fix minimal — only change what's necessary.
4. Return ONLY the complete fixed file content, no explanation.
5. Do NOT add comments explaining the fix.

FIXED CODE:"""

    try:
        response = client.messages.create(
            model=_llm_model(),
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        
        fixed_code = response.content[0].text.strip()
        
        # Strip markdown code fences if present
        if fixed_code.startswith("```"):
            lines = fixed_code.split("\n")
            # Remove first and last lines (fences)
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            fixed_code = "\n".join(lines)
        
        explanation = f"Added required field '{breaking_change.field_name}' to {breaking_change.method.upper()} {breaking_change.path} call"
        return fixed_code, explanation
        
    except Exception as e:
        print(f"  ⚠️  LLM error: {e}. Using template fix.")
        return _generate_with_template(original_code, consumer, breaking_change)


def _generate_with_template(
    original_code: str,
    consumer: ConsumerMatch,
    breaking_change: BreakingChange,
) -> tuple[str, str]:
    """
    Template-based fix (no LLM needed).
    Works for the common case: "add a field to a JSON payload."
    """
    field_name = breaking_change.field_name
    
    if breaking_change.change_type in ("removed_field", "field_removed"):
        # Use the comprehensive template engine
        from .fix_templates import apply_fix_template
        fixed, explanation = apply_fix_template(
            code=original_code,
            language=consumer.language or "unknown",
            change_type="field_removed",
            field_name=field_name,
        )
        if fixed != original_code:
            return fixed, explanation
        # Fallback to basic line removal
        return _remove_field_references(original_code, field_name, consumer.language)
    
    if breaking_change.change_type in ("field_renamed", "renamed_field"):
        from .fix_templates import apply_fix_template, annotate_references
        # Was getattr(breaking_change, 'new_name', '') on a dataclass with no
        # such field, so new_name was ALWAYS '' and this branch never fired --
        # every rename fell through to "Unsupported change type", leaving the
        # code unchanged, which opens no PR. Detection became silence.
        new_name = breaking_change.new_name
        if new_name:
            fixed, expl = apply_fix_template(
                code=original_code,
                language=consumer.language or "unknown",
                change_type="field_renamed",
                field_name=field_name,
                new_name=new_name,
            )
            if fixed != original_code:
                return fixed, expl
            # The template recognised the operation but matched nothing (it is
            # a no-op for javascript and ruby today). Flag rather than go quiet.
            return annotate_references(
                original_code, consumer.language or "unknown", field_name,
                f"field '{field_name}' was renamed to '{new_name}' -- update "
                f"these references.")
        # An engine that detects the rename but cannot name the target. Do NOT
        # borrow field_removed here: that deletes references to a field which
        # still exists under a new name, and would report "Removed all
        # references".
        return annotate_references(
            original_code, consumer.language or "unknown", field_name,
            f"field '{field_name}' was renamed, but the diff engine did not "
            f"report the new name -- rename these references by hand.")

    if breaking_change.change_type in ("field_type_changed", "type_changed"):
        from .fix_templates import apply_fix_template, annotate_references
        # PRECEDENCE BUG: this read
        #     getattr(bc,'old_type','') or bc.field_type.split(...)[0] if COND else ''
        # which Python groups as
        #     (getattr(...) or split(...)) if COND else ''
        # because a conditional expression binds looser than `or`. So whenever
        # field_type lacked ' -> ', old_type became '' EVEN IF the attribute
        # held a value -- and the attribute never existed anyway. The branch
        # therefore fired only when field_type happened to be formatted as
        # "old -> new", i.e. it depended on a string-formatting accident.
        old_type = breaking_change.old_type
        new_type = breaking_change.new_type
        if not (old_type and new_type):
            # Fall back to parsing the display string, accepting either arrow.
            raw = str(breaking_change.field_type)
            for arrow in (" \u2192 ", " -> "):
                if arrow in raw:
                    left, _, right = raw.partition(arrow)
                    old_type = old_type or left.strip()
                    new_type = new_type or right.strip()
                    break
        if old_type and new_type:
            fixed, expl = apply_fix_template(
                code=original_code,
                language=consumer.language or "unknown",
                change_type="type_changed",
                field_name=field_name,
                old_type=old_type,
                new_type=new_type,
            )
            if fixed != original_code:
                return fixed, expl
            # change_field_type is a measured no-op in 6 of 9 languages. That is
            # a template gap, but it must not surface as silence.
            return annotate_references(
                original_code, consumer.language or "unknown", field_name,
                f"type of '{field_name}' changed from {old_type} to {new_type} "
                f"-- verify these references still compile.")
        # A type change we cannot describe is still a break the consumer must
        # look at.
        return annotate_references(
            original_code, consumer.language or "unknown", field_name,
            f"type of '{field_name}' changed, but the diff engine did not "
            f"report the old and new types -- verify these references.")
    
    if breaking_change.change_type != "added_required_field":
        return original_code, "Unsupported change type for template fix"

    # DELEGATE, like every other change type above. This branch used to carry
    # its own inline implementation for typescript/python/java, which was a
    # second implementation of an operation fix_templates already handles for
    # all nine languages -- and the two disagreed on the CATEGORY.
    #
    # added_required_field is JUDGMENT (app/change_types.py). The contract is:
    # apply the safe part, mark the rest, never invent a value. The inline
    # version treated it as mechanical -- it appended a REQUIRED positional
    # parameter and wrote the field into the payload, which breaks every
    # existing caller (`create_user(name, email)` -> TypeError) and silently
    # decides what value to send. fix_templates annotates each construction
    # site instead and says explicitly that it did not choose a value.
    #
    # Deleting the duplicate also puts this path under tools/coverage_matrix.py,
    # which exercises apply_fix_template and never reached the inline code.
    from .fix_templates import apply_fix_template
    return apply_fix_template(
        code=original_code,
        language=consumer.language or "unknown",
        change_type="add_required",
        field_name=field_name,
        site_hints=_call_site_hints(breaking_change),
    )


def _call_site_hints(breaking_change: BreakingChange) -> tuple[str, ...]:
    """Anchors for locating the construction site of an affected request.

    add_required cannot anchor on the field: a newly required field is by
    definition absent from consumer code. The endpoint path is the one thing the
    contract and the consumer both name -- `post("/users", ...)` and, for
    proto/GraphQL engines where `path` holds a message or type name, `User{...}`.

    Ordered widest-anchor-first. The trailing segment is included because
    consumers commonly build the URL (`f"{base}/users/{id}"`) rather than
    writing the path literally, and it is only used when nothing more specific
    matched -- a spurious anchor costs a stray comment, never a code edit.
    """
    hints: list[str] = []
    path = (breaking_change.path or "").strip()
    if path:
        hints.append(path)
        if "/" in path:
            segment = path.rstrip("/").rsplit("/", 1)[-1]
            # 4+ chars: shorter segments ("id", "me", "v1") match everywhere.
            if segment and segment != path and len(segment) >= 4:
                hints.append(segment)
        else:
            # An identifier-shaped path is a type/message name: cover the
            # casings a consumer might use.
            hints.extend(n for n in _field_variants(path) if n != path)
    return tuple(hints)


def _remove_field_references(original_code: str, field_name: str, language: str) -> tuple[str, str]:
    """
    Remove all references to a deleted field from consumer code.
    Works across all languages by:
    1. Removing lines that contain the field name in common patterns
    2. Cleaning up dangling commas and empty blocks
    """
    lines = original_code.split("\n")
    removed_lines = []
    result_lines = []
    
    # Generate variants of the field name
    variants = _field_variants(field_name)
    
    for i, line in enumerate(lines):
        line_lower = line.lower().replace("-", "_").replace(" ", "")
        should_remove = False
        
        for variant in variants:
            variant_lower = variant.lower().replace("-", "_").replace(" ", "")
            if variant_lower in line_lower:
                # Check it's a meaningful reference (not a comment about something else)
                stripped = line.strip()
                # Remove lines that are: assignments, struct fields, params, dict keys, interface props
                if any([
                    f"{field_name}" in line and ("=" in line or ":" in line or "," in line),
                    f"'{field_name}'" in line,
                    f'"{field_name}"' in line,
                    f".{variant}" in line and variant != field_name[:3],  # method calls like .phoneNumber
                    f"{variant}:" in line,  # Go struct field
                    f"{variant} =" in line,  # assignment
                    f"{variant}," in line,  # param in list
                    f"self.{variant}" in line,  # Python instance attr
                    f"this.{variant}" in line,  # JS/TS instance
                ]):
                    should_remove = True
                    break
        
        if should_remove:
            removed_lines.append((i, line))
        else:
            result_lines.append(line)
    
    if not removed_lines:
        return original_code, "No references to removed field found"
    
    # Clean up dangling commas
    fixed_code = "\n".join(result_lines)
    # Remove trailing commas before closing braces/parens
    fixed_code = re.sub(r',\s*(\n\s*[}\])])', r'\1', fixed_code)
    # Remove double blank lines
    fixed_code = re.sub(r'\n{3,}', '\n\n', fixed_code)
    
    explanation = f"Removed {len(removed_lines)} reference(s) to deleted field '{field_name}'"
    return fixed_code, explanation


def _field_variants(field_name: str) -> list[str]:
    """Generate naming variants of a field: snake_case, camelCase, PascalCase, kebab-case."""
    variants = [field_name]
    
    # snake_case → camelCase
    parts = field_name.split("_")
    if len(parts) > 1:
        camel = parts[0] + "".join(p.capitalize() for p in parts[1:])
        pascal = "".join(p.capitalize() for p in parts)
        variants.extend([camel, pascal])
    
    # camelCase → snake_case
    snake = re.sub(r'([A-Z])', r'_\1', field_name).lower().lstrip('_')
    if snake != field_name:
        variants.append(snake)
    
    # kebab-case
    kebab = field_name.replace("_", "-")
    if kebab != field_name:
        variants.append(kebab)
    
    return list(set(variants))


def _compute_diff(original: str, fixed: str, filepath: str) -> str:
    """Compute a unified diff between original and fixed code."""
    import difflib
    
    original_lines = original.splitlines(keepends=True)
    fixed_lines = fixed.splitlines(keepends=True)
    
    diff = difflib.unified_diff(
        original_lines,
        fixed_lines,
        fromfile=f"a/{filepath}",
        tofile=f"b/{filepath}",
        lineterm="",
    )
    
    return "".join(diff)


def format_fixes(fixes: list[GeneratedFix]) -> str:
    """Format generated fixes for display."""
    if not fixes:
        return "  No fixes generated."
    
    lines = [
        f"  Generated {len(fixes)} fix(es):",
        "",
    ]
    
    for i, fix in enumerate(fixes, 1):
        lines.append(f"  ━━━ Fix [{i}]: {fix.consumer.file_path} ━━━")
        lines.append(f"  Language: {fix.consumer.language}")
        lines.append(f"  Explanation: {fix.explanation}")
        lines.append("")
        lines.append(fix.diff)
        lines.append("")
    
    return "\n".join(lines)
