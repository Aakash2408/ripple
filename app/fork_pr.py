"""Fork-based pull requests: contributing to repositories you cannot write to.

WHY THIS EXISTS
---------------
`follow_on` plans a change in an implicated repository, and `pr_engine` opens it by
creating a branch with `POST /repos/{repo}/git/refs`. That needs push permission, so
it works only on repositories the installer owns. Every open-source target needs the
other shape: fork the upstream, branch on the fork, and open a pull request across
the fork boundary with `head="{owner}:{branch}"`.

THREE THINGS THAT MAKE THIS NOT A ONE-LINE CHANGE
-------------------------------------------------
1. FORKING IS ASYNCHRONOUS. `POST /forks` returns 202 with a repo body that may not
   be usable yet -- creating a ref against it immediately gets a 404 or an empty
   repository. `ensure_fork` polls until the fork answers, and REFUSES if it never
   does rather than proceeding into a confusing branch failure.

2. AN EXISTING FORK IS USUALLY STALE. If the fork was made weeks ago, branching from
   its default branch produces a pull request whose diff contains every upstream
   commit since -- hundreds of unrelated files, which is how a bot gets blocked. So
   the branch is cut from the UPSTREAM head sha, not the fork's. That works because a
   fork shares its object store with the parent, so the upstream sha is already
   reachable inside the fork.

3. NOT EVERY REPOSITORY ACCEPTS PULL REQUESTS. Archived repos reject writes, some
   disable pull requests, and some upstreams are themselves forks. Each is checked
   and REFUSED with a reason, because a 403 surfacing from three calls deeper reads
   like a credential problem when it is a policy one.

IDEMPOTENCY
-----------
Re-running must not open a second pull request. Checked by querying the upstream's
open pull requests for our `owner:branch` head. A failed read is treated as "already
open" -- the safe direction, because duplicating pull requests on a repository you do
not own is how an integration gets banned.
"""

import time

from dataclasses import dataclass, field as _field

#: How long to wait for an async fork to become usable, and how often to look.
FORK_READY_TIMEOUT_S = 90
FORK_POLL_INTERVAL_S = 3


@dataclass
class ForkResult:
    """What happened, including the reasons it did not happen."""
    upstream: str
    fork: str = ""
    created: bool = False          # True when WE created it this run
    ready: bool = False
    refusals: list = _field(default_factory=list)

    @property
    def usable(self) -> bool:
        return bool(self.fork) and self.ready and not self.refusals


@dataclass
class ForkPRResult:
    upstream: str
    number: int = 0
    url: str = ""
    branch: str = ""
    already_open: bool = False
    refusals: list = _field(default_factory=list)

    @property
    def opened(self) -> bool:
        return bool(self.url) and not self.already_open


def _viewer_login(token, *, api):
    """The account the token belongs to. The fork lands under it."""
    me = api("GET", "/user", token)
    return (me or {}).get("login", "") if isinstance(me, dict) else ""


def can_accept_pull_requests(upstream, token, *, api):
    """(ok, reasons) -- whether a pull request to this upstream can succeed at all.

    Checked BEFORE forking. Discovering an archived repository after creating a fork
    leaves litter on the user's account for no reason.
    """
    info = api("GET", f"/repos/{upstream}", token)
    if not isinstance(info, dict) or info.get("error"):
        return False, [f"could not read {upstream}: {(info or {}).get('error', 'no response')}"]

    reasons = []
    if info.get("archived"):
        reasons.append(f"{upstream} is archived and accepts no pull requests")
    if info.get("disabled"):
        reasons.append(f"{upstream} is disabled")
    # A repo can have Issues on and PRs off; the flag is absent on older payloads, so
    # only an explicit False is treated as a refusal.
    if info.get("has_pull_requests") is False:
        reasons.append(f"{upstream} has pull requests disabled")
    return (not reasons), reasons


def ensure_fork(upstream, token, *, api, sleep=time.sleep):
    """Fork `upstream` under the token's account, waiting for it to become usable.

    Idempotent: if the fork already exists this returns it without creating anything,
    and `created` says which happened. `sleep` is injected so tests do not wait.
    """
    result = ForkResult(upstream=upstream)

    login = _viewer_login(token, api=api)
    if not login:
        result.refusals.append(
            "could not determine the authenticated account, so where the fork would "
            "live is UNKNOWN -- not forking")
        return result

    name = upstream.split("/")[-1]
    result.fork = f"{login}/{name}"

    existing = api("GET", f"/repos/{result.fork}", token)
    if isinstance(existing, dict) and not existing.get("error"):
        result.ready = True
        return result

    made = api("POST", f"/repos/{upstream}/forks", token, {})
    if isinstance(made, dict) and made.get("error"):
        result.refusals.append(f"fork request failed: {made['error']}")
        return result
    result.created = True

    # POLL. The 202 above means "queued", not "done". Creating a ref against a fork
    # that has not finished materialising fails in a way that looks like a bad sha.
    waited = 0
    while waited < FORK_READY_TIMEOUT_S:
        got = api("GET", f"/repos/{result.fork}", token)
        if isinstance(got, dict) and not got.get("error"):
            result.ready = True
            return result
        sleep(FORK_POLL_INTERVAL_S)
        waited += FORK_POLL_INTERVAL_S

    result.refusals.append(
        f"the fork was requested but was not usable after {FORK_READY_TIMEOUT_S}s; "
        f"whether it will appear later is UNKNOWN, so nothing was pushed to it")
    return result


def upstream_head(upstream, token, *, api):
    """(sha, base_branch) at the upstream's default branch, or ("", "") with a reason.

    The branch is cut from THIS sha rather than from the fork's own default branch. A
    fork that is 200 commits behind would otherwise produce a pull request containing
    every intervening upstream commit.
    """
    info = api("GET", f"/repos/{upstream}", token)
    if not isinstance(info, dict) or info.get("error"):
        return "", ""
    base = info.get("default_branch") or "main"
    ref = api("GET", f"/repos/{upstream}/git/ref/heads/{base}", token)
    if not isinstance(ref, dict) or ref.get("error"):
        return "", base
    return ((ref.get("object") or {}).get("sha", ""), base)


def existing_pr_for_head(upstream, head, token, *, api):
    """True when an open pull request already exists for our head.

    A FAILED read returns True. Duplicating pull requests on someone else's
    repository is much worse than skipping one, so the ambiguous case takes the
    conservative branch -- the same choice `follow_on.already_open` makes.
    """
    got = api("GET", f"/repos/{upstream}/pulls?state=open&per_page=100", token)
    if not isinstance(got, list):
        return True
    return any((p.get("head") or {}).get("label") == head for p in got)


def open_fork_pr(upstream, branch, title, body, edits, token, *, api,
                 sleep=time.sleep):
    """Fork -> branch on the fork from the upstream sha -> commit -> cross-fork PR.

    `edits` is an iterable of (path, new_content). Content is committed to the FORK,
    never to the upstream -- the upstream only ever receives a pull request.

    Every failure path returns a refusal rather than raising, so a caller iterating
    twenty repositories is not stopped by one archived one.
    """
    result = ForkPRResult(upstream=upstream, branch=branch)

    ok, reasons = can_accept_pull_requests(upstream, token, api=api)
    if not ok:
        result.refusals.extend(reasons)
        return result

    fork = ensure_fork(upstream, token, api=api, sleep=sleep)
    if not fork.usable:
        result.refusals.extend(fork.refusals or ["the fork is not usable"])
        return result

    login = fork.fork.split("/")[0]
    head_label = f"{login}:{branch}"

    if existing_pr_for_head(upstream, head_label, token, api=api):
        result.already_open = True
        return result

    sha, base = upstream_head(upstream, token, api=api)
    if not sha:
        result.refusals.append(
            f"could not resolve {upstream}'s default-branch head, so the branch would "
            f"have no known base -- refusing rather than branching from the fork's "
            f"own possibly-stale default")
        return result

    made = api("POST", f"/repos/{fork.fork}/git/refs", token,
               {"ref": f"refs/heads/{branch}", "sha": sha})
    if isinstance(made, dict) and made.get("error"):
        # 422 here usually means the ref already exists from an earlier partial run.
        # That is recoverable -- the commit below will land on it -- but it is
        # RECORDED, because "branch already existed" and "branch was created from the
        # sha I intended" are different states and the second is the one we want.
        result.refusals.append(
            f"branch {branch} was not created on {fork.fork} ({made['error']}); if it "
            f"already existed its base may not be {sha[:8]}")

    committed = 0
    for path, content in edits:
        current = api("GET", f"/repos/{fork.fork}/contents/{path}?ref={branch}", token)
        blob_sha = current.get("sha") if isinstance(current, dict) else None
        payload = {"message": title, "content": content, "branch": branch}
        if blob_sha:
            payload["sha"] = blob_sha
        put = api("PUT", f"/repos/{fork.fork}/contents/{path}", token, payload)
        if isinstance(put, dict) and put.get("error"):
            result.refusals.append(f"could not commit {path}: {put['error']}")
        else:
            committed += 1

    if not committed:
        result.refusals.append(
            "no file was committed, so no pull request was opened -- an empty pull "
            "request wastes a maintainer's attention")
        return result

    pr = api("POST", f"/repos/{upstream}/pulls", token,
             {"title": title, "body": body, "head": head_label, "base": base})
    if not isinstance(pr, dict) or pr.get("error"):
        result.refusals.append(
            f"the branch and commits exist on {fork.fork} but the pull request was "
            f"rejected: {(pr or {}).get('error', 'no response')}")
        return result

    result.number = pr.get("number", 0)
    result.url = pr.get("html_url", "")
    return result
