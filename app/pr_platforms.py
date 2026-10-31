"""GitLab and Bitbucket merge/close events, shaped for the shared outcome handler.

WHY THIS EXISTS
---------------
`webhook._record_pr_terminal` decides what a closed Ripple PR teaches: clean
merge, merge with reviewer edits, or rejection, and then feeds learned_edits
and the pattern store. It was written against GitHub's payload and API, so a
GitLab MR or Bitbucket PR that Ripple opened could merge or be declined and
Ripple learned nothing from it.

Each class here answers the same five questions the handler asks of GitHub,
and nothing else:

    pr_dict()        the payload fields contamination and human-edit checks read
                     (html_url, number, commits, changed_files, head/base sha)
    commit_authors() who wrote each commit, with Ripple's own commits named
                     "ripple"; None when it cannot be known
    merge_base()     the commit Ripple's branch started from
    fetch(path, sha) a file at a commit, or None when it could not be read
    repo             "owner/name" (GitLab: the full namespace path)

FAIL CLOSED, the same direction as the GitHub path. A count that might be
truncated (a full first page) is None, which reads as contaminated. An author
list without a known Ripple identity is None, which reads as human-edited.
An unreadable file is None, never "", because an empty "after" would teach
"delete every reference".

KNOWN LIMIT. GitHub commits come from the ripple-api[bot] App identity. GitLab
and Bitbucket commits are made with a user token, so "Ripple's commit" means
"authored by the token's user". If that same person also edits the MR by hand,
the edit is indistinguishable from Ripple's and the merge reads as clean.
"""
from __future__ import annotations

import base64
from typing import Optional
from urllib.parse import quote

RIPPLE = "ripple"
_PAGE = 100


def _int_or_none(v) -> Optional[int]:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.isdigit():
        return int(v)
    return None   # GitLab reports "1000+" past its cap: unknown, not 1000


# ---------------------------------------------------------------- GitLab
class GitLabMR:
    """A GitLab merge request event (`Merge Request Hook`, action merge/close)."""

    platform = "gitlab"

    def __init__(self, payload: dict, client):
        attrs = payload.get("object_attributes") or {}
        project = payload.get("project") or {}
        self.client = client
        self.project_id = attrs.get("target_project_id") or project.get("id")
        self.iid = attrs.get("iid")
        self.url = attrs.get("url") or ""
        self.repo = project.get("path_with_namespace") or ""
        self.merged = attrs.get("action") == "merge" or attrs.get("state") == "merged"
        self._mr = None
        self._commits = None

    def _get(self, path: str):
        try:
            data = self.client._request("GET", path)
        except Exception:                                       # noqa: BLE001
            return None
        if isinstance(data, dict) and "error" in data:
            return None
        return data

    def _detail(self) -> dict:
        if self._mr is None:
            self._mr = self._get(
                f"/projects/{self.project_id}/merge_requests/{self.iid}") or {}
        return self._mr

    def _refs(self) -> dict:
        return self._detail().get("diff_refs") or {}

    def pr_dict(self) -> dict:
        commits = self._get(f"/projects/{self.project_id}/merge_requests/"
                            f"{self.iid}/commits?per_page={_PAGE}")
        n_commits = (len(commits) if isinstance(commits, list)
                     and len(commits) < _PAGE else None)
        self._commits = commits if n_commits is not None else None
        refs = self._refs()
        return {"html_url": self.url, "url": self.url, "number": self.iid,
                "commits": n_commits,
                "changed_files": _int_or_none(self._detail().get("changes_count")),
                "head": {"sha": refs.get("head_sha") or ""},
                "base": {"sha": refs.get("base_sha") or ""}}

    def commit_authors(self) -> Optional[list]:
        commits = self._commits
        if commits is None:
            return None
        me = self._get("/user")
        if not isinstance(me, dict):
            return None
        names = {x for x in (me.get("name"), me.get("username")) if x}
        emails = {x.lower() for x in (me.get("email"), me.get("public_email"),
                                      me.get("commit_email")) if x}
        if not names and not emails:
            return None
        out = []
        for c in commits:
            email = (c.get("author_email") or "").lower()
            name = c.get("author_name") or ""
            out.append(RIPPLE if (email in emails or name in names) else (name or email))
        return out

    def merge_base(self) -> str:
        # GitLab's diff_refs.base_sha IS the merge base of source and target.
        return self._refs().get("base_sha") or ""

    def head_sha(self) -> str:
        return self._refs().get("head_sha") or ""

    def fetch(self, path: str, sha: str) -> Optional[str]:
        data = self._get(f"/projects/{self.project_id}/repository/files/"
                         f"{quote(path, safe='')}?ref={sha}")
        if not isinstance(data, dict) or "content" not in data:
            return None
        try:
            return base64.b64decode(data["content"]).decode()
        except Exception:                                       # noqa: BLE001
            return None


# ---------------------------------------------------------------- Bitbucket
class BitbucketPR:
    """A Bitbucket pull request event (pullrequest:fulfilled / :rejected)."""

    platform = "bitbucket"

    def __init__(self, payload: dict, client, merged: bool):
        pr = payload.get("pullrequest") or {}
        self.client = client
        self.pr = pr
        self.id = pr.get("id")
        self.url = ((pr.get("links") or {}).get("html") or {}).get("href", "")
        dest_repo = ((pr.get("destination") or {}).get("repository") or {})
        self.repo = (dest_repo.get("full_name")
                     or (payload.get("repository") or {}).get("full_name") or "")
        self.merged = merged
        self._commits = None

    def _get(self, path: str):
        try:
            data = self.client._request("GET", path)
        except Exception:                                       # noqa: BLE001
            return None
        if isinstance(data, dict) and "error" in data:
            return None
        return data

    def _sha(self, side: str) -> str:
        return (((self.pr.get(side) or {}).get("commit") or {}).get("hash") or "")

    def _count(self, path: str):
        data = self._get(path)
        if not isinstance(data, dict) or data.get("next"):
            return None, None   # error, or more than one page: unknown
        values = data.get("values")
        if not isinstance(values, list):
            return None, None
        return len(values), values

    def pr_dict(self) -> dict:
        base = f"/repositories/{self.repo}/pullrequests/{self.id}"
        n_commits, self._commits = self._count(f"{base}/commits?pagelen={_PAGE}")
        n_files, _ = self._count(f"{base}/diffstat?pagelen={_PAGE}")
        return {"html_url": self.url, "url": self.url, "number": self.id,
                "commits": n_commits, "changed_files": n_files,
                "head": {"sha": self._sha("source")},
                "base": {"sha": self._sha("destination")}}

    def commit_authors(self) -> Optional[list]:
        if self._commits is None:
            return None
        me = self._get("/user")
        my_id = me.get("account_id") if isinstance(me, dict) else None
        if not my_id:
            return None
        out = []
        for c in self._commits:
            author = c.get("author") or {}
            user = author.get("user") or {}
            if user.get("account_id") == my_id:
                out.append(RIPPLE)
            else:
                out.append(user.get("nickname") or author.get("raw") or "")
        return out

    def merge_base(self) -> str:
        head, dest = self._sha("source"), self._sha("destination")
        data = self._get(f"/repositories/{self.repo}/merge-base/{head}..{dest}")
        if isinstance(data, dict) and data.get("hash"):
            return data["hash"]
        return dest

    def head_sha(self) -> str:
        return self._sha("source")

    def fetch(self, path: str, sha: str) -> Optional[str]:
        ws, _, slug = self.repo.partition("/")
        try:
            text = self.client.get_file_at_commit(ws, slug, path, sha)
        except Exception:                                       # noqa: BLE001
            return None
        # get_file returns "" only for a 404: the file is not there, which
        # for learning is "could not read the version", not "it is empty".
        return text or None
