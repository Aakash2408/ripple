#!/usr/bin/env python3
"""Audit every diff engine for two systemic parser flaws.

FLAW 1 -- non-nesting block regex:  r'block\\s+(\\w+)\\s*\\{([^}]*)\\}'
  [^}]* cannot cross a '}', so the first NESTED closing brace truncates the
  block body. Every field after a nested message/enum/oneof/object becomes
  invisible -> silent FALSE NEGATIVE (reports "no breaking changes" on a
  schema that just broke its consumers).

FLAW 2 -- no comment stripping:
  A commented-out field is parsed as a live field, so deleting or
  uncommenting a comment line fabricates a breaking change -> FALSE
  POSITIVE (opens a PR for a change that never happened).

False negatives are the dangerous class: the user trusts a silent pass.

Usage: python3.12 tools/audit_diff_engines.py
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.proto_diff import diff_proto
from app.graphql_diff import diff_graphql
from app.smithy_diff import diff_smithy
from app.thrift_diff import diff_thrift
from app.migration_diff import diff_schema


# (engine_name, diff_fn, filename, nested_old, nested_new, commented_old, commented_new)
CASES = [
    (
        "proto", diff_proto, "x.proto",
        # nested: field 'gone' removed, but a nested message precedes it
        """message U {
  message Inner { string x = 1; }
  string keep = 1;
  string gone = 2;
}""",
        """message U {
  message Inner { string x = 1; }
  string keep = 1;
}""",
        # commented: only a COMMENT changes -- must not be breaking
        """message U {
  string keep = 1;
  // string gone = 2;
}""",
        """message U {
  string keep = 1;
}""",
    ),
    (
        "graphql", diff_graphql, "x.graphql",
        """type User {
  keep: String
  gone: String
}
enum Role { ADMIN USER }""",
        """type User {
  keep: String
}
enum Role { ADMIN USER }""",
        """type User {
  keep: String
  # gone: String
}""",
        """type User {
  keep: String
}""",
    ),
    (
        "smithy", diff_smithy, "x.smithy",
        """structure User {
    keep: String
    gone: String
}
operation GetUser {
    input: GetUserInput
}""",
        """structure User {
    keep: String
}
operation GetUser {
    input: GetUserInput
}""",
        """structure User {
    keep: String
    // gone: String
}""",
        """structure User {
    keep: String
}""",
    ),
    (
        "thrift", diff_thrift, "x.thrift",
        """struct User {
  1: string keep,
  2: string gone,
}
service S {
  User get(1: string id),
}""",
        """struct User {
  1: string keep,
}
service S {
  User get(1: string id),
}""",
        """struct User {
  1: string keep,
  // 2: string gone,
}""",
        """struct User {
  1: string keep,
}""",
    ),
    (
        "prisma/db", diff_schema, "schema.prisma",
        """model User {
  keep String
  gone String
}
model Other {
  x String
}""",
        """model User {
  keep String
}
model Other {
  x String
}""",
        """model User {
  keep String
  // gone String
}""",
        """model User {
  keep String
}""",
    ),
]


def run() -> int:
    print("=" * 74)
    print("DIFF ENGINE AUDIT")
    print("=" * 74)

    rows = []
    for name, fn, fname, n_old, n_new, c_old, c_new in CASES:
        # FLAW 1: real removal that follows a nested/second block
        try:
            nested = fn(n_old, n_new, file_path=fname)
            nested_detected = len(nested) > 0
            nested_err = ""
        except Exception as e:
            nested_detected = False
            nested_err = f"{type(e).__name__}: {e}"

        # FLAW 2: comment-only change
        try:
            commented = fn(c_old, c_new, file_path=fname)
            comment_fp = len(commented) > 0
            comment_err = ""
        except Exception as e:
            comment_fp = False
            comment_err = f"{type(e).__name__}: {e}"

        rows.append((name, nested_detected, nested_err, comment_fp, comment_err))

        print(f"\n--- {name} ---")
        status = "OK" if nested_detected else "FALSE NEGATIVE"
        print(f"  nested-block removal : {status}"
              + (f"  [{nested_err}]" if nested_err else "")
              + (f"  detected={[c.change_type for c in nested]}" if nested_detected else ""))
        status = "FALSE POSITIVE" if comment_fp else "OK"
        print(f"  comment-only change  : {status}"
              + (f"  [{comment_err}]" if comment_err else "")
              + (f"  detected={[c.change_type for c in commented]}" if comment_fp else ""))

    print("\n" + "=" * 74)
    print("SUMMARY")
    print("=" * 74)
    print(f"  {'engine':<14} {'nested removal':<18} {'comment-only':<18}")
    print(f"  {'-'*14} {'-'*18} {'-'*18}")
    fn_count = fp_count = 0
    for name, nested_ok, _, comment_fp, _ in rows:
        a = "OK" if nested_ok else "MISSED (FN)"
        b = "FALSE POSITIVE" if comment_fp else "OK"
        if not nested_ok:
            fn_count += 1
        if comment_fp:
            fp_count += 1
        print(f"  {name:<14} {a:<18} {b:<18}")
    print()
    print(f"  false negatives: {fn_count}/{len(rows)} engines "
          f"(silently report 'no breaking changes')")
    print(f"  false positives: {fp_count}/{len(rows)} engines "
          f"(open PRs for comment-only edits)")
    return 0


if __name__ == "__main__":
    sys.exit(run())
