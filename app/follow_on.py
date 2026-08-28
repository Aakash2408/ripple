"""Follow-on pull requests in the repositories a removal implicates.

WHAT GATES A FOLLOW-ON PR
------------------------
Stage 5 posts a comment naming affected files. This opens a change in those repos --
a much larger commitment, on code the author of the originating pull request does not
own. So the bar is higher than for a comment, and two rules do the gating:

  1. AN APPROXIMATE MATCH NEVER BECOMES A COMMIT. A regex hit is good enough to
     mention in a comment ("this file may need a look") and not good enough to edit.
     `confidence == "approximate"` is planned as a MENTION, never as a PR.
  2. A PARTIAL SEARCH STILL RAISES WHAT IT FOUND, but says the set is incomplete.
     Withholding a real consumer because a different repo was unindexed helps nobody.

ONE PR PER REPO, NOT ONE PER FILE
---------------------------------
Three files in one repo referencing the same removed symbol is one change, not three
reviews. Grouping by repo is also what makes idempotency tractable: the branch name
is derived from the symbol, so a re-run finds its own branch instead of opening a
second PR.

WHY THIS PLANS RATHER THAN POSTS
--------------------------------
`plan_follow_ons` is pure. The actual branch/commit/PR calls live in pr_engine, which
already owns them -- a second implementation would be a second place to get the base
sha or the default branch wrong.
"""

import re

from dataclasses import dataclass, field as _field

#: Follow-on PRs opened per originating pull request. Beyond this the blast radius is
#: large enough that a human should decide, and the cap is stated.
MAX_FOLLOW_ON_REPOS = 5


def branch_name(symbol: str, origin_repo: str, number) -> str:
    """Deterministic, so a re-run finds its own branch instead of opening a rival PR."""
    slug = re.sub(r"[^a-z0-9]+", "-", f"{symbol}".lower()).strip("-") or "symbol"
    owner = (origin_repo or "upstream").split("/")[-1]
    return f"ripple/{owner}-{number}-drop-{slug}"[:60]


@dataclass(frozen=True)
class FollowOn:
    """One repository's worth of change, and why it is safe to propose."""
    repo: str
    symbol: str
    files: tuple
    branch: str
    title: str
    body: str

    @property
    def exact_only(self) -> bool:
        return all(f.get("confidence") == "exact" for f in self.files)


@dataclass
class FollowOnPlan:
    proposals: list = _field(default_factory=list)
    mentioned_only: list = _field(default_factory=list)   # [{repo, path, why}]
    truncated_at: int = 0

    def summary(self) -> str:
        parts = [f"{len(self.proposals)} follow-on PR(s)"]
        if self.mentioned_only:
            parts.append(f"{len(self.mentioned_only)} mentioned but not edited")
        if self.truncated_at:
            parts.append(f"capped at {self.truncated_at} repos")
        return ", ".join(parts)


def _body(symbol, origin_repo, number, files, incomplete) -> str:
    rows = "\n".join(f"- `{f['path']}` ({f.get('language', '?')}, exact match)"
                     for f in files)
    lines = [
        f"`{symbol}` was removed upstream in {origin_repo}#{number}, and this "
        f"repository references it.",
        "",
        "Files this touches:",
        rows,
        "",
        "Each of these was located by parsing the file, not by pattern matching -- "
        "an approximate match is reported upstream but never edited here.",
    ]
    if incomplete:
        lines += ["", "**The upstream search was incomplete**, so this may not be "
                      "every file in this repository that is affected: " + incomplete]
    lines += ["", f"Opened in response to {origin_repo}#{number}."]
    return "\n".join(lines)


def plan_follow_ons(symbol, origin_repo, number, impact) -> FollowOnPlan:
    """Group affected files into one proposal per repository.

    `impact` is the same flattened dict the PR handler and the comment builder use.
    """
    out = FollowOnPlan()
    affected = impact.get("affected") or []

    by_repo = {}
    for hit in affected:
        if hit.get("repo") == origin_repo:
            # The originating repo fixes itself in its own pull request. Opening a
            # second PR against the same repo would race the one under review.
            out.mentioned_only.append({
                "repo": hit["repo"], "path": hit["path"],
                "why": "same repository as the originating pull request"})
            continue
        if hit.get("confidence") != "exact":
            out.mentioned_only.append({
                "repo": hit["repo"], "path": hit["path"],
                "why": "approximate match -- good enough to report, not to edit"})
            continue
        by_repo.setdefault(hit["repo"], []).append(hit)

    incomplete = ""
    if impact.get("partial") or impact.get("unsearchable"):
        bits = []
        if impact.get("partial"):
            bits.append("some indexes were truncated")
        if impact.get("unsearchable"):
            bits.append(f"{len(impact['unsearchable'])} repo(s) were never indexed")
        incomplete = " and ".join(bits)

    for repo in sorted(by_repo):
        if len(out.proposals) >= MAX_FOLLOW_ON_REPOS:
            out.truncated_at = MAX_FOLLOW_ON_REPOS
            out.mentioned_only.append({
                "repo": repo, "path": "",
                "why": f"more than {MAX_FOLLOW_ON_REPOS} repositories affected; "
                       f"this one was reported but no pull request was opened"})
            continue
        files = by_repo[repo]
        out.proposals.append(FollowOn(
            repo=repo, symbol=symbol, files=tuple(files),
            branch=branch_name(symbol, origin_repo, number),
            title=f"fix: drop references to `{symbol}` removed in "
                  f"{origin_repo}#{number}",
            body=_body(symbol, origin_repo, number, files, incomplete)))

    return out


def already_open(proposal, token, *, api) -> bool:
    """True when Ripple's own branch already has an open pull request here.

    Checked by BRANCH, which is derived from the symbol, so this is stable across
    re-runs. `synchronize` fires on every push to the originating pull request, and
    without this each push would open another PR in every implicated repository --
    the fastest way to get an app banned.
    """
    got = api("GET", f"/repos/{proposal.repo}/pulls?head_branch={proposal.branch}"
                     f"&state=open&per_page=100", token)
    if not isinstance(got, list):
        # A failed read is not evidence that nothing is open. Reported as "already
        # open" so the caller does NOT open a duplicate -- the safe direction when
        # the alternative is spamming someone else's repository.
        return True
    return any((p.get("head") or {}).get("ref") == proposal.branch for p in got)
