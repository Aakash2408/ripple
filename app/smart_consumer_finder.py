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
    """Generate all naming convention variants of a field name.

    Returns them in a DETERMINISTIC, priority order: the canonical naming
    conventions first, then accessor-prefixed forms.

    This previously built a `set()` and returned it directly. Python
    randomizes string hashes per process, so the order changed on every run,
    with two consequences:

      * file_is_consumer() reported the confidence of whichever variant
        happened to match first, so the same file scored 0.95 on one run and
        0.70 on the next.
      * the code-search query takes only the first few variants, so which
        ones got searched was random -- a snake_case Python consumer could be
        found on one run and silently missed on the next.

    Order is now fixed, so discovery and scoring are reproducible.
    """
    ordered = []

    def add(v):
        if v and len(v) > 2 and v not in ordered:
            ordered.append(v)

    # Split into parts
    if "_" in field_name:
        parts = field_name.split("_")
    elif any(c.isupper() for c in field_name[1:]):
        # camelCase or PascalCase → split on uppercase
        parts = re.sub(r'([A-Z])', r'_\1', field_name).lower().strip('_').split('_')
    else:
        parts = [field_name]

    snake = "_".join(parts).lower()
    camel = parts[0].lower() + "".join(p.capitalize() for p in parts[1:])
    pascal = "".join(p.capitalize() for p in parts)
    upper_snake = "_".join(parts).upper()
    kebab = "-".join(parts).lower()

    # Canonical conventions first -- these are the declaration-site names
    # that actually need rewriting, and the ones worth spending a limited
    # search query on.
    add(field_name)      # exactly as written in the contract
    add(snake)           # python, proto, ruby
    add(camel)           # typescript, javascript, java fields
    add(pascal)          # go exported, c#
    add(upper_snake)     # constants
    add(kebab)           # json/yaml keys

    # Accessor forms last: they are usages, not declarations.
    for prefix in ("get", "set", "has", "Get", "Set", "Has"):
        add(f"{prefix}{pascal}")

    return ordered


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
    elif language == "yaml":
        return _classify_yaml(stripped, variant)
    elif language == "shell":
        return _classify_shell(stripped, variant)
    
    # Default: medium confidence
    return ("unknown_usage", 0.6)


# ─────────────────────────────────────────────────────────────────────────────
# Config and script languages
# ─────────────────────────────────────────────────────────────────────────────
#
# Measured on the PropBench replay: 137 files that a real merged PR had to change
# were skipped entirely because Ripple had no matcher for their language. On
# kubernetes#109798 that was 24 of 36 label files -- YAML manifests and shell
# scripts, i.e. MOST of the actual work in that change. No amount of tuning the
# code matchers reaches them.
#
# These differ from code languages in one important way: a reference inside
# quotes is a REAL reference, not a false positive. `name: "podsecuritypolicy"`
# is exactly as meaningful as the unquoted form, so the string-literal demotion
# that protects code matchers must not apply here.

def _classify_yaml(line: str, variant: str) -> tuple[str, float]:
    """Classify a reference in a YAML/manifest file.

    Case-INSENSITIVE, unlike the code classifiers. find_field_consumers matches
    variants case-insensitively but code classifiers then match case-sensitively
    -- a mismatch that already caused one bug in this file. For config it is
    worse, because the needed variant is often not generated at all:
    generate_variants('podsecuritypolicy') cannot produce 'PodSecurityPolicy',
    since splitting an unseparated lowercase compound needs a dictionary. So
    `kind: PodSecurityPolicy` fell through every specific pattern to the 0.70
    fallback. In config, PodSecurityPolicy / podsecuritypolicy /
    pod-security-policy all denote the same resource.
    """
    I = re.IGNORECASE
    # `variant:` as a mapping key -- the strongest signal.
    if re.match(rf'^-?\s*["\']?{re.escape(variant)}["\']?\s*:', line, I):
        return ("yaml_key", 0.95)
    # `- variant` as a sequence item (RBAC rules, resource lists).
    if re.match(rf'^-\s*["\']?{re.escape(variant)}["\']?\s*$', line, I):
        return ("yaml_list_item", 0.92)
    # `key: variant` or `key: [a, variant]` -- referenced as a value.
    if re.search(rf':\s*.*(?<![A-Za-z0-9]){re.escape(variant)}(?![A-Za-z0-9])', line, I):
        return ("yaml_value", 0.90)
    return ("yaml_reference", 0.70)


def _classify_shell(line: str, variant: str) -> tuple[str, float]:
    """Classify a reference in a shell script. Case-insensitive -- see
    _classify_yaml for why config languages differ from code here."""
    I = re.IGNORECASE
    # $VAR / ${VAR}
    if re.search(rf'\$\{{?{re.escape(variant)}\b', line, I):
        return ("shell_variable", 0.95)
    # VAR= assignment (including export / local / readonly)
    if re.match(rf'^(?:export\s+|local\s+|readonly\s+|declare\s+-\w+\s+)?'
                rf'{re.escape(variant)}=', line, I):
        return ("shell_assignment", 0.95)
    # A path segment or flag value -- how manifests and dirs get referenced.
    if re.search(rf'(?<![A-Za-z0-9])[-/\w.]*{re.escape(variant)}[-/\w.]*', line, I):
        return ("shell_argument", 0.80)
    return ("shell_reference", 0.70)


def _is_comment(line: str, language: str) -> bool:
    """Check if line is a comment."""
    if line.startswith("//") or line.startswith("#") or line.startswith("*") or line.startswith("/*"):
        return True
    if language == "python" and (line.startswith('"""') or line.startswith("'''")):
        return True
    return False


def _is_string_literal_only(line: str, variant: str, language: str) -> bool:
    """Check if the variant only appears inside a string literal (not as code).

    Does NOT apply to config and script languages. In YAML a quoted value is a
    real reference -- `name: "podsecuritypolicy"` means exactly what the
    unquoted form means -- and in shell, quoted paths and arguments are the
    normal way to reference something. Demoting those to 0.3 would put them
    below min_confidence and silently drop the very files these matchers were
    added to catch.
    """
    if language in ("yaml", "shell"):
        return False

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
    if re.search(rf'{variant}\s*[,\}}]', line):
        return ("field_access", 0.9)
    # Destructuring: { phoneNumber } or { phoneNumber: alias }
    if re.search(rf'\{{[^}}]*{variant}[^}}]*\}}', line):
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

    Per line, EVERY matching variant is classified and the strongest result
    wins. The previous version broke out of the variant loop on the first
    case-insensitive hit, which was wrong in a subtle way: the membership
    test (`variant.lower() in line.lower()`) is case-insensitive, but
    classify_match uses case-SENSITIVE regexes. So for a Go line like

        PhoneNumber: phone,

    both `phoneNumber` and `PhoneNumber` pass the membership test, yet only
    `PhoneNumber` matches rf'{variant}\\s*:' and scores 0.95 as a field
    assignment. Whichever variant came first was locked in -- and variant
    order used to be randomised per process, so the same file scored 0.95 on
    one run and 0.70 on the next.
    """
    variants = generate_variants(field_name)
    matches = []
    
    lines = file_content.split("\n")
    for i, line in enumerate(lines, 1):
        lowered = line.lower()
        best = None
        for variant in variants:
            if variant.lower() not in lowered:
                continue
            match_type, confidence = classify_match(line, variant, language)
            if best is None or confidence > best[1]:
                best = (match_type, confidence, variant)
        
        if best is not None and best[1] >= min_confidence:
            matches.append(SmartMatch(
                file_path=file_path,
                line_number=i,
                line_content=line.strip(),
                match_type=best[0],
                confidence=best[1],
                variant_matched=best[2],
            ))
    
    # Sort by confidence descending
    matches.sort(key=lambda m: m.confidence, reverse=True)
    return matches


# ─────────────────────────────────────────────────────────────────────────────
# Path-locality matching (PACKAGE vector)
# ─────────────────────────────────────────────────────────────────────────────
#
# A breaking change propagates by one of two vectors:
#
#   symbol   a declared identifier was removed; consumers name it
#   package  a directory was deleted; consumers are files INSIDE it, plus files
#            that import its path -- most of which never name any single symbol
#
# Ripple only had the symbol matcher, so package deletions were largely
# invisible to it. Measured on kubernetes#109798 ("Remove PodSecurityPolicy
# admission plugin"): 31 of 32 files it failed to flag lived under
# pkg/security/podsecuritypolicy/, and the path said so plainly while nothing in
# their content named a shared identifier.
#
# These functions are deliberately SEPARATE from find_field_consumers rather
# than folded into it: the symbol path is exercised by the live webhook and its
# behaviour must stay bit-identical. match_type values are prefixed `package_`
# so the two signals remain distinguishable in any output that mixes them.

_IMPORT_LINE = re.compile(
    r"^\s*(?:import|from|use|require|include|using|#include|export\s+\*\s+from)\b"
    r"|require\s*\(|from\s+['\"]"
    # Go and similar put each path on its own line inside an import block, with
    # no keyword: `\t"k8s.io/kubernetes/pkg/security/podsecuritypolicy"` -- and
    # optionally an alias before it. Without this, real imports were demoted to
    # package_path_ref (0.80) instead of package_import (0.95).
    r"|^\s*(?:_\s+|[A-Za-z_][\w.]*\s+)?[\"'][^\"']+[\"'],?\s*$",
)


def _norm_path(s: str) -> str:
    """Strip separators and case so path forms compare equal.

    pkg/security/podsecuritypolicy  ==  pkg.security.podsecuritypolicy
                                    ==  PkgSecurityPodSecurityPolicy
    """
    return re.sub(r"[^a-z0-9]", "", s.lower())


def find_package_consumers(
    file_content: str,
    file_path: str,
    package_path: str,
    language: str,
    min_confidence: float = 0.5,
) -> list[SmartMatch]:
    """Find a file's relationship to a DELETED PACKAGE.

    Three signals, strongest first:

      package_member    the file lives inside the deleted package. It does not
                        "reference" anything -- it IS part of what was removed,
                        which is why a symbol search cannot see it.
      package_import    a line imports the package path.
      package_path_ref  the package path or its tail segment appears in content
                        that is not an import (YAML manifests, build files,
                        scripts referencing a directory).

    Returns [] when none fire, so a caller can distinguish "not a consumer" from
    "consumer by path".
    """
    pkg = package_path.rstrip("/")
    if not pkg:
        return []

    matches: list[SmartMatch] = []

    # 1. Membership -- a whole-file relationship, so line_number 0.
    if file_path.startswith(pkg + "/") or file_path == pkg:
        matches.append(SmartMatch(
            file_path=file_path,
            line_number=0,
            line_content=f"(file is inside deleted package {pkg}/)",
            match_type="package_member",
            confidence=0.99,
            variant_matched=pkg,
        ))
        # Membership is decisive; no need to also scan for imports of itself.
        return matches

    needle = _norm_path(pkg)
    tail = _norm_path(pkg.rsplit("/", 1)[-1])
    # A one- or two-segment tail is too generic to carry a path reference on its
    # own -- 'utils', 'api', 'core' would match half a repo.
    tail_usable = len(tail) >= 8

    for i, line in enumerate(file_content.split("\n"), 1):
        if _is_comment(line.strip(), language):
            continue
        norm = _norm_path(line)
        if needle and needle in norm:
            is_import = bool(_IMPORT_LINE.search(line))
            matches.append(SmartMatch(
                file_path=file_path,
                line_number=i,
                line_content=line.strip(),
                match_type="package_import" if is_import else "package_path_ref",
                confidence=0.95 if is_import else 0.80,
                variant_matched=pkg,
            ))
        elif tail_usable and tail in norm:
            matches.append(SmartMatch(
                file_path=file_path,
                line_number=i,
                line_content=line.strip(),
                match_type="package_path_ref",
                confidence=0.70,
                variant_matched=pkg.rsplit("/", 1)[-1],
            ))

    matches = [m for m in matches if m.confidence >= min_confidence]
    matches.sort(key=lambda m: m.confidence, reverse=True)
    return matches


def find_consumers(
    file_content: str,
    file_path: str,
    target: str,
    language: str,
    vector: str = "symbol",
    min_confidence: float = 0.5,
) -> list[SmartMatch]:
    """Dispatch on propagation vector.

    Querying the wrong vector under-reports badly: on kubernetes#109798 a symbol
    query scored 38.5% while the correct package query scores 90.9% on the same
    PR, with no change to the matchers themselves.
    """
    if vector == "package":
        return find_package_consumers(
            file_content, file_path, target, language, min_confidence
        )
    return find_field_consumers(
        file_content, file_path, target, language, min_confidence
    )


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


def find_residual_references(
    code: str, field_name: str, language: str
) -> list:
    """Find references to `field_name` that SURVIVED a fix.

    Removing a field's declaration and its pass-through plumbing is
    mechanical and safe to automate. Deciding what a *usage* should become
    is not. After `phone_number` is deleted from the contract:

        results["sms"] = send_sms(
            to=user.phone_number,     <-- still here; now invalid
            message=msg,
        )

    Dropping that argument leaves `send_sms()` missing a required
    parameter; dropping the whole call silently disables the customer's
    SMS. Both are product decisions, so Ripple surfaces them for a human
    rather than guessing -- and must not present the fix as complete while
    references remain.

    Comment-only mentions are ignored: they do not break at runtime.

    Returns a list of (line_number, line_text, variant_matched).
    """
    variants = sorted(set(generate_variants(field_name)), key=len, reverse=True)

    residual = []
    for idx, line in enumerate(code.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or _is_comment(stripped, language):
            continue
        for variant in variants:
            if variant in line:
                residual.append((idx, stripped, variant))
                break
    return residual
