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
import re
from dataclasses import dataclass
from pathlib import Path

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
    """
    Find files that consume the endpoint affected by a breaking change.
    
    Simple strategy:
    1. Find files containing the endpoint path
    2. Filter to those that use the HTTP method
    3. Rank by confidence
    """
    if exclude_patterns is None:
        exclude_patterns = [
            "node_modules", ".git", "dist", "build", "__pycache__",
            "vendor", ".venv", "target", ".gradle",
        ]
    
    endpoint_path = breaking_change.path  # e.g., "/users"
    http_method = breaking_change.method  # e.g., "post"
    
    matches = []
    
    for search_dir in search_dirs:
        for root, dirs, files in os.walk(search_dir):
            # Skip excluded directories
            dirs[:] = [d for d in dirs if d not in exclude_patterns]
            
            for filename in files:
                # Only scan code files
                if not _is_code_file(filename):
                    continue
                
                filepath = os.path.join(root, filename)
                file_matches = _scan_file(filepath, endpoint_path, http_method)
                matches.extend(file_matches)
    
    # Sort by confidence (high first)
    confidence_order = {"high": 0, "medium": 1, "low": 2}
    matches.sort(key=lambda m: confidence_order.get(m.confidence, 3))
    
    return matches


def _is_code_file(filename: str) -> bool:
    """Check if a file is worth scanning."""
    code_extensions = {
        ".ts", ".tsx", ".js", ".jsx",  # TypeScript/JavaScript
        ".py",                          # Python
        ".java", ".kt",                 # JVM
        ".go",                          # Go
        ".rs",                          # Rust
        ".rb",                          # Ruby
        ".cs",                          # C#
        ".swift",                       # Swift
        ".php",                         # PHP
    }
    return Path(filename).suffix.lower() in code_extensions


def _scan_file(filepath: str, endpoint_path: str, http_method: str) -> list[ConsumerMatch]:
    """Scan a single file for references to the endpoint."""
    try:
        with open(filepath, "r", errors="ignore") as f:
            lines = f.readlines()
    except (IOError, OSError):
        return []
    
    matches = []
    language = _detect_language(filepath)
    
    # Patterns to look for
    # 1. Direct URL path reference
    path_pattern = re.compile(re.escape(endpoint_path), re.IGNORECASE)
    
    # 2. HTTP method call patterns by language
    method_patterns = _get_method_patterns(http_method, language)
    
    for line_num, line in enumerate(lines, 1):
        # Check for endpoint path
        if path_pattern.search(line):
            # Found the path — now check if it's an HTTP call
            confidence = "medium"
            reason = f"References endpoint path '{endpoint_path}'"
            
            # Check surrounding lines for method call
            context = "".join(lines[max(0, line_num-3):min(len(lines), line_num+3)])
            
            for pattern in method_patterns:
                if pattern.search(context):
                    confidence = "high"
                    reason = f"HTTP {http_method.upper()} call to '{endpoint_path}'"
                    break
            
            matches.append(ConsumerMatch(
                file_path=filepath,
                line_number=line_num,
                code_snippet=line.strip(),
                confidence=confidence,
                match_reason=reason,
                language=language,
            ))
    
    return matches


def _detect_language(filepath: str) -> str:
    """Detect programming language from file extension."""
    ext = Path(filepath).suffix.lower()
    lang_map = {
        ".ts": "typescript", ".tsx": "typescript",
        ".js": "javascript", ".jsx": "javascript",
        ".py": "python",
        ".java": "java", ".kt": "kotlin",
        ".go": "go",
        ".rs": "rust",
        ".rb": "ruby",
        ".cs": "csharp",
        ".swift": "swift",
        ".php": "php",
    }
    return lang_map.get(ext, "unknown")


def _get_method_patterns(http_method: str, language: str) -> list[re.Pattern]:
    """Get regex patterns for HTTP method calls by language."""
    method_lower = http_method.lower()
    method_upper = http_method.upper()
    
    patterns = [
        # Generic patterns (work across languages)
        re.compile(rf'\.{method_lower}\s*\(', re.IGNORECASE),       # .post(, .get(
        re.compile(rf'"{method_upper}"', re.IGNORECASE),            # "POST", "GET"
        re.compile(rf"'{method_upper}'", re.IGNORECASE),            # 'POST', 'GET'
        re.compile(rf'method\s*[:=]\s*["\']?{method_upper}', re.IGNORECASE),  # method: "POST"
        re.compile(rf'requests\.{method_lower}', re.IGNORECASE),    # requests.post (Python)
        re.compile(rf'http\.{method_lower}', re.IGNORECASE),        # http.post
        re.compile(rf'fetch\(.*{method_upper}', re.IGNORECASE),     # fetch(...POST
        re.compile(rf'\.{method_upper}\s*\(', re.IGNORECASE),       # .POST( (Java HttpRequest)
    ]
    
    return patterns


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
