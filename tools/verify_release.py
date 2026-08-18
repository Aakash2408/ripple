#!/usr/bin/env python3
"""Is the live service running the EXACT commit that passed the hardening suite?

    tested sha  (tests/.last_run.json, written by the suite itself)
        |
        v
    origin/main   (the commit that is actually pushed)
        |
        v
    deployed sha  (GET / -> build.sha, from RAILWAY_GIT_COMMIT_SHA)
        |
        v
    verdict

WHY THIS IS NOT PEDANTRY
Ripple modifies other people's code. "Which commit produced this PR" cannot be a
guess, and "the tests pass" is not evidence about production unless it names the
revision it tested. Every prior claim in this repository was of the form "the code
does X" -- true of a working tree, and silent about a deployment that has been ~14
commits behind for a day.

WHAT IT REFUSES TO DO
Pass on absence of evidence, in five distinct ways. Each is a separate failure with
its own message, because "release verification failed" that could mean any of five
things is barely better than no check:

    no tested sha recorded        the suite has not run, or could not read git
    tested tree was DIRTY         the tested code is not ANY commit, so it cannot
                                  legitimately equal a deployed sha -- a refusal,
                                  not a mismatch
    deployed sha unknown          `/` has no build block, so the running revision
                                  is undeterminable. NOT a claim of staleness -- a
                                  refusal to guess
    tested sha not pushed         the deployed service cannot be running a commit
                                  that origin has never seen
    tested != deployed            the honest mismatch, with the distance in commits

A note on the last one: the distance is reported because "3 commits behind" and
"unrelated history" need different responses, and a bare != cannot tell them apart.

Usage:
    python tools/verify_release.py
    python tools/verify_release.py --url https://...
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RUN_EVIDENCE = os.path.join(ROOT, "tests", ".last_run.json")
DEFAULT_URL = "https://ripple-production-be7f.up.railway.app"
TIMEOUT = 30


def _git(*args) -> str:
    try:
        r = subprocess.run(["git", "-C", ROOT, *args],
                           capture_output=True, text=True, timeout=15)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _deployed(url: str) -> tuple:
    """(build_dict, error). A missing build block is distinguished from a dead host."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/", timeout=TIMEOUT) as r:
            payload = json.loads(r.read().decode())
    except (urllib.error.URLError, ValueError, TimeoutError, OSError) as e:
        return None, f"cannot reach the service: {type(e).__name__}: {e}"
    if "build" not in payload:
        return None, ("`/` returned no `build` block, so the deployed revision is "
                      "undeterminable -- the running code predates app/build_info.py")
    return payload["build"], ""


def main(argv: list) -> int:
    url = DEFAULT_URL
    if "--url" in argv:
        url = argv[argv.index("--url") + 1]

    print("=" * 78)
    print("RELEASE GATE -- does production run the code that was tested?")
    print("=" * 78)

    failures = []

    # --- 1. what was tested -------------------------------------------------
    tested = None
    dirty = None
    try:
        with open(RUN_EVIDENCE) as fh:
            evidence = json.load(fh)
        tested = evidence.get("tested_sha")
        dirty = evidence.get("tested_tree_dirty")
        total = evidence.get("total")
        failed = evidence.get("failed") or []
    except (OSError, ValueError) as e:
        evidence, total, failed = {}, None, []
        failures.append(f"no run evidence at tests/.last_run.json ({type(e).__name__}) "
                        f"-- run the suite first; an unrun suite is not a pass")

    print(f"\n  tested sha        {tested or 'NOT RECORDED'}")
    print(f"  tested tree       {'DIRTY' if dirty else 'clean' if dirty is False else 'unknown'}")
    print(f"  suite result      {total if total is not None else '?'} tests, "
          f"{len(failed)} failed")

    if evidence and not tested:
        failures.append(
            "the run evidence records no tested_sha, so the suite result cannot be "
            "attributed to any revision")
    if failed:
        failures.append(f"the recorded run had {len(failed)} failing test(s): "
                        f"{', '.join(failed[:3])}")
    if dirty:
        failures.append(
            "the tested tree was DIRTY, so the tested code is not any commit and can "
            "never legitimately equal a deployed sha. Commit, re-run, then release")

    # --- 2. is it pushed ----------------------------------------------------
    remote_head = _git("rev-parse", "origin/main")
    unpushed = _git("rev-list", "--count", "origin/main..HEAD")
    print(f"\n  origin/main       {remote_head or 'UNKNOWN'}")
    print(f"  unpushed commits  {unpushed or '?'}")

    if tested and remote_head:
        # `--is-ancestor` communicates via EXIT CODE and prints nothing, so the
        # helper that returns stdout cannot read it.
        try:
            rc = subprocess.run(["git", "-C", ROOT, "merge-base", "--is-ancestor",
                                 tested, remote_head],
                                capture_output=True, timeout=15).returncode
        except (OSError, subprocess.SubprocessError):
            rc = 1
        if rc != 0:
            failures.append(
                f"the tested sha {tested[:8]} is NOT an ancestor of origin/main, so "
                f"no deployment from that branch can be running it "
                f"({unpushed or '?'} commit(s) unpushed)")

    # --- 3. what is deployed ------------------------------------------------
    build, err = _deployed(url)
    if err:
        print(f"\n  deployed sha      UNKNOWN")
        print(f"                    {err}")
        failures.append(f"deployed revision undeterminable -- {err}. Unverifiable is "
                        f"not verified")
    else:
        deployed = build.get("sha")
        print(f"\n  deployed sha      {deployed or 'UNKNOWN'} "
              f"({build.get('source', '?')})")
        if not deployed:
            failures.append(
                "the build block reports no sha, so the running revision is unknown "
                "-- set RAILWAY_GIT_COMMIT_SHA")
        elif tested and deployed != tested:
            distance = _git("rev-list", "--count", f"{deployed}..{tested}") or "?"
            failures.append(
                f"tested {tested[:8]} != deployed {deployed[:8]} -- production is "
                f"running different code from what passed the suite "
                f"({distance} commit(s) between them)")

    print("\n" + "-" * 78)
    if failures:
        print(f"  RELEASE VERIFICATION: FAIL ({len(failures)} reason(s))")
        for msg in failures:
            print(f"      {msg}")
        return 1

    print("  RELEASE VERIFICATION: PASS")
    print(f"  the live service is running {tested[:8]}, the exact commit that passed "
          f"{total} tests.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
