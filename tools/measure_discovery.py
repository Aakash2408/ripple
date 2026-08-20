#!/usr/bin/env python3
"""Stage 6: measure discovery before and after Stages 4 and 5.

BEFORE  = `consumers[:5]` -- the platform's own search order, unscored.
AFTER   = admit above the floor -> rank by strength -> MMR -> cap.

Ground truth is declared per fixture, so recall is a number rather than a claim.
Two properties are measured separately because they are different claims:

  recall    did we retrieve the files that genuinely reference the field?
            Stage 4 moves this. A candidate never fetched cannot be fixed.
  breadth   how many distinct packages are represented in the survivors?
            Stage 5 moves this. It is a reallocation, NOT a recall gain.

Run with RIPPLE_DATA_DIR set to a scratch dir.
"""
from __future__ import annotations

import os
import sys

FIELD = "phoneNumber"
CAP = 5

# --- fixtures -------------------------------------------------------------
# Each: (path, content, is_real_consumer)
# "Real consumer" = the file would fail to compile / behave wrongly if the
# field were removed. A changelog mentioning the name would not.

FIXTURE_PROSE_FLOOD = [
    # the platform returns docs first -- lexical search favours prose density
    ("docs/changelog-2024-01.md", f"- removed {FIELD} from the user payload\n" * 3, False),
    ("docs/changelog-2024-02.md", f"- {FIELD} deprecation notice\n" * 3, False),
    ("docs/changelog-2024-03.md", f"- {FIELD} migration guide\n" * 3, False),
    ("docs/changelog-2024-04.md", f"- {FIELD} removal plan\n" * 3, False),
    ("docs/changelog-2024-05.md", f"- {FIELD} rollout\n" * 3, False),
    ("packages/reporting/src/summary.ts",
     f"export function render(u: User) {{ return u.{FIELD}.trim(); }}", True),
    ("packages/reporting/src/report.ts",
     f'const cols = ["id", "{FIELD}", "email"];', True),
]

FIXTURE_ONE_PACKAGE_DOMINATES = [
    ("packages/checkout/src/client.ts", f"u.{FIELD}", True),
    ("packages/checkout/src/cart.ts", f"u.{FIELD}", True),
    ("packages/checkout/src/order.ts", f"u.{FIELD}", True),
    ("packages/checkout/src/pay.ts", f"u.{FIELD}", True),
    ("packages/checkout/src/ship.ts", f"u.{FIELD}", True),
    ("packages/reporting/src/summary.ts", f"u.{FIELD}", True),
    ("packages/notify/src/sms.ts", f"u.{FIELD}", True),
]


def _package_of(path: str) -> str:
    parts = path.split("/")
    return "/".join(parts[:2]) if len(parts) > 2 else parts[0]


def _measure(name: str, fixture: list[tuple[str, str, bool]]) -> dict:
    from app import webhook

    paths = [p for p, _c, _r in fixture]
    body = {p: c for p, c, _r in fixture}
    truth = {p for p, _c, r in fixture if r}

    def fetch(path: str):
        return body.get(path)

    # ---- BEFORE: platform order, unscored, hard cut
    before = paths[:CAP]

    # ---- AFTER: the production path
    budget = {"remaining": 60}
    admitted = webhook._admit_consumers(
        paths, fetch,
        field_name=FIELD,
        language_of=lambda p: "typescript" if p.endswith(".ts") else "markdown",
        max_consumers=CAP,
        budget=budget,
        candidate_window=25,
    )
    after = [p for p, _c, _s in admitted]
    strengths = {p: s for p, _c, s in admitted}

    def recall(sel: list[str]) -> float:
        return len(set(sel) & truth) / len(truth) if truth else 0.0

    def breadth(sel: list[str]) -> int:
        return len({_package_of(p) for p in sel})

    print(f"\n=== {name}")
    print(f"    ground truth: {len(truth)} real consumers of {len(paths)} candidates"
          f"   cap={CAP}")
    print(f"\n    BEFORE  consumers[:{CAP}]  (platform order, no scoring)")
    for p in before:
        print(f"      {'REAL' if p in truth else '    '}  {p}")
    print(f"      recall {recall(before):.2f}   packages {breadth(before)}")

    print(f"\n    AFTER   admit -> rank -> mmr -> cap")
    for p in after:
        print(f"      {'REAL' if p in truth else '    '}  {strengths[p]:.2f}  {p}")
    print(f"      recall {recall(after):.2f}   packages {breadth(after)}"
          f"   budget left {budget['remaining']}")

    return {
        "name": name,
        "recall_before": recall(before), "recall_after": recall(after),
        "breadth_before": breadth(before), "breadth_after": breadth(after),
    }


def main() -> int:
    rows = [
        _measure("prose floods the platform's search order", FIXTURE_PROSE_FLOOD),
        _measure("one package dominates the candidates", FIXTURE_ONE_PACKAGE_DOMINATES),
    ]

    print("\n" + "=" * 72)
    print(f"    {'fixture':<44} {'recall':<14} {'packages'}")
    for r in rows:
        rc = f"{r['recall_before']:.2f} -> {r['recall_after']:.2f}"
        bd = f"{r['breadth_before']} -> {r['breadth_after']}"
        print(f"    {r['name']:<44} {rc:<14} {bd}")

    failures = []
    # Stage 4's claim: recall must not fall, and must rise where prose displaced code.
    if rows[0]["recall_after"] <= rows[0]["recall_before"]:
        failures.append("prose fixture: recall did not improve")
    # Stage 5's claim: breadth rises where one package dominates, recall unchanged.
    if rows[1]["breadth_after"] <= rows[1]["breadth_before"]:
        failures.append("dominance fixture: breadth did not improve")
    for r in rows:
        if r["recall_after"] < r["recall_before"]:
            failures.append(f"{r['name']}: recall REGRESSED")

    print()
    if failures:
        print("    FAIL: " + "; ".join(failures))
        return 1
    print("    PASS: recall improved where prose displaced code; breadth improved")
    print("          where one package dominated; recall regressed nowhere.")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if not os.environ.get("RIPPLE_DATA_DIR"):
        # Manage our own scratch dir so this is a single gate entry and never
        # writes rows into the mounted volume.
        import shutil
        import tempfile
        scratch = tempfile.mkdtemp(prefix="ripple_discovery_measure_")
        os.environ["RIPPLE_DATA_DIR"] = scratch
        try:
            sys.exit(main())
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    sys.exit(main())
