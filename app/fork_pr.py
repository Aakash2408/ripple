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

import subprocess
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


#: Remote URL forms git accepts, mapped to owner/repo.
_REMOTE_FORMS = (
    "https://github.com/",
    "http://github.com/",
    "git://github.com/",
    "ssh://git@github.com/",
    "git@github.com:",
)


def _git(args, cwd):
    """Run one read-only git command, returning stdout or "" on any failure.

    Read-only by construction: every caller passes a query verb. Failure is returned
    as "" and every caller converts that into a STATED refusal rather than proceeding,
    so nothing here returns a value a caller could mistake for success.
    """
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                              text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def upstream_from_git_remote(path):
    """(owner/repo, reason) for the GitHub repository a local file belongs to.

    WHY THIS LIVES HERE AND WHY IT IS NOT AN ENV VAR
    `pr_engine._infer_repo` is a placeholder that reads RIPPLE_TARGET_REPO and returns
    None otherwise, with the comment "in production, the GitHub App webhook provides
    the repo context directly". That is true for the webhook and false for the CLI:
    the CLI is given LOCAL DIRECTORIES with `--repos`, and ConsumerMatch carries only
    a file_path, so there is no repository attribution anywhere in the CLI path.

    One env var cannot supply it either, because a single run can span several
    consumer directories that are different repositories -- and a wrong answer here
    does not fail loudly, it opens a pull request against SOMEBODY ELSE'S repository.
    So the coordinates are read from the clone itself, which is evidence rather than
    configuration, and a directory that is not a clone is refused with the reason.
    """
    if not path:
        return "", "no path given"
    url = _git(["config", "--get", "remote.origin.url"], cwd=path)
    if not url:
        top = _git(["rev-parse", "--show-toplevel"], cwd=path)
        if not top:
            return "", f"{path} is not inside a git clone, so its GitHub repository is unknown"
        return "", f"{top} has no `origin` remote, so its GitHub repository is unknown"
    for prefix in _REMOTE_FORMS:
        if url.startswith(prefix):
            slug = url[len(prefix):]
            break
    else:
        return "", f"origin remote {url!r} is not a github.com URL"
    if slug.endswith(".git"):
        slug = slug[:-4]
    slug = slug.strip("/")
    if slug.count("/") != 1 or not all(slug.split("/")):
        return "", f"origin remote {url!r} does not resolve to owner/repo"
    return slug, ""


def repo_relative_path(path):
    """(path relative to the clone root, reason).

    The GitHub contents API needs a repo-relative path. `pr_engine._relative_file_path`
    guesses one by scanning for a path component named src, lib or app and falling back
    to the BASENAME -- which silently writes to the wrong location for any project not
    laid out that way, and for the locked target would turn
    `do_mpc/sampling/_samplingplanner.py` into `_samplingplanner.py`, committing a new
    top-level file instead of editing the real one. git already knows the answer.
    """
    if not path:
        return "", "no path given"
    import os as _os
    directory = path if _os.path.isdir(path) else _os.path.dirname(path) or "."
    top = _git(["rev-parse", "--show-toplevel"], cwd=directory)
    if not top:
        return "", f"{path} is not inside a git clone, so its repo-relative path is unknown"
    # realpath, NOT abspath, on BOTH sides. abspath does not resolve symlinks, so on a
    # host where /home is a link to /local/home the caller's path and git's answer are
    # two spellings of the same directory and the containment check below fails --
    # rejecting a file that is plainly inside the clone. Caught by running this against
    # a real clone; the unit test's fake `git` could never see it, because a fake
    # returns whatever spelling the test already used.
    abs_path = _os.path.realpath(path)
    top_abs = _os.path.realpath(top)
    if not abs_path.startswith(top_abs + _os.sep):
        return "", f"{path} is outside the clone root {top}"
    return _os.path.relpath(abs_path, top_abs).replace(_os.sep, "/"), ""


@dataclass
class ForkPRRun:
    """What a multi-repository fork-PR pass did. Every outcome is recorded.

    `refused` is kept apart from `failed` because they need different responses: a
    refusal is a decision this code made and states (no repository coordinates, an
    archived upstream, a dry run), while a failure is the platform saying no.
    """
    opened: list = _field(default_factory=list)
    already_open: list = _field(default_factory=list)
    failed: list = _field(default_factory=list)
    refused: list = _field(default_factory=list)

    def summary(self) -> str:
        return (f"{len(self.opened)} opened, {len(self.already_open)} already open, "
                f"{len(self.failed)} failed, {len(self.refused)} refused")


def open_fork_prs(groups, *, branch, title, body, token, api,
                  dry_run=False, sleep=time.sleep):
    """Open one fork-based pull request per upstream repository.

    `groups` is {upstream: [(repo_relative_path, base64_content), ...]}, so this
    function knows nothing about how a fix was produced -- keeping this module free of
    any dependency on the app's types, which is what lets the CLI and the webhook both
    reach it without either shape leaking into the other.

    DRY RUN ISSUES NO REQUEST AT ALL. Not "issues requests and discards the result":
    `ensure_fork` CREATES a fork, so a dry run that called through would leave a real
    repository on the operator's account and, worse, would look harmless in the log.
    """
    run = ForkPRRun()
    for upstream, edits in sorted(groups.items()):
        if not edits:
            run.refused.append((upstream, "no edits, so there is nothing to propose"))
            continue
        if dry_run:
            run.refused.append((
                upstream,
                f"dry run: would open `{branch}` on {upstream} with "
                f"{len(edits)} file(s): {', '.join(p for p, _ in edits)}"))
            continue
        result = open_fork_pr(upstream=upstream, branch=branch, title=title,
                              body=body, edits=edits, token=token, api=api,
                              sleep=sleep)
        if result.opened:
            run.opened.append((upstream, result.number, result.url))
        elif result.already_open:
            run.already_open.append((upstream, result.branch))
        else:
            run.failed.append((upstream, result.refusals))
    return run


def error_returning_api(raw_api):
    """Adapt a RAISING GitHub client to the error-returning contract this module needs.

    THE MISMATCH THIS EXISTS TO FIX, found by actually running the path rather than by
    any gate. Every function here checks `result.get("error")` and converts it into a
    stated refusal -- that is what makes "one archived repository does not stop a run
    over twenty" true. But `pr_engine._github_request` RAISES on any non-2xx, so
    handing it in directly turns the first refusal into an unhandled exception and
    aborts the whole run. The CLI wiring did exactly that.

    Wrapping at the boundary rather than changing `_github_request` is deliberate: that
    function is the same-repo path's client and its callers there expect it to raise.
    Two contracts, one adapter, instead of changing a contract two call sites already
    depend on.
    """
    def _api(method, path, token, data=None, **kw):
        try:
            return raw_api(method, path, token, data, **kw)
        except Exception as exc:                                    # noqa: BLE001
            # The message carries the status and body, which is what the refusal text
            # needs. Losing the type is fine; losing the status would not be.
            return {"error": str(exc)[:300]}
    return _api


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
