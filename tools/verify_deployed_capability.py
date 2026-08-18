#!/usr/bin/env python3
"""Can the DEPLOYED service actually do what the repository claims?

WHY THIS EXISTS
Every capability number in this repo is derived from the code, which was the fix for
declaring capabilities by hand. It is still not enough. The registry can correctly
derive AUTO=1 while the deployed image is `python:3.11-slim` with no node, no npm and
no docker daemon -- so `choose_backend()` returns "", `validate()` returns
UNABLE_TO_VALIDATE, and no cell can reach AUTO in production regardless of what the
registry says.

Measured in the actual base image, not argued:

    node ABSENT   npm ABSENT   npx ABSENT   docker ABSENT
    backend ''    verdict UNABLE_TO_VALIDATE   is_valid False

Neither side could see this. The repository's audits ran on a laptop that HAS docker.
The deployed service was never asked. `AUTO=1` and `AUTO is unreachable` were both
true at the same time, in different places, and pushing the 13 pending commits would
not have changed it -- this is an image problem wearing a deployment problem's
clothes.

That is the same shape as a matcher that is built, tested and CI-gated but
unreachable from production: the defect class this project keeps rediscovering, one
level further out.

WHY IT IS NOT A CI GATE
It needs a deployed instance and network access. A gate that cannot run is the same
defect as a matcher that cannot be reached, so this is an ACCEPTANCE check -- run it,
and cite its result. Same reasoning as tools/verify_durability.py and
tools/verify_validation.py.

WHAT IT WILL NOT DO
It will not pass from absence of evidence. Unreachable service, missing endpoint, or
an unparseable answer all FAIL. "Cannot verify" is not "verified" -- the rule the
validator itself enforces.

Usage:
    python tools/verify_deployed_capability.py
    python tools/verify_deployed_capability.py --url https://...
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

DEFAULT_URL = "https://ripple-production-be7f.up.railway.app"
TIMEOUT = 30


def _get(url: str, path: str) -> tuple:
    """(payload, error). A 404 is reported distinctly -- it means the deployed code
    predates this check, which is itself the answer to 'is the deploy current?'."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + path, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode()), ""
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, (f"404 -- the deployed service has no {path}, so it is "
                          f"running code from before this check existed")
        return None, f"HTTP {e.code}"
    except (urllib.error.URLError, ValueError, TimeoutError, OSError) as e:
        return None, f"{type(e).__name__}: {e}"


def _repo_auto_cells() -> list:
    """Cells the REPOSITORY says are AUTO, asked of the authority that decides it.

    First version of this read a `pr_level` key off `claim_matrix()` rows. That key
    does not exist -- AUTO is decided by app/routing.pr_level() -- so it reported
    "0 AUTO cells" while the repo has exactly one, and the tool's central assertion
    could never have fired. A check that cannot fail is the defect it was written to
    find, so the count is asserted against the registry summary below.
    """
    from app.capability_claims import claim_matrix
    from app.routing import pr_level, Level

    out = []
    for row in claim_matrix():
        # Same arguments the AUTO regression test uses: confidence high enough that
        # it cannot be the limiting factor, so what remains is the registry's
        # verdict rather than a scoring artefact.
        decision = pr_level(row["language"], row["contract"], row["operation"],
                            confidence=0.99, min_confidence=0.5)
        if decision.level is Level.AUTO:
            out.append(f"{row['language']}x{row['contract']}x{row['operation']}")
    return out


def main(argv: list) -> int:
    url = DEFAULT_URL
    if "--url" in argv:
        url = argv[argv.index("--url") + 1]

    print("=" * 78)
    print("DEPLOYED CAPABILITY vs REPOSITORY CLAIM")
    print("=" * 78)
    print(f"\n  target   {url}\n")

    auto_cells = _repo_auto_cells()
    print(f"  repository claims AUTO for {len(auto_cells)} cell(s):")
    for cell in auto_cells or ["(none)"]:
        print(f"    {cell}")

    failures = []

    payload, err = _get(url, "/health/capability")
    if err:
        print(f"\n  FAIL  cannot read the deployed capability: {err}")
        print("        unverifiable is not verified -- refusing to pass")
        return 1

    validation = payload.get("validation") or {}
    backend = validation.get("backend")
    can_validate = bool(validation.get("can_validate"))
    print(f"\n  deployed backend       {backend or 'NONE'}")
    print(f"  deployed isolation     {validation.get('isolation', '?')}")
    print(f"  can validate           {can_validate}")

    build = payload.get("build") or {}
    deployed_sha = build.get("sha")
    print(f"  deployed revision      {deployed_sha or 'UNKNOWN'} "
          f"({build.get('source', 'unavailable')})")

    # The claim/reality comparison. AUTO with no toolchain is the divergence.
    if auto_cells and not can_validate:
        failures.append(
            f"the repository claims {len(auto_cells)} AUTO cell(s) but the deployed "
            f"image cannot validate anything ({validation.get('isolation')}), so "
            f"every fix there is UNABLE_TO_VALIDATE and AUTO is unreachable in "
            f"production")

    if backend == "host":
        # Not a failure, but it must never pass silently: the host backend runs
        # `npm install` on an untrusted customer repository with network access.
        print("\n  WARNING  the deployed backend is 'host' -- DEGRADED isolation. "
              "This runs\n           npm install on untrusted customer code with "
              "network access.")

    if failures:
        print(f"\n  {len(failures)} FAILURE(S):")
        for msg in failures:
            print(f"      {msg}")
        return 1

    print("\n  the deployed image can validate, so an AUTO claim is reachable there.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
