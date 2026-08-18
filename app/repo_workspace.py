"""Get a repository's TREE onto disk, safely and with hard limits.

WHY THIS EXISTS -- IT IS THE ONE ARCHITECTURAL BLOCKER
Every fix path fetched consumer files ONE AT A TIME via
`GET /repos/{repo}/contents/{path}`. That single fact is why:

  * validate() can never run in the request path. `tsc`, `mypy` and `go build` need
    a PROJECT -- a tsconfig, a package.json, resolvable imports. A file in isolation
    typechecks nothing, so AUTO is 0 in production for EVERY language, not only the
    ones with no codemod yet.
  * monorepos are impossible. You cannot know that packages/api/src/user.ts belongs
    to packages/api without seeing the tree.
  * there is no accepted-fix corpus to learn from, because no run ever completes
    with a validated fix.

So this is not a feature, it is the removal of a ceiling.

WHY A TARBALL AND NOT `git clone`
The production image has NO git binary -- measured:

    docker run --rm ripple-test:local sh -c 'command -v git'   ->  absent

`python:3.11-slim` plus a copied node toolchain, nothing else. Adding git would grow
the image for a capability the tarball endpoint already provides in ONE authenticated
request, with no .git directory to carry and a stream we can abort mid-download when
it exceeds a cap. `--depth 1` cannot do that: by the time git tells you the size, you
have it.

WHAT AN UNTRUSTED ARCHIVE CAN DO, AND WHAT STOPS IT
This extracts an archive built by whoever owns the repository. That is untrusted
input, and the classic attacks are not theoretical:

    ../../etc/passwd              path traversal
    /etc/passwd                   absolute member path
    a symlink out, then a write   symlink escape
    2 KB -> 40 GB                 decompression bomb
    8 million empty files         inode exhaustion
    device / fifo members         nonsense that should never land on disk

The first four classes are handled by tarfile's `data_filter` (PEP 706). Sizes and
counts are NOT -- a filter cannot know your budget -- so they are enforced here,
DURING extraction, member by member. An archive that blows a cap is abandoned and the
partial tree deleted.

If `data_filter` is unavailable, this REFUSES rather than extracting unfiltered. A
missing safety primitive must not silently degrade to the unsafe path -- that is the
same shape as a validator that treats "could not check" as "fine".

WHAT THE CALLER MUST DO WITH RepoTooLarge
Degrade to REVIEW with the reason stated. NEVER proceed on a partial tree: validation
against half a repository produces confident wrong answers, which is worse than
admitting the repository is out of budget.
"""
from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass


class WorkspaceError(Exception):
    """Base: the tree could not be made available."""


class RepoTooLarge(WorkspaceError):
    """A cap was exceeded. The caller must degrade to REVIEW, not retry."""


class WorkspaceUnavailable(WorkspaceError):
    """Network, auth, or archive failure. Distinct from too-large on purpose --
    "we could not reach it" and "it does not fit" need different responses."""


@dataclass(frozen=True)
class Limits:
    """Budgets, chosen for a small container rather than a workstation.

    Railway containers are memory- and disk-constrained, so these are deliberately
    modest. A repository over budget is analysed rather than fixed, which is a
    product decision stated in the PR body -- not a crash.
    """
    download_bytes: int = 150 * 1024 * 1024        # compressed, aborts the stream
    extracted_bytes: int = 400 * 1024 * 1024       # uncompressed, aborts extraction
    files: int = 40_000
    file_bytes: int = 8 * 1024 * 1024              # one absurd file is a red flag
    timeout_seconds: int = 120


DEFAULT_LIMITS = Limits()

_CHUNK = 64 * 1024


def _tarball_url(repo: str, ref: str) -> str:
    return f"https://api.github.com/repos/{repo}/tarball/{ref}"


def _download(url: str, token: str, limits: Limits, dest: str) -> int:
    """Stream to `dest`, aborting the moment the compressed cap is passed.

    Counted WHILE reading rather than from Content-Length: that header is absent on
    GitHub's redirect to codeload, and trusting a self-reported length from an
    untrusted source would be the wrong way round anyway.
    """
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "ripple",
    })
    total = 0
    try:
        with urllib.request.urlopen(req, timeout=limits.timeout_seconds) as resp, \
                open(dest, "wb") as out:
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > limits.download_bytes:
                    raise RepoTooLarge(
                        f"the archive exceeded {limits.download_bytes // (1024*1024)} "
                        f"MB compressed and the download was abandoned")
                out.write(chunk)
    except urllib.error.HTTPError as exc:
        raise WorkspaceUnavailable(
            f"HTTP {exc.code} fetching the archive -- "
            f"{'the token cannot read this repository' if exc.code in (401, 403, 404) else 'transient'}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise WorkspaceUnavailable(
            f"could not fetch the archive: {type(exc).__name__}: {exc}") from exc
    return total


def _extract(archive: str, into: str, limits: Limits) -> tuple:
    """(files, bytes). Enforces count and size caps DURING extraction."""
    if not hasattr(tarfile, "data_filter"):
        raise WorkspaceUnavailable(
            "this runtime has no tarfile.data_filter, so an untrusted archive "
            "cannot be extracted safely. Refusing rather than extracting "
            "unfiltered")

    files = written = 0
    try:
        with tarfile.open(archive, "r:*") as tar:
            for member in tar:
                # data_filter rejects traversal, absolute paths, symlink escapes and
                # device files. Non-regular members are skipped here as well so the
                # tree contains only things a compiler can read.
                if not member.isreg():
                    continue
                if member.size > limits.file_bytes:
                    raise RepoTooLarge(
                        f"{member.name} is {member.size // (1024*1024)} MB, over the "
                        f"{limits.file_bytes // (1024*1024)} MB per-file cap")
                files += 1
                written += member.size
                if files > limits.files:
                    raise RepoTooLarge(
                        f"the archive holds more than {limits.files} files")
                if written > limits.extracted_bytes:
                    raise RepoTooLarge(
                        f"extraction passed "
                        f"{limits.extracted_bytes // (1024*1024)} MB uncompressed -- "
                        f"a decompression bomb looks exactly like this")
                tar.extract(member, into, filter="data")
    except tarfile.TarError as exc:
        raise WorkspaceUnavailable(f"archive is unreadable: {exc}") from exc
    return files, written


def _single_root(path: str) -> str:
    """GitHub wraps the tree in one `{owner}-{repo}-{sha}` directory. Return it.

    Returning the temp root instead would put every relative path one level off, and
    a tsconfig lookup would silently find nothing -- a failure that looks like "this
    repo has no TypeScript project" rather than a bug here.
    """
    entries = [e for e in os.listdir(path) if not e.startswith(".")]
    if len(entries) == 1 and os.path.isdir(os.path.join(path, entries[0])):
        return os.path.join(path, entries[0])
    return path


def fetch_tree(repo: str, ref: str, token: str,
               limits: Limits = DEFAULT_LIMITS) -> tuple:
    """(tree_path, cleanup_root) with EXPLICIT cleanup. Prefer checkout().

    checkout() is a context manager and is the right shape for a single use. This
    exists for the webhook, where one tree serves EVERY consumer file in a repository
    and a `with` block would mean re-indenting a 150-line loop body -- a large
    mechanical diff over code with governance guards and a ChangeRun scope, for no
    behavioural gain.

    The caller MUST rmtree(cleanup_root) in a finally. Returning the two paths
    separately rather than one is deliberate: the tree is a subdirectory (GitHub wraps
    it in {owner}-{repo}-{sha}), and deleting the tree while leaving the temp root
    would leak the parent on every webhook.
    """
    if not token:
        raise WorkspaceUnavailable("no token, so no archive can be fetched")

    tmp = tempfile.mkdtemp(prefix="ripple-tree-")
    archive = os.path.join(tmp, "repo.tar.gz")
    extracted = os.path.join(tmp, "tree")
    os.makedirs(extracted, exist_ok=True)
    try:
        _download(_tarball_url(repo, ref), token, limits, archive)
        _extract(archive, extracted, limits)
        os.remove(archive)
        return _single_root(extracted), tmp
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


@contextmanager
def checkout(repo: str, ref: str, token: str, limits: Limits = DEFAULT_LIMITS):
    """Yield a path holding the repository tree at `ref`. Always cleaned up.

        with checkout("owner/name", sha, token) as tree:
            ...                      # tree is a real directory a compiler can read

    Raises RepoTooLarge (degrade to REVIEW) or WorkspaceUnavailable (could not fetch).
    """
    if not token:
        raise WorkspaceUnavailable("no token, so no archive can be fetched")

    tmp = tempfile.mkdtemp(prefix="ripple-tree-")
    archive = os.path.join(tmp, "repo.tar.gz")
    extracted = os.path.join(tmp, "tree")
    os.makedirs(extracted, exist_ok=True)
    try:
        _download(_tarball_url(repo, ref), token, limits, archive)
        _extract(archive, extracted, limits)
        os.remove(archive)               # freed before the caller does any work
        yield _single_root(extracted)
    finally:
        # Unconditional. A leaked tree in a small container is a disk leak that
        # surfaces later as an unrelated failure.
        shutil.rmtree(tmp, ignore_errors=True)
