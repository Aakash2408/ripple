"""Inline review comments on the pull request that caused the break.

WHERE A COMMENT CAN PHYSICALLY GO
---------------------------------
GitHub accepts an inline review comment only on a line that is part of THIS pull
request's diff. The files this analysis implicates are in OTHER repositories, so
there is no line in this diff to attach them to.

That rules out the obvious design. The comment is anchored on the line where the
symbol was REMOVED -- which is in the diff, on the base side -- and its body names
the cross-repo consumers. Anchoring to another repo's file is not something GitHub
can do, and faking it by guessing a line in this diff would land the comment on
unrelated code.

`side="LEFT"` for the same reason: a removed line exists only in the base revision.
Asking for it on the RIGHT side is a 422.

THE LEDGER IS TEMPLATE-WRITTEN. THE NARRATIVE IS NOT.
-----------------------------------------------------
Every file path, repo name and confidence label in the body comes from the
deterministic index via `impact.ImpactResult`. The model may contribute one
narrative paragraph, held to `pr_prose.check`. A model that cannot be trusted to
enumerate files is not asked to.

IDEMPOTENCY IS A HIDDEN MARKER, NOT A HEURISTIC
-----------------------------------------------
`synchronize` fires on every push to a pull request, so the same symbol will be
analysed repeatedly. Each comment carries an invisible HTML marker naming the file
and symbol it is about; a re-run PATCHes the comment carrying that marker instead of
posting a second one. Matching on body text would break the moment the narrative
changed, which is exactly when a re-run happens.
"""

import re

from dataclasses import dataclass, field as _field

#: Comments posted per pull request. A refactor removing 200 symbols would otherwise
#: bury the review under its own output. The cap is STATED in the summary when hit.
MAX_COMMENTS_PER_PR = 20

#: Consumers listed per comment before the list is summarised.
MAX_LISTED_CONSUMERS = 10

_MARKER = re.compile(
    r"<!--\s*ripple:removed=(?P<sym>\S+)\s+path=(?P<path>\S+)\s*-->")


def marker_for(symbol: str, path: str) -> str:
    """The invisible identity of a comment. Stable across re-runs by construction."""
    return f"<!-- ripple:removed={symbol} path={path} -->"


def marker_in(body: str):
    """(symbol, path) if this body is a Ripple comment, else None."""
    m = _MARKER.search(body or "")
    return (m.group("sym"), m.group("path")) if m else None


@dataclass(frozen=True)
class ReviewComment:
    """One comment, and whether it could be anchored to a line.

    `line == 0` means NO ANCHOR was available -- the base line of the removed symbol
    could not be established. Such a comment is posted at pull-request level rather
    than with an invented line number, because a guessed line is either rejected by
    GitHub or lands on unrelated code and looks authoritative doing it.
    """
    path: str
    line: int
    body: str
    symbol: str
    side: str = "LEFT"

    @property
    def anchored(self) -> bool:
        return self.line > 0

    def to_github(self) -> dict:
        return {"path": self.path, "line": self.line, "side": self.side,
                "body": self.body}


@dataclass
class ReviewPlan:
    """What would be posted, separately from posting it.

    Built and asserted on without a token or a network, so the body and the anchors
    are testable. `post` consumes a plan; it never builds one.
    """
    anchored: list = _field(default_factory=list)
    unanchored: list = _field(default_factory=list)
    truncated_at: int = 0        # >0 when the comment cap was hit
    skipped: list = _field(default_factory=list)   # [{symbol, why}]

    @property
    def all_comments(self) -> list:
        return list(self.anchored) + list(self.unanchored)

    def summary(self) -> str:
        parts = [f"{len(self.anchored)} anchored",
                 f"{len(self.unanchored)} unanchored"]
        if self.truncated_at:
            parts.append(f"capped at {self.truncated_at}")
        if self.skipped:
            parts.append(f"{len(self.skipped)} skipped")
        return ", ".join(parts)


def _consumer_lines(affected) -> list:
    """The ledger rows. Deterministic, from the index, one per affected file."""
    rows = []
    for c in affected[:MAX_LISTED_CONSUMERS]:
        note = f" -- {c['note']}" if c.get("note") else ""
        rows.append(f"- `{c['repo']}` · `{c['path']}` ({c.get('language', '?')}, "
                    f"{c.get('confidence', '?')} match){note}")
    extra = len(affected) - MAX_LISTED_CONSUMERS
    if extra > 0:
        rows.append(f"- ...and {extra} more, not listed here")
    return rows


def body_for(symbol, origin_path, result, *, narrative="") -> str:
    """The comment body. Every fact in it comes from `result`, not from a model.

    `result` is the impact DICT that already crosses the outcome funnel -- the same
    representation, not a parallel one. A second shape here would be a second thing
    to keep in step with `impact.ImpactResult.to_dict`.

    The structure is deliberate: what changed, what it reaches, what is NOT known.
    The third section is the one that matters -- a comment that lists two consumers
    and stays silent about eight unindexed repos reads as a complete answer.
    """
    affected = result.get("affected") or []
    lines = [f"**`{symbol}` is removed here, and other repositories reference it.**",
             ""]

    if affected:
        lines.append("Referenced in:")
        lines.extend(_consumer_lines(affected))
    else:
        lines.append("No indexed file in any other repository references it.")
    lines.append("")

    # WHAT IS NOT KNOWN. Never omitted when it applies, and never softened.
    caveats = []
    if result.get("ambiguity"):
        caveats.append(f"{result['ambiguity']}, so treat this list as a starting "
                       f"point rather than an inventory")
    for u in result.get("unsearchable") or []:
        caveats.append(f"`{u['repo']}` has no symbol index ({u['reason']}), so whether "
                       f"it references `{symbol}` is unknown")
    caveats.extend(result.get("refusals") or [])

    if caveats:
        lines.append("**Not established:**")
        lines.extend(f"- {c}" for c in caveats)
        lines.append("")
    elif not affected:
        # A clean, complete search is a FACT and is allowed to say so plainly. Adding
        # a hedge here would make the honest case indistinguishable from the unknown
        # one, which defeats the point of tracking completeness at all.
        lines.append("Every granted repository was indexed and searched.")
        lines.append("")

    if narrative:
        lines.extend([narrative, ""])

    if result.get("violations"):
        # Surfaced, not hidden. If the model tried to invent a file, the reviewer is
        # told -- burying it would make the ranking look cleaner than it was.
        lines.append("<details><summary>Ranking notes</summary>")
        lines.append("")
        lines.extend(f"- {v}" for v in result["violations"])
        lines.extend(["", "</details>", ""])

    lines.append(marker_for(symbol, origin_path))
    return "\n".join(lines)


def plan(runs, *, narratives=None) -> ReviewPlan:
    """Turn analysed runs into the comments that would be posted.

    `runs` is the list `_handle_pr_opened` builds: each entry has `symbol`, `path`,
    the impact dict, and -- when available -- the base line via `removed_at`.
    """
    out = ReviewPlan()
    narratives = narratives or {}

    for run in runs:
        symbol = run.get("symbol", "")
        path = run.get("path", "")
        if "affected" not in run:
            # The impact keys are flattened into the run by the PR handler. Their
            # absence means the analysis never ran for this symbol, which must not
            # render as a comment saying nothing is affected.
            out.skipped.append({"symbol": symbol,
                                "why": "no impact analysis was attached to this run"})
            continue
        result = run

        if len(out.all_comments) >= MAX_COMMENTS_PER_PR:
            out.truncated_at = MAX_COMMENTS_PER_PR
            out.skipped.append({"symbol": symbol,
                                "why": f"more than {MAX_COMMENTS_PER_PR} removed "
                                       f"symbols; this one has no comment"})
            continue

        line = int(run.get("line") or 0)
        comment = ReviewComment(
            path=path, line=line, symbol=symbol,
            body=body_for(symbol, path, result,
                          narrative=narratives.get(symbol, "")))
        (out.anchored if comment.anchored else out.unanchored).append(comment)

    return out


def existing_markers(repo, number, token, *, api) -> dict:
    """(symbol, path) -> comment id, for comments Ripple already posted.

    Reads BOTH surfaces, because an unanchored comment lands on the issue thread
    while an anchored one lands on the review thread. Checking only one would make
    every re-run duplicate the other kind.
    """
    found = {}
    for kind, path in (("review", f"/repos/{repo}/pulls/{number}/comments"),
                       ("issue", f"/repos/{repo}/issues/{number}/comments")):
        got = api("GET", f"{path}?per_page=100", token)
        if not isinstance(got, list):
            # Not a list means the read failed. Recording the failure matters: an
            # empty dict would be indistinguishable from "no comments yet", and the
            # re-run would duplicate every comment instead of updating it.
            found.setdefault("_read_failed", []).append(kind)
            continue
        for c in got:
            key = marker_in(c.get("body", ""))
            if key:
                found[key] = {"id": c.get("id"), "kind": kind}
    return found


def post(repo, number, commit_id, review_plan, token, *, api) -> dict:
    """Post or update. Never posts a duplicate, never posts a guessed anchor.

    Returns what happened per comment. On a read failure it REFUSES rather than
    posting: duplicating a bot's review comments on every push is the failure mode
    that gets an app uninstalled.
    """
    result = {"created": [], "updated": [], "refused": [], "errors": []}

    if not review_plan.all_comments:
        result["refused"].append("nothing to post: the plan is empty")
        return result

    known = existing_markers(repo, number, token, api=api)
    if "_read_failed" in known:
        result["refused"].append(
            f"could not read existing comments ({', '.join(known.pop('_read_failed'))}"
            f"), so posting was skipped rather than risk duplicating every comment "
            f"on this pull request")
        return result

    fresh = []
    for c in review_plan.all_comments:
        key = (c.symbol, c.path)
        if key in known:
            target, kind = known[key]["id"], known[key]["kind"]
            path = (f"/repos/{repo}/pulls/comments/{target}" if kind == "review"
                    else f"/repos/{repo}/issues/comments/{target}")
            got = api("PATCH", path, token, {"body": c.body})
            if isinstance(got, dict) and got.get("error"):
                result["errors"].append({"symbol": c.symbol, "error": got["error"]})
            else:
                result["updated"].append(c.symbol)
        else:
            fresh.append(c)

    anchored = [c for c in fresh if c.anchored]
    unanchored = [c for c in fresh if not c.anchored]

    if anchored:
        if not commit_id:
            # The review API requires the head sha. Without it the anchors cannot be
            # attached, so these are demoted to pull-request level rather than posted
            # against an unknown revision.
            unanchored.extend(anchored)
            result["refused"].append(
                "no head sha was available, so anchored comments were posted at "
                "pull-request level instead of on their lines")
        else:
            got = api("POST", f"/repos/{repo}/pulls/{number}/reviews", token, {
                "commit_id": commit_id,
                "event": "COMMENT",
                "comments": [c.to_github() for c in anchored],
            })
            if isinstance(got, dict) and got.get("error"):
                result["errors"].append({"review": got["error"],
                                         "symbols": [c.symbol for c in anchored]})
            else:
                result["created"].extend(c.symbol for c in anchored)

    for c in unanchored:
        got = api("POST", f"/repos/{repo}/issues/{number}/comments", token,
                  {"body": c.body})
        if isinstance(got, dict) and got.get("error"):
            result["errors"].append({"symbol": c.symbol, "error": got["error"]})
        else:
            result["created"].append(c.symbol)

    return result
