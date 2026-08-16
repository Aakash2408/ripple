#!/usr/bin/env python3
"""Show the exact diff each generated fix would apply.

A PR containing a WRONG fix is worse than no PR -- it destroys trust in the
product on first contact. This prints the real unified diff per consumer so
the fix can be reviewed before anything is pushed to GitHub.

Usage: python3.12 tools/inspect_fixes.py
"""
from __future__ import annotations

import difflib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

# Reuse the harness's token loader + framework stub
_harness = os.path.join(HERE, "local_e2e.py")
_src = open(_harness).read()
exec(_src.split("# ------------------------------------------------------------ helpers")[0])

OLD_PROTO = """syntax = "proto3";
package user.v1;
message User {
  string id = 1;
  string name = 2;
  string email = 3;
  string phone_number = 4;
}
"""
NEW_PROTO = """syntax = "proto3";
package user.v1;
message User {
  string id = 1;
  string name = 2;
  string email = 3;
}
"""


def main() -> int:
    token = load_token()
    if not token:
        print("no token")
        return 2
    os.environ["GITHUB_TOKEN"] = token
    install_fastapi_stub()

    from app import webhook as wh

    change = wh.diff_proto(OLD_PROTO, NEW_PROTO, file_path="user.proto")[0]

    repos = ["Aakash2408/auth-service", "Aakash2408/billing-api",
             "Aakash2408/notifications-svc"]

    verdicts = []
    for repo in repos:
        files = wh._search_repo_for_consumers(repo, change, token, exclude_path="user.proto")
        for path, content in files:
            consumer = wh.ConsumerMatch(
                file_path=path, line_number=0, code_snippet="",
                confidence="high", match_reason="inspect",
                language=wh._detect_lang(path),
            )
            fixed, _ = wh._generate_fix_with_rag_fallback(content, consumer, change, "Aakash2408")

            print("=" * 70)
            print(f"{repo}  ::  {path}   ({wh._detect_lang(path)})")
            print("=" * 70)
            diff = list(difflib.unified_diff(
                content.splitlines(), fixed.splitlines(),
                fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="", n=2,
            ))
            if not diff:
                print("  (no change)\n")
                continue
            for line in diff:
                print("  " + line)

            # Flag removals that have nothing to do with the changed field
            removed = [l[1:].strip() for l in diff
                       if l.startswith("-") and not l.startswith("---")]
            field_forms = {"phone_number", "phonenumber", "phone-number", "phone"}
            collateral = [
                r for r in removed
                if r and not any(f in r.lower() for f in field_forms)
            ]
            if collateral:
                print(f"\n  ⚠️  {len(collateral)} line(s) removed with NO reference to the field:")
                for c in collateral:
                    print(f"       - {c[:66]}")

            # More important: does the FIXED file still reference the
            # field that no longer exists? That code cannot run.
            residual = [
                (i + 1, l.strip()) for i, l in enumerate(fixed.splitlines())
                if "phone_number" in l.lower().replace("_", "").replace("-", "")
                or "phonenumber" in l.lower().replace("_", "").replace("-", "")
            ]
            if residual:
                print(f"\n  ❌ {len(residual)} SURVIVING reference(s) to the removed field:")
                for lineno, text in residual[:6]:
                    print(f"       line {lineno}: {text[:64]}")

            if residual:
                verdicts.append((repo, path, "BROKEN_REFS", residual))
            elif collateral:
                verdicts.append((repo, path, "COLLATERAL", collateral))
            else:
                verdicts.append((repo, path, "CLEAN", []))
            print()

    print("=" * 70)
    print("VERDICT")
    print("=" * 70)
    bad = 0
    for repo, path, status, extra in verdicts:
        mark = "✅" if status == "CLEAN" else "❌"
        print(f"  {mark} {repo.split('/')[-1]:20} {path:16} {status}"
              + (f" ({len(extra)} bad line(s))" if extra else ""))
        if status != "CLEAN":
            bad += 1
    print(f"\n  {len(verdicts) - bad}/{len(verdicts)} fixes safe to ship")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
