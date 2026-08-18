#!/usr/bin/env python3
"""Prove the DEPLOYED store survives a redeploy. Exits 1 until it does.

WHY THIS IS SEPARATE FROM THE TEST SUITE
tests/test_regression.py::test_activity_survives_process_restart proves the CODE
persists: it writes in one interpreter and reads in another. That is necessary and
not sufficient. Railway's container filesystem is writable and ephemeral, so the
code can persist perfectly into storage that is discarded on the next deploy --
which is exactly what happened, six times. `event_count: 0` after every redeploy.

This checks the thing only the live service can answer, so it cannot be a CI gate:
it needs network access and a deployed instance. It is the ACCEPTANCE check for
mounting the volume.

WHAT IT WILL NOT DO
It will not report success from absence of evidence. If it cannot reach the
service, or cannot tell whether the directory is a mount, it fails -- the same
rule the capability registry applies to validation. "Cannot verify" is not
"verified".

Usage:
    python tools/verify_durability.py                       # check the default host
    python tools/verify_durability.py --url https://...     # another instance
    python tools/verify_durability.py --before              # record the pre-deploy state
    python tools/verify_durability.py --after               # compare after a redeploy
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "https://ripple-production-be7f.up.railway.app"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          ".durability_check.json")
TIMEOUT = 25


def _get(url: str, path: str) -> dict:
    try:
        with urllib.request.urlopen(url.rstrip("/") + path, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, ValueError, TimeoutError) as e:
        raise SystemExit(f"  FAIL  cannot reach {path}: {type(e).__name__}: {e}\n"
                         f"        unreachable is not durable -- refusing to pass")


def _local_head() -> str:
    """Local HEAD, or "" if undeterminable. Used only to CONTRAST with the deployed
    revision -- never as a substitute for it."""
    import subprocess
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        out = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def main(argv: list) -> int:
    url = DEFAULT_URL
    if "--url" in argv:
        url = argv[argv.index("--url") + 1]

    health = _get(url, "/health/storage")
    st = health.get("storage", {})
    durable = bool(st.get("durable"))
    print(f"  host           {url}")
    print(f"  dir            {st.get('dir')}")
    print(f"  activity file  {st.get('activity_file')}  "
          f"exists={st.get('activity_exists')}")
    print(f"  event_count    {st.get('event_count')}")
    print(f"  durable        {durable}  ({st.get('durability_reason')})")

    # WHICH CODE IS RUNNING. Reported before any verdict, because a durability
    # reading against a stale deploy tells you about code you did not ship. After
    # pushing 8 commits there was no way to answer this at all: `/` returned a
    # hardcoded "0.1.0" and /health/storage was byte-identical to before the push,
    # so "is the fix deployed?" was unanswerable rather than yes or no.
    build = _get(url, "/health").get("build") or {}
    deployed = build.get("sha")
    if deployed:
        print(f"  deployed       {build.get('short')}  (source {build.get('source')})")
        local = _local_head()
        if local and deployed != local:
            print(f"  LOCAL HEAD IS  {local[:8]}  -- the deploy is NOT running your "
                  f"local commit")
        elif local:
            print(f"  matches local HEAD {local[:8]}")
    else:
        # Not a failure of durability. Say which question is unanswerable, rather
        # than letting a missing field read as a pass.
        print(f"  deployed       UNKNOWN -- {build.get('detail', 'no build info on /health')}")

    # --before / --after turn this into a real redeploy comparison rather than a
    # point-in-time reading. A single "durable: true" only says the directory
    # looks like a mount; surviving an actual redeploy is the claim that matters.
    if "--before" in argv:
        with open(STATE_FILE, "w") as fh:
            json.dump({"url": url, "event_count": st.get("event_count"),
                       "activity_exists": st.get("activity_exists")}, fh)
        print(f"\n  recorded pre-deploy state -> {STATE_FILE}")
        print(f"  now trigger a redeploy, then re-run with --after")
        return 0

    if "--after" in argv:
        if not os.path.exists(STATE_FILE):
            print("\n  FAIL  no pre-deploy state; run --before first")
            return 1
        before = json.load(open(STATE_FILE))
        b, a = before.get("event_count", 0), st.get("event_count", 0)
        print(f"\n  event_count before redeploy  {b}")
        print(f"  event_count after redeploy   {a}")
        if b == 0:
            print("  INCONCLUSIVE  there was nothing to lose -- generate activity "
                  "first, then re-run --before")
            return 1
        if a < b:
            print(f"  FAIL  lost {b - a} event(s) across the redeploy: the store "
                  f"is NOT durable")
            return 1
        print("  PASS  state survived an actual redeploy")
        return 0

    if not durable:
        print(f"\n  FAIL  storage is not durable")
        print(f"        {health.get('storage', {}).get('hint') or ''}")
        print(f"\n  WHAT THIS NEEDS (Railway dashboard -- not something code can do):")
        print(f"        1. Railway project -> the Ripple service -> Variables/Settings")
        print(f"        2. Add a Volume, mount path exactly: /app/data")
        print(f"        3. Redeploy so the container picks the mount up")
        print(f"        4. Re-run this script: `durable` must become true")
        print(f"        5. Then prove it end to end:")
        print(f"             python tools/verify_durability.py --before")
        print(f"             (trigger a redeploy)")
        print(f"             python tools/verify_durability.py --after")
        print(f"\n        The app already reads RIPPLE_DATA_DIR, so an alternative")
        print(f"        is to point that at an existing mounted path instead.")
        return 1

    print("\n  PASS  storage directory is a real mount")
    print("        this is necessary, not sufficient -- run --before / --after "
          "around a redeploy to prove survival")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
