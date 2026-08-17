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
    field_type = breaking_change.field_type
    
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
        from .fix_templates import apply_fix_template
        new_name = getattr(breaking_change, 'new_name', '') or ''
        if new_name:
            return apply_fix_template(
                code=original_code,
                language=consumer.language or "unknown",
                change_type="field_renamed",
                field_name=field_name,
                new_name=new_name,
            )
    
    if breaking_change.change_type in ("field_type_changed", "type_changed"):
        from .fix_templates import apply_fix_template
        old_type = getattr(breaking_change, 'old_type', '') or breaking_change.field_type.split(' → ')[0] if ' → ' in str(breaking_change.field_type) else ''
        new_type = breaking_change.field_type.split(' → ')[1] if ' → ' in str(breaking_change.field_type) else ''
        if old_type and new_type:
            return apply_fix_template(
                code=original_code,
                language=consumer.language or "unknown",
                change_type="type_changed",
                field_name=field_name,
                old_type=old_type,
                new_type=new_type,
            )
    
    if breaking_change.change_type != "added_required_field":
        return original_code, "Unsupported change type for template fix"
    
    fixed_code = original_code
    explanation = ""
    
    if consumer.language == "typescript":
        # Add to interface
        interface_pattern = re.compile(
            r'(interface\s+\w+Request\s*\{[^}]*?)(})',
            re.DOTALL
        )
        ts_type = "string" if field_type == "string" else "number" if field_type == "integer" else "any"
        match = interface_pattern.search(fixed_code)
        if match:
            fixed_code = interface_pattern.sub(
                rf'\1  {field_name}: {ts_type};\n\2',
                fixed_code
            )
        
        # Add to API call payload — find the object being passed to .post()
        # Look for the last property in the object literal before the closing }
        payload_pattern = re.compile(
            r'(\.post\([^{]*\{[^}]*?)(,?\n\s*\})',
            re.DOTALL
        )
        match = payload_pattern.search(fixed_code)
        if match:
            fixed_code = payload_pattern.sub(
                rf'\1,\n    {field_name}: data.{field_name}\2',
                fixed_code
            )
        
        explanation = f"Added '{field_name}' to interface and API call payload"
    
    elif consumer.language == "python":
        # The previous version of this branch was written against the demo
        # fixture: it matched a parameter literally named `age` and a payload
        # variable literally named `payload`. On ordinary code the signature
        # fallback matched while the payload regex missed, producing a function
        # that ACCEPTS the new field and never sends it -- and the explanation
        # was assigned unconditionally, so the PR body claimed the payload had
        # been updated. That compiles, so the syntax-only validator passes it.
        sig_ok = False
        payload_ok = False

        # 1. Signature. Any function whose body contains the call, not just
        #    `create_*`: the demo happened to use that prefix.
        func_pattern = re.compile(r'(def\s+\w+\s*\([^)]*?)(\)\s*(?:->[^:]+)?:)')
        if func_pattern.search(fixed_code) and f"{field_name}:" not in fixed_code:
            fixed_code, n = func_pattern.subn(
                rf'\1, {field_name}: str\2', fixed_code, count=1
            )
            sig_ok = bool(n)

        # 2. Payload. Cover the shapes that actually occur: an inline
        #    `json={...}` / `data={...}` / `body={...}` kwarg, and a named
        #    dict assigned beforehand.
        payload_patterns = (
            re.compile(r'((?:json|data|body)\s*=\s*\{)([^{}]*?)(\})', re.DOTALL),
            re.compile(r'((?:payload|body|data)\s*=\s*\{)([^{}]*?)(\})', re.DOTALL),
        )
        for pat in payload_patterns:
            m = pat.search(fixed_code)
            if not m:
                continue
            inner = m.group(2)
            if f'"{field_name}"' in inner or f"'{field_name}'" in inner:
                payload_ok = True  # already present, nothing to do
                break
            sep = "" if not inner.strip() else ", "
            fixed_code = (
                fixed_code[:m.start(2)] + inner.rstrip().rstrip(",")
                + sep + f'"{field_name}": {field_name}'
                + fixed_code[m.end(2):]
            )
            payload_ok = True
            break

        if sig_ok and payload_ok:
            explanation = (
                f"Added '{field_name}' parameter and included in request payload"
            )
        elif sig_ok:
            # Honest partial. Ripple's own contract for an incomplete fix is to
            # apply the safe part and flag the rest, never to overstate it.
            explanation = (
                f"Added '{field_name}' parameter to the signature but could NOT "
                f"locate the request payload -- RIPPLE-ACTION-REQUIRED: send "
                f"'{field_name}' in the request body yourself"
            )
        elif payload_ok:
            explanation = (
                f"Added '{field_name}' to the request payload but could NOT "
                f"locate the enclosing function signature -- "
                f"RIPPLE-ACTION-REQUIRED: thread '{field_name}' through callers"
            )
        else:
            # Nothing changed, so no PR opens -- silence is correct here.
            explanation = (
                f"No template fix applied for '{field_name}' (python): neither "
                f"a function signature nor a request payload matched"
            )

    elif consumer.language == "java":
        # Add parameter to method
        method_pattern = re.compile(
            r'(public\s+\w+\s+create\w+\([^)]*)',
            re.DOTALL
        )
        match = method_pattern.search(fixed_code)
        if match:
            insert_point = match.end()
            fixed_code = (
                fixed_code[:insert_point] +
                f", String {field_name}" +
                fixed_code[insert_point:]
            )
        
        # Add to JSON payload
        json_pattern = re.compile(
            r'(String\.format\(\s*"[^"]*)',
            re.DOTALL
        )
        match = json_pattern.search(fixed_code)
        if match:
            # Replace the JSON format string to include new field
            old_json = match.group(0)
            # Find the closing } in the JSON template
            fixed_code = fixed_code.replace(
                '{"name": "%s", "email": "%s"}',
                '{"name": "%s", "email": "%s", "' + field_name + '": "%s"}'
            )
            # Add the parameter to String.format args
            fixed_code = fixed_code.replace(
                "name, email",
                f"name, email, {field_name}"
            )
        
        explanation = f"Added '{field_name}' parameter and included in JSON payload"
    
    else:
        explanation = "Unsupported language for template fix"
    
    return fixed_code, explanation


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
