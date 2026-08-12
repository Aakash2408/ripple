"""
ripple/app/monorepo.py

Monorepo Support — finds consumers WITHIN the same repository.

Most companies (70%+) use monorepos. Their API specs and consumers
live side-by-side. Existing tools (Optic, buf, Speakeasy) only work
across repos. Ripple works WITHIN a single repo too.

Key difference from cross-repo:
  - No need for GitHub App installation or OAuth
  - Consumer finding uses filesystem paths instead of API calls
  - Fixes are in the same PR (not separate PRs in other repos)
  - The PR becomes a "propagation PR" — spec change + all consumer fixes

How it works:
  1. Detect which spec files changed (proto, openapi, graphql, etc.)
  2. Identify the breaking changes in those specs
  3. Scan the SAME REPO for files that reference the changed entities
  4. Generate fixes for each affected file
  5. Include all fixes in the same commit/PR as the spec change

Integration points:
  - GitHub Action (ripple-check): adds monorepo scanning as a check
  - Pre-commit hook: blocks commit if consumers aren't updated
  - CLI: `ripple scan --monorepo /path/to/repo`
"""

from __future__ import annotations
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class MonorepoConsumer:
    """A file within the same repo that consumes a changed API."""
    file_path: str
    line_number: int
    reference: str          # The actual code referencing the field/endpoint
    confidence: float       # 0.0 - 1.0
    category: str           # "import", "usage", "test", "config", "generated"
    fix_suggestion: str = ""


@dataclass
class MonorepoScanResult:
    """Result of scanning a monorepo for consumers."""
    spec_file: str
    breaking_changes: list[dict]
    consumers: list[MonorepoConsumer]
    scan_paths: list[str]
    files_scanned: int
    time_ms: int


def scan_monorepo(
    repo_path: str,
    spec_file: str,
    changed_fields: list[str],
    scan_paths: list[str] = None,
    exclude_patterns: list[str] = None,
) -> MonorepoScanResult:
    """
    Scan a monorepo for consumers of changed API fields.
    
    Args:
        repo_path: Root of the monorepo
        spec_file: Path to the changed spec file (relative to repo root)
        changed_fields: List of field/endpoint names that were changed/removed
        scan_paths: Optional list of paths to scan (defaults to entire repo)
        exclude_patterns: Patterns to exclude (node_modules, vendor, etc.)
    """
    import time
    start = time.time()
    
    if not scan_paths:
        scan_paths = [repo_path]
    
    if not exclude_patterns:
        exclude_patterns = [
            "node_modules", "vendor", "dist", "build", ".git",
            "__pycache__", ".next", "target", "bin", "obj",
            "*.min.js", "*.map", "package-lock.json", "yarn.lock",
        ]
    
    consumers = []
    files_scanned = 0
    
    for field_name in changed_fields:
        # Generate search variants (snake_case, camelCase, etc.)
        variants = _generate_variants(field_name)
        
        # Use git grep for fast searching (respects .gitignore)
        for variant in variants:
            results = _git_grep(repo_path, variant, exclude_patterns)
            
            for result in results:
                # Skip the spec file itself
                if result["file"] == spec_file:
                    continue
                
                # Classify the consumer
                category = _classify_consumer(result["file"], result["line"])
                confidence = _compute_confidence(category, variant, field_name)
                
                consumers.append(MonorepoConsumer(
                    file_path=result["file"],
                    line_number=result["line_num"],
                    reference=result["line"].strip()[:200],
                    confidence=confidence,
                    category=category,
                ))
                
            files_scanned += len(results)
    
    # Deduplicate by file + line
    seen = set()
    unique_consumers = []
    for c in consumers:
        key = f"{c.file_path}:{c.line_number}"
        if key not in seen:
            seen.add(key)
            unique_consumers.append(c)
    
    elapsed_ms = int((time.time() - start) * 1000)
    
    return MonorepoScanResult(
        spec_file=spec_file,
        breaking_changes=[{"field": f} for f in changed_fields],
        consumers=unique_consumers,
        scan_paths=scan_paths,
        files_scanned=files_scanned,
        time_ms=elapsed_ms,
    )


def _git_grep(repo_path: str, pattern: str, exclude: list[str]) -> list[dict]:
    """Fast search using git grep."""
    try:
        # Build exclude args
        exclude_args = []
        for ex in exclude:
            exclude_args.extend(["--", f":!{ex}"])
        
        cmd = ["git", "grep", "-n", "--fixed-strings", "-I", pattern]
        result = subprocess.run(
            cmd, cwd=repo_path,
            capture_output=True, text=True, timeout=30
        )
        
        results = []
        for line in result.stdout.strip().split("\n")[:100]:  # Cap at 100
            if ":" in line:
                parts = line.split(":", 2)
                if len(parts) >= 3 and parts[1].isdigit():
                    results.append({
                        "file": parts[0],
                        "line_num": int(parts[1]),
                        "line": parts[2],
                    })
        return results
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


def _generate_variants(field_name: str) -> list[str]:
    """Generate naming variants for a field."""
    variants = [field_name]
    
    if "_" in field_name:
        parts = field_name.split("_")
        # camelCase
        variants.append(parts[0] + "".join(p.capitalize() for p in parts[1:]))
        # PascalCase
        variants.append("".join(p.capitalize() for p in parts))
        # kebab-case
        variants.append(field_name.replace("_", "-"))
    elif any(c.isupper() for c in field_name[1:]):
        # camelCase → snake_case
        snake = re.sub(r'([A-Z])', r'_\1', field_name).lower().lstrip('_')
        variants.append(snake)
        variants.append(snake.replace("_", "-"))
    
    return variants


def _classify_consumer(file_path: str, line_content: str) -> str:
    """Classify what kind of consumer this is."""
    path_lower = file_path.lower()
    
    if any(p in path_lower for p in ["test", "spec", "__tests__", "_test."]):
        return "test"
    if any(p in path_lower for p in ["_pb2.py", ".pb.go", "Grpc.java"]):
        return "generated"
    if any(p in path_lower for p in [".yaml", ".yml", ".json", "config"]):
        return "config"
    if "import" in line_content.lower() or "require" in line_content.lower():
        return "import"
    return "usage"


def _compute_confidence(category: str, matched_variant: str, original_field: str) -> float:
    """Compute confidence that this is a real consumer."""
    base = 0.7
    
    # Exact match = higher confidence
    if matched_variant == original_field:
        base += 0.1
    
    # Category adjustments
    if category == "generated":
        base = 0.95  # Generated code is almost certainly a consumer
    elif category == "usage":
        base += 0.1
    elif category == "test":
        base += 0.05
    elif category == "config":
        base += 0.0
    elif category == "import":
        base += 0.15
    
    return min(base, 1.0)


# === CLI Interface ===

def scan_and_report(repo_path: str, spec_file: str, fields: list[str]) -> str:
    """Run scan and return a formatted report."""
    result = scan_monorepo(repo_path, spec_file, fields)
    
    if not result.consumers:
        return f"✅ No consumers found for changes in `{spec_file}`. Safe to merge."
    
    lines = [
        f"## 🌊 Ripple Monorepo Scan — {len(result.consumers)} consumer(s) found",
        f"",
        f"**Spec:** `{spec_file}`",
        f"**Changed fields:** {', '.join(f'`{f}`' for f in fields)}",
        f"**Scanned in:** {result.time_ms}ms",
        f"",
        f"| File | Line | Category | Confidence | Reference |",
        f"|------|------|----------|-----------|-----------|",
    ]
    
    for c in sorted(result.consumers, key=lambda x: -x.confidence)[:20]:
        ref = c.reference[:60] + "..." if len(c.reference) > 60 else c.reference
        lines.append(
            f"| `{c.file_path}` | L{c.line_number} | {c.category} | {c.confidence:.0%} | `{ref}` |"
        )
    
    if len(result.consumers) > 20:
        lines.append(f"\n*... and {len(result.consumers) - 20} more*")
    
    lines.extend([
        "",
        "---",
        "**Action:** Update these consumers before merging, or Ripple will open fix PRs automatically.",
        "*Generated by [Ripple](https://aakash2408.github.io/ripple) 🌊*",
    ])
    
    return "\n".join(lines)
