from __future__ import annotations
"""
ripple/app/smart_consumer_finder.py

AST-aware consumer finding -- replaces naive grep with language-specific
pattern matching that reduces false positives.

Instead of "any line containing phone_number", this module:
1. Generates naming variants (snake_case, camelCase, PascalCase)
2. Uses language-specific patterns to identify REAL usage (not comments/strings)
3. Scores each match by confidence (struct field access > string mention)
"""

import re
from dataclasses import dataclass


@dataclass
class SmartMatch:
    """A consumer match with confidence and context."""
    file_path: str
    line_number: int
    line_content: str
    match_type: str  # "field_access", "param", "assignment", "import", "comment", "string_literal"
    confidence: float  # 0.0-1.0
    variant_matched: str  # which variant was found


# ─────────────────────────────────────────────────────────────────────────────
# Naming Variant Generation
# ─────────────────────────────────────────────────────────────────────────────

def generate_variants(field_name: str) -> list[str]:
    """Generate all naming convention variants of a field name."""
    variants = set()
    variants.add(field_name)  # original
    
    # Split into parts
    if "_" in field_name:
        parts = field_name.split("_")
    elif any(c.isupper() for c in field_name[1:]):
        # camelCase or PascalCase → split on uppercase
        parts = re.sub(r'([A-Z])', r'_\1', field_name).lower().strip('_').split('_')
    else:
        parts = [field_name]
    
    # Generate variants
    snake = "_".join(parts).lower()
    camel = parts[0].lower() + "".join(p.capitalize() for p in parts[1:])
    pascal = "".join(p.capitalize() for p in parts)
    upper_snake = "_".join(parts).upper()
    kebab = "-".join(parts).lower()
    
    variants.update([snake, camel, pascal, upper_snake, kebab])
    
    # Proto-specific: phone_number → PhoneNumber (Go generated), phone_number (Python pb2)
    # Also: getPhoneNumber, setPhoneNumber, hasPhoneNumber (Java getters/setters)
    variants.add(f"get{pascal}")
    variants.add(f"set{pascal}")
    variants.add(f"has{pascal}")
    variants.add(f"Get{pascal}")  # Go exported
    
    return [v for v in variants if v and len(v) > 2]


# ─────────────────────────────────────────────────────────────────────────────
# Language-Aware Pattern Matching
# ─────────────────────────────────────────────────────────────────────────────

def classify_match(line: str, variant: str, language: str) -> tuple[str, float]:
    """
    Classify what kind of usage this is and assign confidence.
    Returns (match_type, confidence).
    """
    stripped = line.strip()
    
    # LOW confidence: comments
    if _is_comment(stripped, language):
        return ("comment", 0.1)
    
    # LOW confidence: string literals (except proto field names in quotes)
    if _is_string_literal_only(stripped, variant, language):
        return ("string_literal", 0.3)
    
    # HIGH confidence: language-specific patterns
    if language == "go":
        return _classify_go(stripped, variant)
    elif language in ("typescript", "javascript"):
        return _classify_typescript(stripped, variant)
    elif language == "python":
        return _classify_python(stripped, variant)
    elif language == "java":
        return _classify_java(stripped, variant)
    elif language == "rust":
        return _classify_rust(stripped, variant)
    elif language == "ruby":
        return _classify_ruby(stripped, variant)
    elif language == "kotlin":
        return _classify_kotlin(stripped, variant)
    elif language in ("csharp", "c#"):
        return _classify_csharp(stripped, variant)
    
    # Default: medium confidence
    return ("unknown_usage", 0.6)


def _is_comment(line: str, language: str) -> bool:
    """Check if line is a comment."""
    if line.startswith("//") or line.startswith("#") or line.startswith("*") or line.startswith("/*"):
        return True
    if language == "python" and (line.startswith('"""') or line.startswith("'''")):
        return True
    return False


def _is_string_literal_only(line: str, variant: str, language: str) -> bool:
    """Check if the variant only appears inside a string literal (not as code)."""
    # If variant appears in quotes but not outside them, it's a string
    in_quotes = re.findall(r'["\']([^"\']*)["\']', line)
    outside_quotes = re.sub(r'["\'][^"\']*["\']', '', line)
    
    if variant in outside_quotes:
        return False  # It's used as code too
    if any(variant in q for q in in_quotes):
        return True  # Only in strings
    return False


def _classify_go(line: str, variant: str) -> tuple[str, float]:
    """Go-specific pattern matching."""
    # Struct field assignment: PhoneNumber: phone
    if re.search(rf'{variant}\s*:', line):
        return ("field_access", 0.95)
    # Method call: .GetPhoneNumber() or .PhoneNumber
    if re.search(rf'\.\s*{variant}\s*[(\[]?', line):
        return ("field_access", 0.95)
    # Function parameter: phone string
    if re.search(rf'{variant}\s+\w+[,)]', line):
        return ("param", 0.9)
    # Variable declaration: phone :=
    if re.search(rf'{variant}\s*:?=', line):
        return ("assignment", 0.85)
    return ("usage", 0.7)


def _classify_typescript(line: str, variant: str) -> tuple[str, float]:
    """TypeScript/JavaScript-specific pattern matching."""
    # Interface/type property: phoneNumber: string
    if re.search(rf'{variant}\s*[?:]?\s*:', line):
        return ("field_access", 0.95)
    # Object property: phoneNumber: value or phoneNumber,
    if re.search(rf'{variant}\s*[,}]', line):
        return ("field_access", 0.9)
    # Destructuring: { phoneNumber } or { phoneNumber: alias }
    if re.search(rf'\{[^}}]*{variant}[^}}]*\}}', line):
        return ("field_access", 0.9)
    # Dot access: user.phoneNumber
    if re.search(rf'\.\s*{variant}', line):
        return ("field_access", 0.95)
    # Parameter: (phoneNumber: string)
    if re.search(rf'[(,]\s*{variant}\s*[?:]', line):
        return ("param", 0.9)
    return ("usage", 0.7)


def _classify_python(line: str, variant: str) -> tuple[str, float]:
    """Python-specific pattern matching."""
    # Dataclass/class field: phone_number: str
    if re.search(rf'{variant}\s*:', line) and not line.strip().startswith('#'):
        return ("field_access", 0.95)
    # Keyword argument: phone_number=value
    if re.search(rf'{variant}\s*=', line):
        return ("assignment", 0.9)
    # Attribute access: self.phone_number or obj.phone_number
    if re.search(rf'\.{variant}', line):
        return ("field_access", 0.95)
    # Function parameter: def func(phone_number, ...)
    if re.search(rf'def\s+\w+\([^)]*{variant}', line):
        return ("param", 0.9)
    # Dict key: ['phone_number'] or ["phone_number"]
    if re.search(rf'[\["\']' + variant + r'["\'\]]', line):
        return ("field_access", 0.85)
    return ("usage", 0.7)


def _classify_java(line: str, variant: str) -> tuple[str, float]:
    """Java-specific pattern matching."""
    # Getter/setter call: getPhoneNumber() or setPhoneNumber(x)
    if re.search(rf'(get|set|has){variant[0].upper() + variant[1:]}\s*\(', line, re.IGNORECASE):
        return ("field_access", 0.95)
    # Field declaration: private String phoneNumber
    if re.search(rf'(private|public|protected)\s+\w+\s+{variant}', line):
        return ("field_access", 0.95)
    # Method parameter: String phoneNumber
    if re.search(rf'\w+\s+{variant}\s*[,)]', line):
        return ("param", 0.9)
    # Dot access: user.phoneNumber or user.getPhoneNumber()
    if re.search(rf'\.{variant}', line):
        return ("field_access", 0.9)
    return ("usage", 0.7)


def _classify_rust(line: str, variant: str) -> tuple[str, float]:
    """Rust-specific pattern matching."""
    # Struct field: phone_number: String
    if re.search(rf'{variant}\s*:', line):
        return ("field_access", 0.95)
    # Field access: .phone_number
    if re.search(rf'\.{variant}', line):
        return ("field_access", 0.95)
    # Function param: phone_number: &str
    if re.search(rf'{variant}\s*:\s*&', line):
        return ("param", 0.9)
    return ("usage", 0.7)


def _classify_ruby(line: str, variant: str) -> tuple[str, float]:
    """Ruby-specific pattern matching."""
    # Symbol: :phone_number
    if re.search(rf':{variant}', line):
        return ("field_access", 0.9)
    # Attr accessor: attr_accessor :phone_number
    if re.search(rf'attr_\w+\s+:{variant}', line):
        return ("field_access", 0.95)
    # Hash key: phone_number:
    if re.search(rf'{variant}:', line):
        return ("field_access", 0.9)
    # Method call: .phone_number
    if re.search(rf'\.{variant}', line):
        return ("field_access", 0.9)
    return ("usage", 0.7)


def _classify_kotlin(line: str, variant: str) -> tuple[str, float]:
    """Kotlin-specific pattern matching."""
    # Property: val phoneNumber: String
    if re.search(rf'(val|var)\s+{variant}\s*:', line):
        return ("field_access", 0.95)
    # Named argument: phoneNumber = value
    if re.search(rf'{variant}\s*=', line):
        return ("assignment", 0.9)
    # Dot access: user.phoneNumber
    if re.search(rf'\.{variant}', line):
        return ("field_access", 0.95)
    return ("usage", 0.7)


def _classify_csharp(line: str, variant: str) -> tuple[str, float]:
    """C#-specific pattern matching."""
    # Property: public string PhoneNumber { get; set; }
    if re.search(rf'(public|private|internal)\s+\w+\s+{variant}', line):
        return ("field_access", 0.95)
    # Dot access: user.PhoneNumber
    if re.search(rf'\.{variant}', line):
        return ("field_access", 0.95)
    # Named argument: PhoneNumber = value or PhoneNumber: value
    if re.search(rf'{variant}\s*[=:]', line):
        return ("assignment", 0.9)
    return ("usage", 0.7)


# ─────────────────────────────────────────────────────────────────────────────
# Main Search Function
# ─────────────────────────────────────────────────────────────────────────────

def find_field_consumers(
    file_content: str,
    file_path: str,
    field_name: str,
    language: str,
    min_confidence: float = 0.5,
) -> list[SmartMatch]:
    """
    Find all references to a field in a file with confidence scoring.
    
    Returns only matches above min_confidence, sorted by confidence desc.
    Filters out comments, string literals, and other false positives.
    """
    variants = generate_variants(field_name)
    matches = []
    
    lines = file_content.split("\n")
    for i, line in enumerate(lines, 1):
        for variant in variants:
            if variant.lower() in line.lower():
                match_type, confidence = classify_match(line, variant, language)
                
                if confidence >= min_confidence:
                    matches.append(SmartMatch(
                        file_path=file_path,
                        line_number=i,
                        line_content=line.strip(),
                        match_type=match_type,
                        confidence=confidence,
                        variant_matched=variant,
                    ))
                    break  # Only count each line once
    
    # Sort by confidence descending
    matches.sort(key=lambda m: m.confidence, reverse=True)
    return matches


def file_is_consumer(
    file_content: str,
    file_path: str,
    field_name: str,
    language: str,
    min_confidence: float = 0.5,
) -> tuple[bool, float, list[SmartMatch]]:
    """
    Determine if a file is a consumer of the given field.
    
    Returns (is_consumer, overall_confidence, matches).
    A file is a consumer if it has at least one high-confidence match
    that isn't just a comment or string.
    """
    matches = find_field_consumers(file_content, file_path, field_name, language, min_confidence)
    
    if not matches:
        return (False, 0.0, [])
    
    # Filter out comment-only and string-only matches
    real_matches = [m for m in matches if m.match_type not in ("comment", "string_literal")]
    
    if not real_matches:
        return (False, 0.0, matches)
    
    # Overall confidence = max of real matches
    overall_confidence = max(m.confidence for m in real_matches)
    
    return (True, overall_confidence, real_matches)


def search_content_for_field(
    content: str,
    field_name: str,
    language: str,
) -> bool:
    """Quick check: does this file contain meaningful references to the field?
    
    Faster than find_field_consumers -- used for initial filtering before
    fetching full file content.
    """
    variants = generate_variants(field_name)
    content_lower = content.lower()
    
    for variant in variants:
        if variant.lower() in content_lower:
            # Quick false-positive filter: not just in a comment
            for line in content.split("\n"):
                if variant.lower() in line.lower():
                    if not _is_comment(line.strip(), language):
                        return True
    
    return False
