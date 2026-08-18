#!/usr/bin/env python3
"""Run the pipeline against a REAL cloned repository and record what happened.

WHY THIS EXISTS SEPARATELY FROM THE FIXTURE
The golden fixture proves the path can work. It cannot prove the path survives
contact with a repository nobody designed for it. Those are different claims, and
Stage 7 exists because conflating them is how a demo becomes a product on paper
only.

WHAT IT DELIBERATELY DOES NOT DO
It does not open a pull request. Opening one needs GitHub App credentials, and the
PAT that was embedded in the git remote has been revoked. More importantly, a PR
opened from a laptop is not the claim worth making -- the claim worth making is that
the DEPLOYED service opened it, and the deployed service is stale (see below). So
this stops at the last step it can honestly complete, and says so.

Usage:
    python3.12 tools/verify_real_repo.py
    python3.12 tools/verify_real_repo.py --repo https://github.com/owner/name.git \\
                                         --field phoneNumber
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

DEFAULT_REPO = "https://github.com/Aakash2408/billing-api.git"
DEFAULT_FIELD = "phoneNumber"
LIVE = "https://ripple-production-be7f.up.railway.app"


def _deployed_revision() -> dict:
    """What the live service says it is running, or why we cannot tell."""
    import urllib.request
    try:
        with urllib.request.urlopen(LIVE + "/", timeout=20) as r:
            body = json.load(r)
    except Exception as exc:
        return {"reachable": False, "detail": str(exc)[:120]}
    build = body.get("build")
    if not build:
        return {"reachable": True, "build_field": False,
                "detail": "`/` returns no `build` block, so the deployed code "
                          "predates the commit that added it. The revision is "
                          "OLDER than app/build_info.py, which is itself the "
                          "answer."}
    return {"reachable": True, "build_field": True, **build}


def _local_head() -> str:
    out = subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"],
                         capture_output=True, text=True)
    return out.stdout.strip()[:8] if out.returncode == 0 else "?"


def main(argv: list) -> int:
    from app import languages
    from app.smart_consumer_finder import find_matches_in_file
    from app.change_types import vector_for
    from app.ts_codemod import remove_field
    from app.validation import validate
    from app.routing import pr_level
    from app.run_outcome import ChangeRun, Terminal

    repo = argv[argv.index("--repo") + 1] if "--repo" in argv else DEFAULT_REPO
    field = argv[argv.index("--field") + 1] if "--field" in argv else DEFAULT_FIELD

    print("=" * 74)
    print("REAL REPOSITORY PROOF")
    print("=" * 74)

    dep = _deployed_revision()
    print(f"\n  local HEAD        {_local_head()}")
    print(f"  deployed revision {dep.get('short') or 'UNKNOWN'}")
    if not dep.get("build_field"):
        print(f"                    {dep.get('detail','')[:100]}")
    print(f"\n  repo   {repo}")
    print(f"  field  {field}\n")

    tmp = tempfile.mkdtemp(prefix="ripple-realrepo-")
    try:
        clone = subprocess.run(
            ["git", "clone", "-q", "--depth", "1", repo, os.path.join(tmp, "r")],
            capture_output=True, text=True, timeout=180)
        if clone.returncode != 0:
            print(f"  FAIL  clone failed: {clone.stderr.strip()[:200]}")
            return 1
        work = os.path.join(tmp, "r")

        # --- consumer discovery, using the PRODUCTION matcher -----------------
        found = []
        for base, _dirs, files in os.walk(work):
            if ".git" in base:
                continue
            for name in files:
                path = os.path.join(base, name)
                rel = os.path.relpath(path, work)
                lang = languages.detect(rel)
                if not lang or not languages.is_scannable(rel):
                    continue
                try:
                    content = open(path, encoding="utf-8").read()
                except (OSError, UnicodeDecodeError):
                    continue
                hits = find_matches_in_file(content, rel, field, lang,
                                            vector=vector_for("field_removed"))
                if hits:
                    found.append((rel, lang, content, len(hits)))

        print(f"  consumers found: {len(found)}")
        for rel, lang, _c, n in found:
            print(f"      {rel}  ({lang}, {n} match(es))")
        if not found:
            print("\n  NO_CONSUMER -- nothing references the field")
            return 0

        # --- transformation ---------------------------------------------------
        with ChangeRun(change_type="removed_field", spec=f"<{field}>",
                       repo=repo) as run:
            for rel, lang, content, _n in found:
                run.consumer_found(rel)
                if lang != "typescript":
                    run.refused(rel, f"no codemod for {lang}")
                    print(f"\n  {rel}: REFUSED, no codemod for {lang}")
                    continue
                result = remove_field(content, field)
                print(f"\n  {rel}")
                print(f"      edits    {len(result.edits)}   "
                      f"refusals {len(result.refusals)}   "
                      f"complete {result.complete}")
                for e in result.edits:
                    print(f"        EDIT   {e['shape']}: {e['removed'][:56]}")
                for x in result.refusals:
                    print(f"        REFUSE {x[:104]}")

                if not result.complete:
                    run.refused(rel, "; ".join(result.refusals)[:200]
                                or "transformation incomplete")
                    continue

                # --- validation, which needs the repo to BE a project ---------
                open(os.path.join(work, rel), "w").write(result.code)
                verdict = validate(lang, work)
                print(f"      validate {verdict.state.value}: {verdict.reason[:88]}")
                if verdict.is_valid:
                    decision = pr_level(lang, "openapi", "removed_field", 0.95, 0.5)
                    print(f"      level    {decision.level.value}")
                    run.pr_created("<not opened: see header>", rel, validated=True)
                else:
                    run.refused(rel, verdict.reason[:200])
            terminal = run.terminal()

        print(f"\n  terminal state: {terminal.value}")
        print("\n  NOT PROVEN HERE, and why:")
        print("    * no PR was opened -- that needs GitHub App credentials, and the")
        print("      claim worth making is that the DEPLOYED service opened it")
        print("    * the deployed service is older than app/build_info.py, so a live")
        print("      webhook run would exercise code without the codemod, the")
        print("      validator, or the terminal state")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
