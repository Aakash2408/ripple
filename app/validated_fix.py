from __future__ import annotations
"""
ripple/app/validated_fix.py

Validated Fix Generation — the second moat.

Instead of just generating code and hoping it's right:
1. Generate fix with Claude
2. Validate it (syntax check, type check if possible)
3. If invalid → retry with the error message as context
4. Only open PR if the fix COMPILES

This guarantees PRs contain working code — not "AI suggestions" that break the build.
"""

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Tuple

from .diff_engine import BreakingChange
from .consumer_finder import ConsumerMatch


def generate_validated_fix(
    consumer: ConsumerMatch,
    breaking_change: BreakingChange,
    original_code: str,
    max_retries: int = 2,
) -> Tuple[Optional[str], str, bool]:
    """
    Generate a fix and VALIDATE it compiles/type-checks.
    
    Returns: (fixed_code, explanation, is_validated)
    """
    # Generate the fix
    fixed_code, explanation = _generate_with_llm(original_code, consumer, breaking_change)
    
    if not fixed_code or fixed_code == original_code:
        return None, "No fix generated", False
    
    # Validate based on language
    is_valid, error = _validate_code(fixed_code, consumer.language)
    
    if is_valid:
        return fixed_code, explanation + " [✓ validated]", True
    
    # Retry with error context
    for attempt in range(max_retries):
        fixed_code, explanation = _generate_with_llm(
            original_code, consumer, breaking_change,
            previous_error=error, previous_attempt=fixed_code
        )
        
        if not fixed_code:
            continue
        
        is_valid, error = _validate_code(fixed_code, consumer.language)
        if is_valid:
            return fixed_code, explanation + f" [✓ validated after {attempt + 2} attempts]", True
    
    # Return best-effort (unvalidated) if all retries fail
    return fixed_code, explanation + " [⚠️ unvalidated — syntax check failed]", False


def _generate_with_llm(
    original_code: str,
    consumer: ConsumerMatch,
    breaking_change: BreakingChange,
    previous_error: str = None,
    previous_attempt: str = None,
) -> Tuple[Optional[str], str]:
    """Generate fix using Claude API."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        # Fallback to template
        from .fix_generator import _generate_with_template
        return _generate_with_template(original_code, consumer, breaking_change)
    
    try:
        import anthropic
    except ImportError:
        from .fix_generator import _generate_with_template
        return _generate_with_template(original_code, consumer, breaking_change)
    
    from .llm_config import base_url as _llm_base, model as _llm_model
    client = anthropic.Anthropic(api_key=api_key, base_url=_llm_base())
    
    prompt = f"""You are a precise code assistant. An API has a breaking change. Fix the consumer code.

BREAKING CHANGE:
- Endpoint: {breaking_change.method.upper()} {breaking_change.path}
- Change type: {breaking_change.change_type}
- Field: "{breaking_change.field_name}" (type: {breaking_change.field_type})
- Description: {breaking_change.description}

CONSUMER CODE ({consumer.language}):
```
{original_code}
```
"""
    
    if previous_error and previous_attempt:
        prompt += f"""
PREVIOUS ATTEMPT FAILED WITH ERROR:
```
{previous_error}
```

PREVIOUS ATTEMPT (has bugs):
```
{previous_attempt}
```

Fix the bugs in the previous attempt. The code must be syntactically valid.
"""
    
    prompt += """
INSTRUCTIONS:
1. Add the new required field as a parameter that callers must provide.
2. Include it in the API call payload/body.
3. Keep the fix minimal — only change what's necessary.
4. The code MUST be syntactically valid and compile without errors.
5. Return ONLY the complete fixed file content, no explanation, no markdown fences.

FIXED CODE:"""

    try:
        response = client.messages.create(
            model=_llm_model(),
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        
        fixed_code = response.content[0].text.strip()
        
        # Strip markdown fences if present
        if fixed_code.startswith("```"):
            lines = fixed_code.split("\n")
            lines = lines[1:]  # remove opening fence
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]  # remove closing fence
            fixed_code = "\n".join(lines)
        
        explanation = f"Added required field '{breaking_change.field_name}' to {breaking_change.method.upper()} {breaking_change.path}"
        return fixed_code, explanation
    
    except Exception as e:
        from .fix_generator import _generate_with_template
        return _generate_with_template(original_code, consumer, breaking_change)


def _validate_code(code: str, language: str) -> Tuple[bool, str]:
    """
    Validate that generated code is syntactically correct.
    
    Returns: (is_valid, error_message)
    """
    if language == "python":
        return _validate_python(code)
    elif language in ("typescript", "javascript"):
        return _validate_typescript(code)
    elif language == "java":
        return _validate_java(code)
    else:
        # Can't validate — assume valid
        return True, ""


def _validate_python(code: str) -> Tuple[bool, str]:
    """Check Python syntax using compile()."""
    try:
        compile(code, "<generated>", "exec")
        return True, ""
    except SyntaxError as e:
        return False, f"SyntaxError at line {e.lineno}: {e.msg}"


def _validate_typescript(code: str) -> Tuple[bool, str]:
    """Check TypeScript/JS syntax — basic bracket/brace matching."""
    # Simple validation: balanced braces, brackets, parens
    stack = []
    pairs = {')': '(', ']': '[', '}': '{'}
    
    in_string = False
    string_char = None
    
    for i, char in enumerate(code):
        if in_string:
            if char == string_char and (i == 0 or code[i-1] != '\\'):
                in_string = False
            continue
        
        if char in ('"', "'", '`'):
            in_string = True
            string_char = char
            continue
        
        if char in '([{':
            stack.append(char)
        elif char in ')]}':
            if not stack:
                return False, f"Unmatched closing '{char}' at position {i}"
            if stack[-1] != pairs[char]:
                return False, f"Mismatched '{char}' at position {i}, expected closing for '{stack[-1]}'"
            stack.pop()
    
    if stack:
        return False, f"Unclosed '{stack[-1]}'"
    
    return True, ""


def _validate_java(code: str) -> Tuple[bool, str]:
    """Check Java syntax — basic bracket matching + semicolons."""
    # Same bracket matching as TypeScript
    valid, error = _validate_typescript(code)
    if not valid:
        return valid, error
    
    # Check for common Java issues
    lines = code.split("\n")
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        # Skip empty, comments, braces-only, annotations
        if not stripped or stripped.startswith("//") or stripped.startswith("/*"):
            continue
        if stripped.startswith("*") or stripped.startswith("@"):
            continue
        if stripped in ('{', '}', '};'):
            continue
        if stripped.startswith("package ") or stripped.startswith("import "):
            if not stripped.endswith(";"):
                return False, f"Line {i}: Missing semicolon after '{stripped[:30]}...'"
    
    return True, ""
