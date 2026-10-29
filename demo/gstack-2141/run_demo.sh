#!/usr/bin/env bash
#
# Reproduce the review miss recorded in gstack PR #2141, and run Ripple on it.
#
#   https://github.com/garrytan/gstack/pull/2141
#
# The PR describes two misses a diff-scoped /review pass did not catch:
#   1. a validator was loosened to accept a typeless .pdf, while an UNCHANGED
#      submit helper still forced every typeless file to text/plain;
#   2. a "Total file size..." message went stale after the logic behind it
#      changed to count only some files.
# In both, the code that was now wrong never appeared in the diff.
#
# This builds a throwaway git repo: `base/` is the first commit, `after/` is the
# uncommitted change an agent is about to commit. Then it runs `ripple impact`.
#
# The fixture is a reconstruction from the PR's own description, not the
# original project's code, which is not public.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RIPPLE="$HERE/../../bin/ripple"
WORK="$(mktemp -d "${KIROCREW_SCRATCH:-${TMPDIR:-/tmp}}/ripple-gstack-2141.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

cp -R "$HERE/base/." "$WORK/"
git -C "$WORK" init -q -b main
git -C "$WORK" -c user.name=demo -c user.email=demo@example.invalid add -A
git -C "$WORK" -c user.name=demo -c user.email=demo@example.invalid \
    commit -q -m "base: upload flow"
cp -R "$HERE/after/." "$WORK/"

echo "== the change (what a diff-scoped review sees) =="
git -C "$WORK" --no-pager diff --stat
echo
echo "== ripple impact =="
"$RIPPLE" impact --repo "$WORK" --base main --no-write "$@"
