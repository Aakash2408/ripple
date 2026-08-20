#!/usr/bin/env python3
"""Derive MAX_ACTIVE_PATTERNS and ARCHIVE_AFTER_DAYS from measurement.

Both were introduced as round numbers -- KiroCrew's shape with our digits, with
nothing behind either. They are different KINDS of number and need different
evidence, which is the point of separating them here:

  MAX_ACTIVE_PATTERNS   a COST bound. Fully measurable now, with no production
                        data, because it is about bytes and milliseconds:
                        save() rewrites the whole file on every outcome and
                        retrieve_fix_pattern() scores every row.

  ARCHIVE_AFTER_DAYS    a claim about how long a pattern stays USEFUL. Ripple
                        holds zero patterns, so this cannot be measured from
                        Ripple at all. The nearest real evidence is PropBench:
                        881 dated entries mined from OSS repos, from which the
                        RECURRENCE INTERVAL of changes touching the same file
                        can be measured.

                        That is a PROXY, not the quantity. It answers "how long
                        until something of this shape comes back", which is what
                        decides whether a dormancy threshold ever bites. It does
                        NOT answer "how long until a fix strategy goes stale."
                        Reported as a proxy, and the residual guess is named.

Usage:  python3 tools/measure_store_constants.py [--propbench DIR]
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from collections import defaultdict

DEFAULT_PROPBENCH = os.path.expanduser(
    "~/.meshclaw/workspace/judgment-engine/datasets")


# --------------------------------------------------------------- ageing
def measure_recurrence(propbench_dir: str) -> dict:
    """Interval in days between successive changes touching the same file.

    One observation per (repo, file) pair per consecutive date pair. Entries
    without a usable date are counted and excluded rather than defaulted -- a
    missing date is not day zero.
    """
    try:
        import yaml
    except ImportError:
        return {"error": "pyyaml not available"}

    families = os.path.join(propbench_dir, "families")
    if not os.path.isdir(families):
        return {"error": f"no families/ under {propbench_dir}"}

    seen: dict[tuple, list[str]] = defaultdict(list)
    entries = undated = 0

    for root, _dirs, files in os.walk(families):
        for name in files:
            if not name.endswith((".yaml", ".yml")):
                continue
            try:
                doc = yaml.safe_load(open(os.path.join(root, name)).read())
            except Exception:                                    # noqa: BLE001
                continue
            if not isinstance(doc, dict):
                continue
            entries += 1
            date = str(doc.get("date") or "")
            if len(date) < 10:
                undated += 1
                continue
            repo = str(doc.get("source_repo") or doc.get("repo") or "?")
            for cons in doc.get("consequences") or []:
                if not isinstance(cons, dict):
                    continue
                for path in cons.get("files") or []:
                    seen[(repo, str(path))].append(date[:10])

    def to_days(a: str, b: str) -> float:
        fmt = "%Y-%m-%d"
        return abs(time.mktime(time.strptime(b, fmt))
                   - time.mktime(time.strptime(a, fmt))) / 86400

    intervals: list[float] = []
    recurring = 0
    for _key, dates in seen.items():
        uniq = sorted(set(dates))
        if len(uniq) < 2:
            continue
        recurring += 1
        for i in range(1, len(uniq)):
            try:
                intervals.append(to_days(uniq[i - 1], uniq[i]))
            except ValueError:
                pass

    if not intervals:
        return {"entries": entries, "undated": undated,
                "distinct_files": len(seen), "recurring_files": recurring,
                "intervals": 0,
                "note": "no file recurred on two distinct dates"}

    intervals.sort()

    def pct(p: float) -> float:
        return intervals[min(len(intervals) - 1, int(len(intervals) * p))]

    return {
        "entries": entries,
        "undated": undated,
        "distinct_files": len(seen),
        "recurring_files": recurring,
        "recurrence_rate": recurring / len(seen) if seen else 0.0,
        "intervals": len(intervals),
        "median_days": statistics.median(intervals),
        "p75_days": pct(0.75),
        "p90_days": pct(0.90),
        "p95_days": pct(0.95),
        "max_days": intervals[-1],
    }


# ------------------------------------------------------------------ cap
def measure_cost() -> dict:
    """Bytes per row, and the cost of save/load/retrieve as N grows.

    save() is the one that matters: it rewrites the ENTIRE file on every
    recorded outcome, so its cost is paid per webhook, not per query.
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import tempfile

    scratch = tempfile.mkdtemp(prefix="ripple_cost_")
    os.environ["RIPPLE_DATA_DIR"] = scratch

    from app.rag_store import FixPattern, PatternStore
    from app.rag_retriever import _multi_signal_score

    def row(i: int) -> FixPattern:
        return FixPattern(
            pattern_id=f"{i:016x}", change_type="field_removed",
            language="typescript", field_name=f"someFieldName{i}",
            strategy="field_removed in typescript -- merged unchanged by a reviewer",
            source_file=f"packages/service-{i % 40}/src/handlers/resource.ts",
            repo=f"acme/service-{i % 40}", merge_count=3, reject_count=1,
            last_used=time.time(), example_count=2, provenance="merged_clean")

    one = len(json.dumps(row(1).__dict__)) + 1
    rows = []
    out: dict = {"bytes_per_row": one, "curve": []}

    for n in (500, 1000, 2000, 5000, 10000, 25000):
        while len(rows) < n:
            rows.append(row(len(rows)))
        st = PatternStore("cost")
        st._loaded = True
        st.patterns = list(rows)
        st.structured_patterns = []
        st.archived = []

        t0 = time.perf_counter()
        st.save()
        save_ms = (time.perf_counter() - t0) * 1000

        fresh = PatternStore("cost")
        t0 = time.perf_counter()
        fresh.load()
        load_ms = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        for p in st.patterns:
            _multi_signal_score(p, "field_removed", "typescript", "acme/service-1")
        score_ms = (time.perf_counter() - t0) * 1000

        out["curve"].append({
            "n": n, "file_kb": (one * n) / 1024,
            "save_ms": save_ms, "load_ms": load_ms, "retrieve_ms": score_ms,
        })

    import shutil
    shutil.rmtree(scratch, ignore_errors=True)
    return out


def main() -> int:
    pb = DEFAULT_PROPBENCH
    if "--propbench" in sys.argv:
        pb = sys.argv[sys.argv.index("--propbench") + 1]

    print("\n" + "=" * 74)
    print("  ARCHIVE_AFTER_DAYS -- recurrence interval (PROXY, real dated data)")
    print("=" * 74)
    rec = measure_recurrence(pb)
    for k, v in rec.items():
        print(f"    {k:<20} {v if not isinstance(v, float) else f'{v:.1f}'}")

    print("\n" + "=" * 74)
    print("  MAX_ACTIVE_PATTERNS -- measured cost (save() runs per outcome)")
    print("=" * 74)
    cost = measure_cost()
    print(f"    bytes per row  {cost['bytes_per_row']}")
    print(f"\n    {'N':>7} {'file':>10} {'save':>9} {'load':>9} {'retrieve':>10}")
    for c in cost["curve"]:
        print(f"    {c['n']:>7} {c['file_kb']:>8.0f}kB {c['save_ms']:>7.1f}ms "
              f"{c['load_ms']:>7.1f}ms {c['retrieve_ms']:>8.1f}ms")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
