#!/usr/bin/env python3
"""
ripple/app/cli.py

Ripple CLI — detect breaking API changes, find consumers, generate fixes.

Usage:
    python -m app.cli diff old.yaml new.yaml
    python -m app.cli scan old.yaml new.yaml --repos ./frontend ./mobile ./analytics
    python -m app.cli run old.yaml new.yaml --repos ./consumer1 ./consumer2
"""

import sys
from pathlib import Path


def main():
    if len(sys.argv) < 2:
        _print_usage()
        sys.exit(1)
    
    command = sys.argv[1]
    
    if command == "diff":
        cmd_diff()
    elif command == "scan":
        cmd_scan()
    elif command == "run":
        cmd_run()
    else:
        print(f"Unknown command: {command}")
        _print_usage()
        sys.exit(1)


def cmd_diff():
    """Just detect breaking changes."""
    if len(sys.argv) < 4:
        print("Usage: ripple diff <old-spec> <new-spec>")
        sys.exit(1)
    
    from .diff_engine import diff_specs
    
    result = diff_specs(sys.argv[2], sys.argv[3])
    print(result.format())
    
    if result.has_breaking_changes:
        sys.exit(1)


def cmd_scan():
    """Detect changes AND find consumers."""
    if len(sys.argv) < 4:
        print("Usage: ripple scan <old-spec> <new-spec> --repos <dir1> <dir2> ...")
        sys.exit(1)
    
    from .diff_engine import diff_specs
    from .consumer_finder import find_consumers, format_consumers
    
    old_path = sys.argv[2]
    new_path = sys.argv[3]
    
    # Parse --repos argument
    repos = []
    if "--repos" in sys.argv:
        repos_idx = sys.argv.index("--repos") + 1
        repos = sys.argv[repos_idx:]
    else:
        print("⚠️  No --repos specified. Use --repos <dir1> <dir2> ...")
        sys.exit(1)
    
    # Step 1: Detect breaking changes
    print("━" * 60)
    print("  RIPPLE — API Change Propagation")
    print("━" * 60)
    print()
    
    result = diff_specs(old_path, new_path)
    print(result.format())
    
    if not result.has_breaking_changes:
        print("✅ No action needed.")
        return
    
    # Step 2: Find consumers for each breaking change
    print("━" * 60)
    print("  SCANNING FOR CONSUMERS...")
    print("━" * 60)
    print()
    
    for change in result.breaking_changes:
        print(f"  Finding consumers of {change.method.upper()} {change.path}...")
        print(f"  Searching in: {', '.join(repos)}")
        print()
        
        matches = find_consumers(repos, change)
        print(format_consumers(matches))
    
    print("━" * 60)


def cmd_run():
    """Full pipeline: diff → find consumers → generate fixes."""
    if len(sys.argv) < 4:
        print("Usage: ripple run <old-spec> <new-spec> --repos <dir1> <dir2> ...")
        sys.exit(1)
    
    from .diff_engine import diff_specs
    from .consumer_finder import find_consumers, format_consumers
    from .fix_generator import generate_fixes, format_fixes
    from .pr_engine import create_prs, format_prs
    
    old_path = sys.argv[2]
    new_path = sys.argv[3]
    
    # Parse --repos argument
    repos = []
    if "--repos" in sys.argv:
        repos_idx = sys.argv.index("--repos") + 1
        repos = [a for a in sys.argv[repos_idx:] if not a.startswith("--")]
    
    # Parse flags
    use_llm = "--no-llm" not in sys.argv
    dry_run = "--dry-run" in sys.argv
    
    # Step 1: Detect breaking changes
    print()
    print("━" * 60)
    print("  🌊 RIPPLE — API Change Propagation")
    print("━" * 60)
    print()
    
    result = diff_specs(old_path, new_path)
    print(result.format())
    
    if not result.has_breaking_changes:
        print("✅ No breaking changes. All consumers are safe.")
        return
    
    # Step 2: Find consumers
    print("━" * 60)
    print("  🔍 FINDING CONSUMERS...")
    print("━" * 60)
    print()
    
    all_fixes = []
    
    for change in result.breaking_changes:
        matches = find_consumers(repos, change)
        print(format_consumers(matches))
        
        # Step 3: Generate fixes
        print("━" * 60)
        print("  🔧 GENERATING FIXES...")
        print("━" * 60)
        print()
        
        fixes = generate_fixes(matches, change, use_llm=use_llm)
        print(format_fixes(fixes))
        all_fixes.extend(fixes)
    
    # Summary
    print("━" * 60)
    if not all_fixes:
        print("  ⚠️  No fixes generated.")
    else:
        # Step 4: Create PRs
        print(f"  📤 CREATING PULL REQUESTS{'  (dry-run)' if dry_run else ''}...")
        print("━" * 60)
        print()
        
        prs = create_prs(all_fixes, result.breaking_changes[0], dry_run=dry_run)
        print(format_prs(prs))
    
    print("━" * 60)
    print(f"  🌊 RIPPLE COMPLETE")
    print(f"     Breaking changes:  {len(result.breaking_changes)}")
    print(f"     Consumers found:   {sum(1 for _ in all_fixes)}")
    print(f"     Fixes generated:   {len(all_fixes)}")
    print(f"     PRs created:       {len(prs) if all_fixes else 0}")
    print("━" * 60)


def _print_usage():
    print("""
Ripple — Self-Maintaining APIs

Usage:
  ripple diff <old-spec> <new-spec>              Detect breaking changes
  ripple scan <old-spec> <new-spec> --repos ...  Find affected consumers
  ripple run  <old-spec> <new-spec> --repos ...  Full pipeline (diff → find → fix → PR)
""")


if __name__ == "__main__":
    main()
