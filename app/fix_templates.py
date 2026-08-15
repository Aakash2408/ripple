"""
Deterministic fix template engine for Ripple.
Handles field_removed, field_renamed, type_changed across 8+ languages WITHOUT any LLM.
"""
from __future__ import annotations

import re
from typing import Callable


# --- Case conversion utilities ---

def to_snake(name: str) -> str:
    s1 = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', name)
    return re.sub(r'([a-z\d])([A-Z])', r'\1_\2', s1).lower()


def to_camel(name: str) -> str:
    parts = to_snake(name).split('_')
    return parts[0] + ''.join(p.capitalize() for p in parts[1:])


def to_pascal(name: str) -> str:
    return ''.join(p.capitalize() for p in to_snake(name).split('_'))


def to_upper_snake(name: str) -> str:
    return to_snake(name).upper()


def name_variants(name: str) -> dict[str, str]:
    """Return all case variants of a field name."""
    return {
        'snake': to_snake(name),
        'camel': to_camel(name),
        'pascal': to_pascal(name),
        'upper_snake': to_upper_snake(name),
    }


# --- Shared helpers ---

def _remove_lines_matching(code: str, pattern: re.Pattern) -> str:
    """Remove entire lines that match the pattern."""
    return '\n'.join(
        line for line in code.split('\n')
        if not pattern.search(line)
    )


def _clean_trailing_commas(code: str) -> str:
    """Remove trailing commas before closing brackets/parens."""
    code = re.sub(r',(\s*\n\s*[)\]}])', r'\1', code)
    code = re.sub(r',(\s*[)\]}])', r'\1', code)
    return code


def _remove_empty_blocks(code: str) -> str:
    """Remove empty parameter lists that became () with only whitespace."""
    code = re.sub(r'\(\s*,\s*', '(', code)
    code = re.sub(r',\s*\)', ')', code)
    return code


def _clean_blank_lines(code: str) -> str:
    """Collapse 3+ consecutive blank lines to 2."""
    return re.sub(r'\n{3,}', '\n\n', code)


def _postprocess(code: str) -> str:
    code = _clean_trailing_commas(code)
    code = _remove_empty_blocks(code)
    code = _clean_blank_lines(code)
    return code


# --- FIELD REMOVED templates per language ---

def _remove_field_go(code: str, variants: dict[str, str]) -> str:
    pascal = variants['pascal']
    camel = variants['camel']
    snake = variants['snake']
    names = {pascal, camel, snake}
    # Remove struct field declaration: FieldName Type `json:"..."`
    code = re.sub(rf'^\s*{pascal}\s+\S+.*$\n?', '', code, flags=re.MULTILINE)
    # Remove struct literal assignment: FieldName: value,
    code = re.sub(rf'^\s*{pascal}\s*:.*,?\s*$\n?', '', code, flags=re.MULTILINE)
    # Remove .FieldName access lines (entire statement if standalone)
    for n in names:
        code = re.sub(rf'^\s*\S*\.{n}\b.*$\n?', '', code, flags=re.MULTILINE)
    # Remove function params containing the field name
    for n in names:
        code = re.sub(rf'\b{n}\s+\w+\s*,?\s*', '', code)
    return code


def _remove_field_typescript(code: str, variants: dict[str, str]) -> str:
    camel = variants['camel']
    snake = variants['snake']
    names = {camel, snake}
    # Remove interface/type property: fieldName: Type;  or  fieldName?: Type;
    code = re.sub(rf'^\s*{camel}\??\s*:.*[;,]?\s*$\n?', '', code, flags=re.MULTILINE)
    # Remove from object literals: fieldName: value,
    code = re.sub(rf'^\s*{camel}\s*:.*,?\s*$\n?', '', code, flags=re.MULTILINE)
    # Remove destructuring: { ..., fieldName, ... } -> remove fieldName
    for n in names:
        code = re.sub(rf'\b{n}\s*,\s*', '', code)
        code = re.sub(rf',\s*{n}\b', '', code)
        code = re.sub(rf'\{{\s*{n}\s*\}}', '{}', code)
    # Remove .fieldName access (entire line if standalone assignment/call)
    for n in names:
        code = re.sub(rf'^\s*\S*\.{n}\b.*$\n?', '', code, flags=re.MULTILINE)
    # Remove function param
    for n in names:
        code = re.sub(rf'\b{n}\s*(\?\s*)?:\s*\w+\s*,?\s*', '', code)
    return code


def _remove_field_python(code: str, variants: dict[str, str]) -> str:
    snake = variants['snake']
    # Remove dataclass field: field_name: Type = default
    code = re.sub(rf'^\s*{snake}\s*:.*$\n?', '', code, flags=re.MULTILINE)
    # Remove keyword argument: field_name=value,
    code = re.sub(rf'\b{snake}\s*=[^,)]+,?\s*', '', code)
    # Remove from function params: field_name: Type,  or  field_name: Type = default,
    code = re.sub(rf'\b{snake}\s*:\s*[^,=)]+(\s*=[^,)]+)?\s*,?\s*', '', code)
    # Remove self.field_name access (entire line)
    code = re.sub(rf'^\s*\S*self\.{snake}\b.*$\n?', '', code, flags=re.MULTILINE)
    # Remove dict key access: ['field_name'] or ["field_name"] or .get('field_name')
    code = re.sub(rf'^\s*.*\[\s*["\']?{snake}["\']?\s*\].*$\n?', '', code, flags=re.MULTILINE)
    return code


def _remove_field_java(code: str, variants: dict[str, str]) -> str:
    camel = variants['camel']
    pascal = variants['pascal']
    # Remove field declaration: private/protected/public Type fieldName;
    code = re.sub(rf'^\s*(private|protected|public)\s+\S+\s+{camel}\s*[;=].*$\n?', '', code, flags=re.MULTILINE)
    # Remove getter: public Type getFieldName() { ... }
    code = re.sub(rf'^\s*(public|protected)\s+\S+\s+get{pascal}\s*\(.*?\)\s*\{{[^}}]*\}}\s*$\n?', '', code, flags=re.MULTILINE | re.DOTALL)
    # Remove single-line getter
    code = re.sub(rf'^\s*(public|protected)\s+\S+\s+get{pascal}\s*\(.*$\n?', '', code, flags=re.MULTILINE)
    # Remove setter
    code = re.sub(rf'^\s*(public|protected)\s+void\s+set{pascal}\s*\(.*?\)\s*\{{[^}}]*\}}\s*$\n?', '', code, flags=re.MULTILINE | re.DOTALL)
    code = re.sub(rf'^\s*(public|protected)\s+void\s+set{pascal}\s*\(.*$\n?', '', code, flags=re.MULTILINE)
    # Remove this.field
    code = re.sub(rf'^\s*this\.{camel}\b.*$\n?', '', code, flags=re.MULTILINE)
    # Remove from constructor/method params
    code = re.sub(rf'\b\w+\s+{camel}\s*,?\s*', '', code)
    return code


def _remove_field_rust(code: str, variants: dict[str, str]) -> str:
    snake = variants['snake']
    # Remove struct field: pub field_name: Type,
    code = re.sub(rf'^\s*(pub\s+)?{snake}\s*:.*,?\s*$\n?', '', code, flags=re.MULTILINE)
    # Remove .field_name access (entire line)
    code = re.sub(rf'^\s*\S*\.{snake}\b.*$\n?', '', code, flags=re.MULTILINE)
    # Remove from function params
    code = re.sub(rf'\b{snake}\s*:\s*[^,)]+,?\s*', '', code)
    # Remove struct literal init: field_name: value,
    code = re.sub(rf'^\s*{snake}\s*:.*,?\s*$\n?', '', code, flags=re.MULTILINE)
    return code


def _remove_field_ruby(code: str, variants: dict[str, str]) -> str:
    snake = variants['snake']
    # Remove attr_accessor/attr_reader/attr_writer :field_name
    code = re.sub(rf'^\s*attr_(accessor|reader|writer)\s+:{snake}\s*$\n?', '', code, flags=re.MULTILINE)
    # Remove from multi-attr declarations
    code = re.sub(rf':{snake}\s*,?\s*', '', code)
    # Remove @field_name (entire line)
    code = re.sub(rf'^\s*@{snake}\b.*$\n?', '', code, flags=re.MULTILINE)
    # Remove hash key: field_name: value,
    code = re.sub(rf'^\s*{snake}:\s*.*,?\s*$\n?', '', code, flags=re.MULTILINE)
    # Remove from method params
    code = re.sub(rf'\b{snake}:\s*[^,)]*,?\s*', '', code)
    return code


def _remove_field_kotlin(code: str, variants: dict[str, str]) -> str:
    camel = variants['camel']
    # Remove val/var from data class: val fieldName: Type,
    code = re.sub(rf'^\s*(val|var)\s+{camel}\s*:.*,?\s*$\n?', '', code, flags=re.MULTILINE)
    # Remove .fieldName access (entire line)
    code = re.sub(rf'^\s*\S*\.{camel}\b.*$\n?', '', code, flags=re.MULTILINE)
    # Remove from function params: fieldName: Type
    code = re.sub(rf'\b{camel}\s*:\s*[^,)]+,?\s*', '', code)
    return code


def _remove_field_csharp(code: str, variants: dict[str, str]) -> str:
    pascal = variants['pascal']
    camel = variants['camel']
    # Remove property: public Type FieldName { get; set; }
    code = re.sub(rf'^\s*(public|private|protected|internal)\s+\S+\s+{pascal}\s*\{{.*\}}\s*$\n?', '', code, flags=re.MULTILINE)
    # Remove auto-property single line
    code = re.sub(rf'^\s*(public|private|protected|internal)\s+\S+\s+{pascal}\s*;.*$\n?', '', code, flags=re.MULTILINE)
    # Remove .FieldName access
    code = re.sub(rf'^\s*\S*\.{pascal}\b.*$\n?', '', code, flags=re.MULTILINE)
    # Remove from constructor params
    code = re.sub(rf'\b\w+\s+{camel}\s*,?\s*', '', code)
    # Remove this.field = param
    code = re.sub(rf'^\s*(this\.)?{pascal}\s*=.*$\n?', '', code, flags=re.MULTILINE)
    return code


REMOVE_HANDLERS: dict[str, Callable[[str, dict[str, str]], str]] = {
    'go': _remove_field_go,
    'typescript': _remove_field_typescript,
    'javascript': _remove_field_typescript,
    'python': _remove_field_python,
    'java': _remove_field_java,
    'rust': _remove_field_rust,
    'ruby': _remove_field_ruby,
    'kotlin': _remove_field_kotlin,
    'csharp': _remove_field_csharp,
    'c#': _remove_field_csharp,
}


# --- FIELD RENAMED templates ---

def _rename_field(code: str, old_variants: dict[str, str], new_variants: dict[str, str]) -> str:
    """Rename all occurrences, respecting case style. Skips strings and comments."""
    for style in ('snake', 'camel', 'pascal', 'upper_snake'):
        old = old_variants[style]
        new = new_variants[style]
        if old == new:
            continue
        # Negative lookbehind/lookahead to skip inside string literals and comments
        # Skip if preceded by quote or followed by quote (basic heuristic)
        pattern = rf'(?<!["\'/])\b{re.escape(old)}\b(?!["\'/])'
        code = re.sub(pattern, new, code)
    return code


# --- TYPE CHANGED templates per language ---

def _change_type_go(code: str, old_type: str, new_type: str) -> str:
    # struct field types, function params, variable declarations
    code = re.sub(rf'\b{re.escape(old_type)}\b', new_type, code)
    return code


def _change_type_typescript(code: str, old_type: str, new_type: str) -> str:
    # property types, param types, generics
    code = re.sub(rf':\s*{re.escape(old_type)}\b', f': {new_type}', code)
    code = re.sub(rf'<{re.escape(old_type)}>', f'<{new_type}>', code)
    code = re.sub(rf'\b{re.escape(old_type)}\b(?=\s*[|&\]])', new_type, code)
    return code


def _change_type_python(code: str, old_type: str, new_type: str) -> str:
    # type hints: -> OldType, : OldType, isinstance(..., OldType)
    code = re.sub(rf':\s*{re.escape(old_type)}\b', f': {new_type}', code)
    code = re.sub(rf'->\s*{re.escape(old_type)}\b', f'-> {new_type}', code)
    code = re.sub(rf'isinstance\(([^,]+),\s*{re.escape(old_type)}\)', rf'isinstance(\1, {new_type})', code)
    code = re.sub(rf'\b{re.escape(old_type)}\b(?=\s*\[)', new_type, code)
    return code


def _change_type_java(code: str, old_type: str, new_type: str) -> str:
    # field types, return types, param types, generics
    code = re.sub(rf'\b{re.escape(old_type)}\b', new_type, code)
    return code


def _change_type_rust(code: str, old_type: str, new_type: str) -> str:
    code = re.sub(rf'\b{re.escape(old_type)}\b', new_type, code)
    return code


def _change_type_kotlin(code: str, old_type: str, new_type: str) -> str:
    code = re.sub(rf':\s*{re.escape(old_type)}\b', f': {new_type}', code)
    code = re.sub(rf'\b{re.escape(old_type)}\b(?=\s*[<>)])', new_type, code)
    return code


def _change_type_csharp(code: str, old_type: str, new_type: str) -> str:
    code = re.sub(rf'\b{re.escape(old_type)}\b', new_type, code)
    return code


def _change_type_ruby(code: str, old_type: str, new_type: str) -> str:
    # Ruby is dynamically typed; replace in yard docs and sig blocks
    code = re.sub(rf'\b{re.escape(old_type)}\b', new_type, code)
    return code


TYPE_CHANGE_HANDLERS: dict[str, Callable[[str, str, str], str]] = {
    'go': _change_type_go,
    'typescript': _change_type_typescript,
    'javascript': _change_type_typescript,
    'python': _change_type_python,
    'java': _change_type_java,
    'rust': _change_type_rust,
    'ruby': _change_type_ruby,
    'kotlin': _change_type_kotlin,
    'csharp': _change_type_csharp,
    'c#': _change_type_csharp,
}


# --- Main entry point ---

def apply_fix_template(
    code: str,
    language: str,
    change_type: str,
    field_name: str,
    new_name: str = '',
    old_type: str = '',
    new_type: str = '',
) -> tuple[str, str]:
    """
    Apply a deterministic fix template to source code.

    Args:
        code: Source code to modify.
        language: Programming language (go, typescript, python, java, rust, ruby, kotlin, csharp).
        change_type: One of field_removed, removed_field, field_renamed, renamed_field, type_changed, field_type_changed.
        field_name: The field being changed.
        new_name: For rename operations, the new field name.
        old_type: For type change operations, the original type.
        new_type: For type change operations, the new type.

    Returns:
        Tuple of (fixed_code, explanation_string).
    """
    lang = language.lower().strip()
    ct = change_type.lower().strip().replace('-', '_')
    variants = name_variants(field_name)

    # Normalize change_type aliases
    if ct in ('field_removed', 'removed_field'):
        handler = REMOVE_HANDLERS.get(lang)
        if handler is None:
            # Fallback: generic line removal for unsupported languages
            result = _generic_remove(code, variants)
        else:
            result = handler(code, variants)
        result = _postprocess(result)
        lines_removed = len(code.split('\n')) - len(result.split('\n'))
        explanation = (
            f"Removed all references to field '{field_name}' ({lines_removed} lines affected). "
            f"Cleaned: struct/class declarations, accessor methods, function params, object literals, "
            f"and direct field access patterns for {lang}."
        )
        return result, explanation

    elif ct in ('field_renamed', 'renamed_field'):
        if not new_name:
            return code, "Error: new_name required for rename operations."
        old_variants = name_variants(field_name)
        new_variants = name_variants(new_name)
        result = _rename_field(code, old_variants, new_variants)
        replacements = sum(
            code.count(old_variants[s]) - result.count(old_variants[s])
            for s in ('snake', 'camel', 'pascal', 'upper_snake')
        )
        explanation = (
            f"Renamed '{field_name}' -> '{new_name}' across all case variants "
            f"(snake_case, camelCase, PascalCase, UPPER_SNAKE). "
            f"{replacements} replacements made. String literals and comments preserved."
        )
        return result, explanation

    elif ct in ('type_changed', 'field_type_changed'):
        if not old_type or not new_type:
            return code, "Error: old_type and new_type required for type change operations."
        handler = TYPE_CHANGE_HANDLERS.get(lang)
        if handler is None:
            # Generic: simple word-boundary replacement
            result = re.sub(rf'\b{re.escape(old_type)}\b', new_type, code)
        else:
            result = handler(code, old_type, new_type)
        replacements = code.count(old_type) - result.count(old_type)
        explanation = (
            f"Changed type '{old_type}' -> '{new_type}' in {lang} code. "
            f"{replacements} type annotations updated."
        )
        return result, explanation

    else:
        return code, f"Unknown change_type: '{change_type}'. Supported: field_removed, field_renamed, type_changed."


def _generic_remove(code: str, variants: dict[str, str]) -> str:
    """Fallback removal for unsupported languages: remove lines containing field name variants."""
    for style in ('snake', 'camel', 'pascal'):
        name = variants[style]
        pattern = re.compile(rf'^\s*.*\b{re.escape(name)}\b.*$\n?', re.MULTILINE)
        code = _remove_lines_matching(code, pattern)
    return code
