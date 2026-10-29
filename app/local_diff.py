"""Read a local change set from git: what changed, against what base.

This is the plumbing under `ripple impact`, the command a coding agent runs
before it commits. Everything here is a thin, honest wrapper over `git`:

  * the BASE is resolved, never guessed silently -- the resolved ref is
    returned so the report can say what it compared against;
  * the change set is base vs the WORKING TREE, not base vs HEAD, because an
    agent asks "what does this break?" before committing, and untracked new
    files are part of that change;
  * a failure is a ValueError with the git error attached, never an empty
    change set. An empty diff and a failed `git diff` must not look the same,
    because "nothing changed" is reported to the user as "nothing breaks".
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field

# Files above this size are not read for scanning. A 5 MB source file is a
# vendored bundle or a fixture, and reading it costs more than it can tell us.
MAX_SCAN_BYTES = 1_000_000

# Default base candidates, in order. The first that resolves wins.
BASE_CANDIDATES = ("origin/main", "origin/master", "main", "master")

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class FileChange:
    """One file's change between the base and the working tree."""
    path: str                       # repo-relative, forward slashes
    status: str                     # "modified" | "added" | "deleted"
    added_lines: dict = field(default_factory=dict)    # new line no -> text
    removed_lines: dict = field(default_factory=dict)  # old line no -> text

    @property
    def changed_new_line_numbers(self) -> set:
        return set(self.added_lines)


@dataclass
class ChangeSet:
    repo_root: str
    base_ref: str          # what the user asked for, or the default chosen
    base_sha: str          # what it resolved to
    files: list = field(default_factory=list)   # [FileChange]

    def file(self, path: str):
        for f in self.files:
            if f.path == path:
                return f
        return None


def _git(repo: str, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, errors="replace",
    )
    if check and proc.returncode != 0:
        raise ValueError(
            f"git {' '.join(args)} failed ({proc.returncode}): "
            f"{proc.stderr.strip()[:400]}")
    return proc.stdout


def repo_root(path: str) -> str:
    """The top level of the git repository containing `path`."""
    return _git(path, "rev-parse", "--show-toplevel").strip()


def resolve_base(repo: str, base: str = "") -> tuple:
    """(ref, sha) to compare against.

    With an explicit base, use the MERGE BASE of it and HEAD, so commits that
    landed on main after this branch was cut are not reported as this change.
    Without one, try BASE_CANDIDATES. If none resolves (a fresh repo with one
    branch), compare against HEAD, i.e. report only uncommitted work -- and say
    so through the returned ref rather than pretending a main branch exists.
    """
    candidates = (base,) if base else BASE_CANDIDATES
    tried = []
    for ref in candidates:
        probe = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--verify", "--quiet",
             f"{ref}^{{commit}}"],
            capture_output=True, text=True)
        if probe.returncode != 0:
            tried.append(ref)
            continue
        mb = subprocess.run(["git", "-C", repo, "merge-base", ref, "HEAD"],
                            capture_output=True, text=True)
        sha = (mb.stdout.strip() if mb.returncode == 0
               else probe.stdout.strip())
        return ref, sha
    if base:
        raise ValueError(f"base ref {base!r} does not resolve in {repo}")
    head = _git(repo, "rev-parse", "HEAD").strip()
    return f"HEAD (none of {', '.join(tried)} exist)", head


def parse_unified_diff(text: str) -> list:
    """Parse `git diff --unified=0` output into FileChange records.

    Pure function so it can be tested without a repository.
    """
    files = []
    cur = None
    old_no = new_no = 0
    old = "/dev/null"
    for line in text.splitlines():
        if line.startswith("diff --git "):
            cur = None
            old = "/dev/null"
            continue
        if cur is None and line.startswith("--- "):
            old = line[4:]
            continue
        if cur is None and line.startswith("+++ "):
            new_path = line[4:]
            if new_path == "/dev/null":
                path = old[2:] if old.startswith("a/") else old
                status = "deleted"
            else:
                path = new_path[2:] if new_path.startswith("b/") else new_path
                status = "added" if old == "/dev/null" else "modified"
            cur = FileChange(path=path, status=status)
            files.append(cur)
            continue
        m = _HUNK.match(line)
        if m and cur is not None:
            old_no = int(m.group(1))
            new_no = int(m.group(3))
            continue
        if cur is None:
            continue
        if line.startswith("+"):
            cur.added_lines[new_no] = line[1:]
            new_no += 1
        elif line.startswith("-"):
            cur.removed_lines[old_no] = line[1:]
            old_no += 1
        # "\ No newline at end of file" and anything else: ignored.
    return files


def collect_changes(repo: str, base: str = "") -> ChangeSet:
    """Base (merge-base) vs working tree, including untracked files."""
    root = repo_root(repo)
    ref, sha = resolve_base(root, base)
    diff = _git(root, "diff", "--unified=0", "--no-color", "--no-ext-diff",
                "-M", sha, "--")
    files = parse_unified_diff(diff)

    untracked = _git(root, "ls-files", "--others", "--exclude-standard",
                     "-z").split("\0")
    for path in filter(None, untracked):
        text = read_worktree(root, path)
        if text is None:
            continue
        fc = FileChange(path=path, status="added")
        for i, line in enumerate(text.splitlines(), 1):
            fc.added_lines[i] = line
        files.append(fc)

    return ChangeSet(repo_root=root, base_ref=ref, base_sha=sha, files=files)


def read_worktree(root: str, path: str):
    """Text of a working-tree file, or None when it cannot be scanned.

    None, not "": an unreadable file must not be mistaken for an empty one.
    The callers count these, so a report can say how many files it skipped.
    """
    full = os.path.join(root, path)
    try:
        if os.path.getsize(full) > MAX_SCAN_BYTES:
            return None
        with open(full, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    if b"\0" in raw[:8192]:
        return None          # binary
    return raw.decode("utf-8", errors="replace")


def read_at_base(root: str, sha: str, path: str):
    """Text of `path` at the base commit, or None when it did not exist."""
    proc = subprocess.run(["git", "-C", root, "show", f"{sha}:{path}"],
                          capture_output=True)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", errors="replace")


def tracked_and_untracked_files(root: str) -> list:
    """Every file git would show: tracked plus untracked-not-ignored."""
    out = _git(root, "ls-files", "--cached", "--others", "--exclude-standard",
               "-z")
    return sorted({p for p in out.split("\0") if p})
