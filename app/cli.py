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
    execute_follow_ons = "--execute-follow-ons" in sys.argv
    
    # Step 1: Detect breaking changes
    print()
    print("━" * 60)
    print("  RIPPLE — API Change Propagation")
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
    prs = []
    fork_run = None
    if not all_fixes:
        print("  ⚠️  No fixes generated.")
    elif execute_follow_ons:
        # THE FORK PATH. `--execute-follow-ons` was parsed here and then never read,
        # so the flag silently did nothing and every run fell through to create_prs,
        # which opens a branch with POST /repos/{repo}/git/refs and therefore 403s on
        # any repository the token cannot push to -- i.e. every open-source target.
        print(f"  📤 FORK → PULL REQUEST{'  (dry-run)' if dry_run else ''}...")
        print("━" * 60)
        print()
        fork_run = _run_fork_follow_ons(all_fixes, result.breaking_changes[0],
                                        dry_run=dry_run)
    else:
        # Step 4: Create PRs -- same-repo path, unchanged. Correct where the token
        # genuinely has push access, which is the case Ripple started from.
        print(f"  📤 CREATING PULL REQUESTS{'  (dry-run)' if dry_run else ''}...")
        print("━" * 60)
        print()

        prs = create_prs(all_fixes, result.breaking_changes[0], dry_run=dry_run)
        print(format_prs(prs))

    print("━" * 60)
    print(f"  RIPPLE COMPLETE")
    print(f"     Breaking changes:  {len(result.breaking_changes)}")
    print(f"     Consumers found:   {sum(1 for _ in all_fixes)}")
    print(f"     Fixes generated:   {len(all_fixes)}")
    if fork_run is not None:
        print(f"     Fork PRs:          {fork_run.summary()}")
    else:
        print(f"     PRs created:       {len(prs)}")
    print("━" * 60)


def _run_fork_follow_ons(fixes, breaking_change, *, dry_run):
    """Open a fork-based pull request per upstream, from CLI-generated fixes.

    Lives in the CLI rather than in fork_pr because it is the only place that knows
    about GeneratedFix: fork_pr deliberately takes plain (path, content) tuples so it
    carries no dependency on the app's types, and the webhook's follow-on path feeds
    it from run data instead. Two callers, two shapes, one transport.

    EVERY SKIP IS STATED. A consumer whose repository cannot be resolved, or whose
    path cannot be made repo-relative, is reported with the reason -- silently
    dropping it would let a run claim success while proposing nothing for that file,
    which is the failure shape tools/audit_fail_silent.py exists to prevent.
    """
    import base64
    import os

    from .change_types import fix_title
    from .fork_pr import (error_returning_api, open_fork_prs, repo_relative_path,
                          upstream_from_git_remote)
    from .pr_engine import _github_request

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token and not dry_run:
        print("  ⚠️  GITHUB_TOKEN not set. Nothing was written.")
        print("      Re-run with --dry-run to see exactly what would be proposed.")
        from .fork_pr import ForkPRRun
        run = ForkPRRun()
        run.refused.append(("(all)", "no GITHUB_TOKEN, so nothing was attempted"))
        return run

    groups: dict = {}
    skipped = []
    for fix in fixes:
        path = fix.consumer.file_path
        upstream, why = upstream_from_git_remote(
            os.path.dirname(path) or ".")
        if not upstream:
            skipped.append((path, why))
            continue
        rel, why = repo_relative_path(path)
        if not rel:
            skipped.append((path, why))
            continue
        content = base64.b64encode(fix.fixed_code.encode()).decode()
        groups.setdefault(upstream, []).append((rel, content))

    for path, why in skipped:
        print(f"  ⏭️  {path}: {why}")

    if not groups:
        print("  ⚠️  No consumer resolved to a GitHub repository, so no pull request "
              "was proposed.")
        from .fork_pr import ForkPRRun
        run = ForkPRRun()
        for path, why in skipped:
            run.refused.append((path, why))
        return run

    branch = f"ripple/{breaking_change.change_type}-{breaking_change.field_name}"
    title = f"fix: {fix_title(breaking_change)}"
    body = _fork_pr_body(breaking_change)

    run = open_fork_prs(groups, branch=branch, title=title, body=body,
                        token=token, api=error_returning_api(_github_request),
                        dry_run=dry_run)

    for upstream, number, url in run.opened:
        print(f"  ✅ {upstream}#{number}  {url}")
    for upstream, br in run.already_open:
        print(f"  ↩️  {upstream}: a pull request for `{br}` is already open")
    for upstream, reason in run.refused:
        print(f"  ⏭️  {upstream}: {reason}")
    for upstream, refusals in run.failed:
        print(f"  ❌ {upstream}:")
        for r in refusals[:3]:
            print(f"       {r}")
    print()
    return run


def _fork_pr_body(breaking_change) -> str:
    """The pull-request body for a fork-based follow-on.

    Says what was changed and what was NOT established. A drive-by pull request on a
    repository the author does not own is read by a maintainer with no context, so
    overclaiming -- "verified", "all references handled" -- is what gets an automated
    contributor blocked.

    THE VALIDATION LINE IS NOT BOILERPLATE. Ripple's Python validator is mypy, and it
    refuses to run without the project's own pinned dev requirements, because an
    unpinned typecheck is not reproducible evidence. On the first real target it
    declined for exactly that reason. Saying "parses as valid Python, not typechecked,
    because <reason>" is a claim a maintainer can check; saying nothing invites them to
    assume more was verified than was.
    """
    from .change_types import describe
    field = breaking_change.field_name
    return (
        f"`{field}` was removed upstream, and this repository still references it.\n\n"
        f"**Change:** {describe(breaking_change.change_type)}\n"
        f"**What this pull request does:** replaces the references to `{field}` in "
        f"the file(s) below.\n\n"
        f"**How it was produced:** a deterministic source transform -- no language "
        f"model was involved in generating this diff.\n\n"
        f"**What has NOT been established:**\n"
        f"- The change has not been run against this project's test suite.\n"
        f"- No claim is made that every reference in the repository is covered -- "
        f"only the ones listed.\n"
        f"- Type checking was not performed. Ripple runs mypy against a project's own "
        f"pinned dev requirements and declines when they are absent, rather than "
        f"reporting an unpinned run as evidence.\n\n"
        f"Opened by [Ripple](https://github.com/Aakash2408/ripple). If this is "
        f"unwanted, closing it is the right response and no follow-up will be sent.\n"
    )


def _print_usage():
    print("""
Ripple — Self-Maintaining APIs

Usage:
  ripple diff <old-spec> <new-spec>              Detect breaking changes
  ripple scan <old-spec> <new-spec> --repos ...  Find affected consumers
  ripple run  <old-spec> <new-spec> --repos ...  Full pipeline (diff → find → fix → PR)

Flags for `run`:
  --dry-run              Propose nothing. Prints exactly what would be written.
  --no-llm               Deterministic codemods only, no model.
  --execute-follow-ons   Open pull requests by FORK instead of by direct push.
                         Required for any repository you cannot push to; the
                         default path creates a branch in the target repo and so
                         needs write access. The upstream repository and the
                         repo-relative path are read from each consumer's own git
                         clone, so a directory that is not a clone is skipped with
                         a reason rather than guessed at.
""")


if __name__ == "__main__":
    main()
