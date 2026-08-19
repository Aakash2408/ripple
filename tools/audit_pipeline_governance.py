#!/usr/bin/env python3
"""Which entry points are GOVERNED by the registry and the outcome funnel.

WHY THIS EXISTS
Stage 6 wired app/routing.py into the pipeline and Stage 3 wired the outcome
funnel, and both were verified by importing them and by tests. Neither check asked
the only question that matters: *how many ways are there into a PR?*

Five. Stage 7 found that `pr_level` is reachable from exactly one of them.

    github_webhook       -> _create_fix_pr        GOVERNED
    gitlab_webhook       -> create_fix_mr         154 lines inline, bypasses all
    bitbucket_webhook    -> bb_create_fix_pr      154 lines inline, bypasses all
    app/cli.py           -> pr_engine.create_prs  own PR body, no routing
    agent/core.py        -> adapter.create_fix_review  separate package

So the two headline claims of this plan -- "no breaking change can produce
silence" and "the registry governs routing" -- are true for GitHub and false for
everything else. On GitLab and Bitbucket a breaking change can still produce
silence, because `_log_fix_generated` is never reached.

This is the duplicated-implementation defect at the largest scale in the codebase,
and it hid from three separate audits because it is INLINE IN ROUTE HANDLERS and in
a second package. Module-level call graphs and filename pairs -- the two things
earlier stages used to size P0.1 -- cannot see it. That assessment ("~1 day: delete
dead code, align two CLI call sites") was wrong, in the opposite direction from the
usual: under-scoped, not over-scoped.

WHAT THIS GATE DOES
It does not demand that everything be governed today -- that is a real refactor of
~310 inline lines plus a separate agent package, and doing it blind, without
per-platform fixtures, would trade a known gap for an unknown one. It demands that
the set of ungoverned entry points NEVER GROWS: each is named here with a reason,
an unlisted ungoverned entry point fails the build, and removing an entry from
EXEMPT is the unit of progress.

Usage:
    python3.12 tools/audit_pipeline_governance.py
"""
from __future__ import annotations

import ast
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# The functions that actually open a pull request / merge request / review.
PR_CREATORS = {
    "_create_fix_pr":     "GitHub PR (webhook)",
    "create_fix_mr":      "GitLab MR",
    "bb_create_fix_pr":   "Bitbucket PR",
    "create_pr":          "pr_engine (CLI)",
    "create_prs":         "pr_engine (CLI, batch)",
    "create_fix_review":  "self-hosted agent adapters",
}

# What it means to be governed.
REQUIRED = {
    "pr_level":           "the registry decides the safety level (Stage 6)",
    "_log_fix_generated": "every attempt ends in a stated outcome (Stage 3)",
}

# Entry points known to be ungoverned, each with the reason. An ungoverned entry
# point NOT listed here fails the build. Deleting a line here is progress.
# Entry points that are SWITCHED OFF, and must stay off. Distinct from EXEMPT: an
# exemption TOLERATES an ungoverned path, whereas this asserts the path cannot be
# reached at all.
#
# The check is structural, not a label: the route must call experimental_disabled()
# BEFORE any PR-creating call. A guard placed after the pipeline would read as
# "disabled" while still opening merge requests.
#
# EMPTY, AND THAT IS PROGRESS. gitlab_webhook and bitbucket_webhook lived here from
# Stage 2 -- each inlined ~154 lines that bypassed the routing decision and the
# outcome funnel, so switching them off was the only honest option. Both now call
# _govern_consumer_fix (one pr_level call site serves all three platforms) and open
# a ChangeRun, so they are GOVERNED rather than merely unreachable, which is the
# stronger claim.
#
# Being governed is NOT being enabled: app/experimental.py still gates both routes
# behind RIPPLE_ENABLE_EXPERIMENTAL_PLATFORMS, so they remain off by default. What
# changed is that turning them on became a deployment decision instead of a safety
# risk. If a future edit re-inlines a pipeline or drops the decision, this audit
# fails -- rather than the platform quietly returning to ungoverned with the env var
# already set in production.
DISABLED: dict = {}

EXEMPT = {
    "app/cli.py:main":
        "app/cli.py calls pr_engine.create_prs, which has its OWN _format_pr_body "
        "and no routing decision. The CLI states no safety level. Left live "
        "because it is developer-invoked and opens nothing without an explicit "
        "command.",
    "agent/core.py:main":
        "drives the self-hosted adapters (Phabricator, Gerrit, CRUX, "
        "generic git). Separate package; imports nothing from app.routing. "
        "Self-hosted, so it cannot be switched off centrally.",
}


def _call_graph() -> dict:
    """'module.py:function' -> callee BARE names, across app/ and agent/.

    Two-level on purpose. Keys are module-qualified so distinct entry points stay
    distinct -- the first version keyed on the bare name and merged app/cli.py:main
    with the agent's main(), reporting one entry point where there are two and
    producing a misleading exemption. Callees stay bare because resolving imports
    properly is a bigger job than this gate needs, and a bare name still CROSSES
    MODULE BOUNDARIES -- which a per-file walk does not. A per-file walk reported
    should_create_pr as unreachable from github_webhook because pr_level imports it
    lazily inside app/routing.py.
    """
    graph: dict = {}
    files = (sorted(glob.glob(os.path.join(ROOT, "app", "*.py")))
             + sorted(glob.glob(os.path.join(ROOT, "agent", "*.py"))))
    for path in files:
        rel = os.path.relpath(path, ROOT)
        try:
            tree = ast.parse(open(path).read())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            callees = graph.setdefault(f"{rel}:{node.name}", set())
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    f = sub.func
                    if isinstance(f, ast.Name):
                        callees.add(f.id)
                    elif isinstance(f, ast.Attribute):
                        callees.add(f.attr)
    return graph


def _keys_named(graph: dict, name: str) -> list:
    return [k for k in graph if k.rsplit(":", 1)[1] == name]


def _reachable(graph: dict, start: str) -> set:
    """Bare callee names transitively reachable from a module-qualified key."""
    seen, frontier, out = set(), [start], set()
    while frontier:
        key = frontier.pop()
        if key in seen:
            continue
        seen.add(key)
        for callee in graph.get(key, set()):
            out.add(callee)
            frontier.extend(_keys_named(graph, callee))
    return out


def _entry_points(graph: dict) -> list:
    """Module-qualified functions that reach a PR creator and are not themselves
    called by another such function: the outermost callers."""
    candidates = {k for k in graph
                  if _reachable(graph, k) & set(PR_CREATORS)}
    inner = set()
    for k in candidates:
        for callee in graph.get(k, set()):
            inner |= {c for c in _keys_named(graph, callee) if c in candidates}
    return sorted(candidates - inner
                  - {k for k in candidates
                     if k.rsplit(":", 1)[1] in PR_CREATORS})


# Terminal exits inside the governed pipeline that are allowed to emit no signal,
# by the line's controlling intent. Anything else is a decision nobody records.
#
# Stage 7 found two that were NOT legitimate, both in the consumer loop:
#   `if config.should_ignore(f): continue`   a consumer dropped by the CUSTOMER'S
#       own .ripple.yaml, with no record -- so an over-broad glob was
#       indistinguishable from "no consumers found", and parse_ripple_config
#       already falls back to defaults on a malformed file without saying so.
#   `if len(prs_created) >= cap: break`      the PR cap abandoned every remaining
#       consumer silently, so a partially-propagated change looked complete.
PIPELINE_FN = "_process_spec_change_inner"
SILENT_EXIT_OK = {
    "ensemble_prediction_match":
        "breaking out of the prediction SEARCH once the matching prediction is "
        "found. A loop-control break, not a terminal outcome for the change.",
    "governed_decision_already_signalled":
        "`if not decision.opens_pr: continue` immediately after "
        "_govern_consumer_fix(), which ALREADY called run.refused() and logged "
        "pr_skipped one frame down -- see the assertion in "
        "test_the_governed_decision_is_platform_neutral_and_denies_auto_without_a_tree, "
        "which fails if the refusal stops being recorded. Emitting a second signal "
        "here would double-count every refusal, the same double-count that made "
        "_generate_fix_with_rag_fallback stop logging fix_generated. This allowance "
        "exists because the decision was MOVED OUT of the pipeline so GitLab and "
        "Bitbucket could share it; the signal did not disappear, it relocated.",
}


def _unsignalled_exits(path: str) -> list:
    """return/continue/break in PIPELINE_FN with no diagnostic before it."""
    src = open(path).read()
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        # A crash still fails the build, but a traceback is not a finding. Say what
        # is wrong -- the same reason verify_durability.py reports UNREACHABLE
        # rather than raising.
        return [f"{os.path.basename(path)} does not parse ({exc}), so terminal "
                f"exits cannot be checked"]
    fns = {n.name: n for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if PIPELINE_FN not in fns:
        return [f"{PIPELINE_FN} is gone -- this check no longer checks anything"]

    markers = ("_log_activity", "record", "_log_fix_generated", "logger", "print")

    def signalled(block, idx):
        for stmt in block[:idx + 1]:
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Call):
                    f = sub.func
                    nm = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
                    if any(m in nm for m in markers):
                        return True
                if isinstance(sub, ast.Raise):
                    return True
        return False

    lines = src.split("\n")
    found = []
    for node in ast.walk(fns[PIPELINE_FN]):
        for attr in ("body", "orelse", "finalbody"):
            block = getattr(node, attr, None)
            if not isinstance(block, list):
                continue
            for i, stmt in enumerate(block):
                if not isinstance(stmt, (ast.Return, ast.Continue, ast.Break)):
                    continue
                if signalled(block, i):
                    continue
                # The prediction-search break is identified by the assignment
                # immediately above it, not by its line number.
                context = " ".join(l.strip() for l in lines[max(0, stmt.lineno - 4):stmt.lineno])
                if "pred.get(" in context or "pred[" in context:
                    continue
                # SILENT_EXIT_OK["governed_decision_already_signalled"]. The guard
                # is the identifier, not the line number, so this survives edits
                # above it -- and it matches ONLY this shape, so a different bare
                # continue in the same loop is still a finding.
                if "decision.opens_pr" in context:
                    continue
                found.append(
                    f"{PIPELINE_FN} line {stmt.lineno}: "
                    f"{type(stmt).__name__} with no signal -- "
                    f"{lines[stmt.lineno - 1].strip()}")
    return found


def _guard_position(entry: str) -> tuple:
    """(guard_line, first_pr_call_line) for a route, or (None, None) if absent.

    Positions, not just presence: a guard added AFTER the pipeline would read as
    "disabled" while still opening merge requests. Line numbers are compared inside
    a single parse, so there is no persistence and nothing to drift -- unlike the
    line-keyed triage that detached the moment webhook.py grew.
    """
    module, func = entry.rsplit(":", 1)
    path = os.path.join(ROOT, module)
    if not os.path.exists(path):
        return None, None
    tree = ast.parse(open(path).read())
    node = next((n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == func), None)
    if node is None:
        return None, None
    guard = pr_call = None
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        name = (sub.func.id if isinstance(sub.func, ast.Name)
                else getattr(sub.func, "attr", ""))
        if name == "experimental_disabled" and guard is None:
            guard = sub.lineno
        if name in PR_CREATORS and (pr_call is None or sub.lineno < pr_call):
            pr_call = sub.lineno
    return guard, pr_call


def _check_disabled() -> list:
    problems = []
    for entry, why in sorted(DISABLED.items()):
        guard, pr_call = _guard_position(entry)
        if guard is None:
            problems.append(
                f"{entry} is listed DISABLED but has no experimental_disabled() "
                f"guard. It is switched off because: {why}")
            print(f"  FAIL   {entry:<40} no guard")
        elif pr_call is not None and guard > pr_call:
            problems.append(
                f"{entry} guards at line {guard} but can open a PR at line "
                f"{pr_call} -- the guard runs too late to stop anything.")
            print(f"  FAIL   {entry:<40} guard after the PR call")
        else:
            print(f"  off    {entry:<40} guarded at line {guard}")
    return problems


def _terminal_state_wrapping(path: str) -> list:
    """The per-breaking-change loop must be wrapped in ChangeRun.

    Structural, because "exactly one terminal state per breaking change" is only
    guaranteed by the context manager. If the loop body is ever unwrapped, or a
    `return` is added outside the `with`, the guarantee silently disappears and the
    Autonomous Resolution Rate loses its denominator without anything erroring.
    """
    problems = []
    try:
        tree = ast.parse(open(path).read())
    except SyntaxError as exc:
        return [f"{os.path.basename(path)} does not parse ({exc})"]

    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == PIPELINE_FN), None)
    if fn is None:
        return [f"{PIPELINE_FN} is gone -- this check no longer checks anything"]

    loops = [n for n in ast.walk(fn)
             if isinstance(n, ast.For) and ast.unparse(n.iter) == "breaking_changes"]
    if not loops:
        return ["no `for change in breaking_changes` loop found -- if it was "
                "renamed, update this check rather than deleting it"]

    for loop in loops:
        first = loop.body[0]
        wrapped = (isinstance(first, ast.With)
                   and any("ChangeRun" in ast.unparse(i.context_expr)
                           for i in first.items))
        if not wrapped:
            problems.append(
                f"the breaking-change loop at line {loop.lineno} is NOT wrapped in "
                f"ChangeRun, so a change can be processed without emitting a "
                f"terminal state")
            continue
        # Every statement of the loop body must be inside the with -- a statement
        # after it runs without the guarantee.
        if len(loop.body) != 1:
            extra = [type(s).__name__ for s in loop.body[1:]]
            problems.append(
                f"the breaking-change loop has {len(loop.body) - 1} statement(s) "
                f"OUTSIDE the ChangeRun block ({', '.join(extra)}) -- those run "
                f"without a terminal state")
    return problems


def main(argv: list) -> int:
    graph = _call_graph()
    entries = _entry_points(graph)

    print("=" * 74)
    print("PIPELINE GOVERNANCE")
    print("=" * 74)
    print("\n  entry points that can open a PR, and whether the registry and the")
    print("  outcome funnel are on their path:\n")

    governed, ungoverned, problems = [], [], []
    for fn in entries:
        reach = _reachable(graph, fn)
        missing = [k for k in REQUIRED if k not in reach]
        creators = sorted(reach & set(PR_CREATORS))
        via = ", ".join(PR_CREATORS[c] for c in creators) or "?"
        if missing:
            ungoverned.append(fn)
            if fn in DISABLED:
                continue        # reported separately by _check_disabled()
            mark = "EXEMPT " if fn in EXEMPT else "FAIL   "
            print(f"  {mark}{fn:22} via {via}")
            for k in missing:
                print(f"             missing {k} -- {REQUIRED[k]}")
            if fn not in EXEMPT:
                problems.append(
                    f"{fn} opens PRs ({via}) without {', '.join(missing)}. Route it "
                    f"through app/routing.py, or add it to EXEMPT with a reason.")
        else:
            governed.append(fn)
            print(f"  OK     {fn:22} via {via}")

    # An exemption for something that is no longer an ungoverned entry point is
    # stale, and a stale exemption is how a gate quietly stops gating.
    for fn in sorted(EXEMPT):
        if fn not in ungoverned:
            problems.append(
                f"{fn} is listed in EXEMPT but is no longer an ungoverned entry "
                f"point. Remove the exemption -- it is now claiming a gap that "
                f"does not exist.")

    print("\n  entry points SWITCHED OFF (must stay off):\n")
    problems.extend(_check_disabled())

    print(f"\n  {len(governed)} governed, {len(DISABLED)} disabled, "
          f"{len(ungoverned) - len(DISABLED)} exempt, {len(entries)} total")

    # Second half: within the governed pipeline, no decision may exit unrecorded.
    wrapping = _terminal_state_wrapping(os.path.join(ROOT, "app", "webhook.py"))
    print(f"\n  per-breaking-change terminal state: "
          f"{'wrapped in ChangeRun' if not wrapping else 'NOT GUARANTEED'}")
    for w in wrapping:
        print(f"      FAIL  {w}")
    problems.extend(wrapping)

    unsignalled = _unsignalled_exits(os.path.join(ROOT, "app", "webhook.py"))
    print(f"\n  unsignalled terminal exits in {PIPELINE_FN}: {len(unsignalled)}")
    for u in unsignalled:
        print(f"      FAIL  {u}")
    problems.extend(unsignalled)

    if problems:
        print(f"\n  {len(problems)} PROBLEM(S):")
        for p in problems:
            print(f"      {p}")
        return 1

    print("\n  no UNLISTED ungoverned entry point. The exempt set is the honest")
    print("  scope of 'the registry governs routing': it holds for all three")
    print("  hosted platforms, and NOT for the CLI or the self-hosted agent.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
