"""
ripple/app/poll_endpoint.py

Polling endpoint for autonomous agents and cron jobs.

POST /poll  — check one or more repos for spec changes since last poll.
GET  /poll/status — list all watched repos and their last-known state.
DELETE /poll/{owner}/{repo} — stop watching a repo.

WHY THIS EXISTS
The webhook path requires a GitHub App installation — the webhook fires when
GitHub pushes an event. That works for repos where the App is installed, but
an autonomous agent (Automaton, cron, GitHub Action) needs to PULL: "has
anything changed since I last looked?" This endpoint answers that question.

The contract:
  POST /poll with {"repos": ["owner/repo", ...]}
  → for each repo, compare HEAD to stored SHA
  → if different, find changed spec files, run diff, return breaking changes
  → optionally execute follow-on PRs (if RIPPLE_EXECUTE_FOLLOW_ONS=1)

State is persisted in SQLite via poll_store.py, so the agent does not need
to remember what it already checked.
"""

from __future__ import annotations

import base64
import json
import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import poll_store
from .diff_engine import diff_specs as _diff_specs, BreakingChange
from .proto_diff import diff_proto as _diff_proto
from .graphql_diff import diff_graphql as _diff_graphql

router = APIRouter(tags=["poll"])


# ─── Request / Response models ───────────────────────────────────────

class PollRequest(BaseModel):
    repos: list[str] = Field(
        ...,
        description="List of 'owner/repo' strings to check",
        min_length=1,
        max_length=20,
    )
    dry_run: bool = Field(
        False,
        description="If true, detect changes but do not open any PRs",
    )


class RepoResult(BaseModel):
    repo: str
    status: str  # "unchanged", "changed", "analysed", "error", "no_specs"
    previous_sha: str = ""
    current_sha: str = ""
    changed_specs: list[str] = []
    breaking_changes: list[dict] = []
    follow_on_plan: dict = {}
    follow_on_results: dict = {}
    error: str = ""


class PollResponse(BaseModel):
    results: list[RepoResult]
    summary: str


# ─── GitHub API helpers (lightweight, no webhook dependency) ─────────

def _github_get(path: str, token: str) -> dict | list | None:
    """Simple GET to GitHub API. Returns parsed JSON or None on error."""
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    import ssl

    url = f"https://api.github.com{path}"
    req = Request(url, headers={
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "ripple-poll",
    })
    try:
        ctx = ssl.create_default_context()
        with urlopen(req, timeout=15, context=ctx) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else None
    except HTTPError:
        return None
    except Exception:
        return None


def _get_head_sha(repo: str, token: str) -> str:
    """Current HEAD SHA of a repo's default branch, or '' on failure."""
    info = _github_get(f"/repos/{repo}", token)
    if not isinstance(info, dict) or info.get("error"):
        return ""
    default_branch = info.get("default_branch", "main")
    ref = _github_get(f"/repos/{repo}/git/ref/heads/{default_branch}", token)
    if not isinstance(ref, dict):
        return ""
    return (ref.get("object") or {}).get("sha", "")


def _get_changed_files(repo: str, base_sha: str, head_sha: str,
                       token: str) -> list[str]:
    """Files changed between two commits."""
    compare = _github_get(
        f"/repos/{repo}/compare/{base_sha}...{head_sha}", token)
    if not isinstance(compare, dict):
        return []
    return [f["filename"] for f in (compare.get("files") or [])]


def _fetch_file(repo: str, path: str, ref: str, token: str) -> str | None:
    """Fetch file content at a specific ref. Returns None on failure."""
    data = _github_get(f"/repos/{repo}/contents/{path}?ref={ref}", token)
    if isinstance(data, dict) and "content" in data:
        return base64.b64decode(data["content"]).decode()
    return None


def _is_spec_file(filepath: str) -> bool:
    """Mirror of webhook._is_spec_file — keeps poll self-contained."""
    lower = filepath.lower()

    openapi_indicators = [
        "openapi", "swagger", "api-spec", "api_spec",
        "spec.yaml", "spec.yml", "spec.json",
    ]
    if any(ind in lower for ind in openapi_indicators):
        return True
    if lower.endswith((".yaml", ".yml", ".json")) and "api" in lower:
        return True
    if lower.endswith(".proto"):
        return True
    if lower.endswith((".graphql", ".gql")):
        return True
    if "asyncapi" in lower:
        return True
    if lower.endswith(".avsc") or ("avro" in lower and lower.endswith(".json")):
        return True
    if lower.endswith(".thrift"):
        return True
    if lower.endswith(".smithy"):
        return True
    return False


def _detect_contract_type(filepath: str) -> str:
    """Infer contract type from file extension."""
    lower = filepath.lower()
    if lower.endswith(".proto"):
        return "protobuf"
    if lower.endswith((".graphql", ".gql")):
        return "graphql"
    if "asyncapi" in lower:
        return "asyncapi"
    if lower.endswith(".avsc") or "avro" in lower:
        return "avro"
    if lower.endswith(".thrift"):
        return "thrift"
    if lower.endswith(".smithy"):
        return "smithy"
    return "openapi"


# ─── Core poll logic ─────────────────────────────────────────────────

def _poll_one_repo(repo: str, token: str, dry_run: bool) -> RepoResult:
    """Check one repo for spec changes since last poll."""
    result = RepoResult(repo=repo, status="unchanged")

    # 1. Get current HEAD
    current_sha = _get_head_sha(repo, token)
    if not current_sha:
        result.status = "error"
        result.error = "could not resolve HEAD SHA (repo may not exist or token lacks access)"
        return result
    result.current_sha = current_sha

    # 2. Compare to stored SHA
    previous_sha = poll_store.get_last_sha(repo)
    result.previous_sha = previous_sha

    if previous_sha == current_sha:
        result.status = "unchanged"
        return result

    # 3. Find changed files
    if previous_sha:
        changed_files = _get_changed_files(repo, previous_sha, current_sha, token)
    else:
        # First poll — check the latest commit only
        changed_files = _get_changed_files(repo, f"{current_sha}~1",
                                           current_sha, token)

    spec_files = [f for f in changed_files if _is_spec_file(f)]
    result.changed_specs = spec_files

    if not spec_files:
        result.status = "no_specs"
        poll_store.set_last_sha(repo, current_sha, "no_specs")
        return result

    result.status = "changed"

    # 4. For each spec file: fetch before/after, diff for breaking changes
    all_breaking = []
    for spec_path in spec_files:
        old_content = _fetch_file(repo, spec_path, previous_sha or f"{current_sha}~1", token)
        new_content = _fetch_file(repo, spec_path, current_sha, token)

        if old_content is None and new_content is None:
            continue
        if old_content is None:
            old_content = ""  # new file
        if new_content is None:
            new_content = ""  # deleted file

        contract_type = _detect_contract_type(spec_path)

        try:
            if contract_type == "protobuf":
                diff_result = _diff_proto(old_content, new_content)
            elif contract_type == "graphql":
                diff_result = _diff_graphql(old_content, new_content)
            else:
                import tempfile
                with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml',
                                                 delete=False) as f_old:
                    f_old.write(old_content)
                    old_path = f_old.name
                with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml',
                                                 delete=False) as f_new:
                    f_new.write(new_content)
                    new_path = f_new.name
                try:
                    diff_result = _diff_specs(old_path, new_path)
                finally:
                    os.unlink(old_path)
                    os.unlink(new_path)

            if diff_result and diff_result.has_breaking_changes:
                for bc in diff_result.breaking_changes:
                    all_breaking.append({
                        "spec_file": spec_path,
                        "contract_type": contract_type,
                        "change_type": bc.change_type,
                        "field_name": bc.field_name,
                        "field_type": bc.field_type,
                        "path": bc.path,
                        "method": bc.method,
                        "description": bc.description,
                        "severity": bc.severity,
                    })
        except Exception as e:
            all_breaking.append({
                "spec_file": spec_path,
                "error": f"{type(e).__name__}: {str(e)[:200]}",
            })

    result.breaking_changes = all_breaking

    if all_breaking:
        result.status = "analysed"

    # 5. Update stored SHA
    status_str = f"{len(all_breaking)} breaking" if all_breaking else "clean"
    poll_store.set_last_sha(repo, current_sha, status_str)

    return result


# ─── Routes ──────────────────────────────────────────────────────────

@router.post("/poll", response_model=PollResponse)
async def poll_repos(req: PollRequest):
    """Check repos for API spec changes since last poll.

    Requires GITHUB_TOKEN (env var or GitHub App token).
    Stores last-checked SHA per repo in SQLite.
    """
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        # Try GitHub App token
        try:
            from .github_app_auth import is_app_configured, get_installation_token
            if is_app_configured():
                token = get_installation_token()
        except Exception:
            pass

    if not token:
        raise HTTPException(
            status_code=401,
            detail="GITHUB_TOKEN not set and no GitHub App configured",
        )

    results = []
    for repo in req.repos:
        repo = repo.strip()
        if "/" not in repo:
            results.append(RepoResult(
                repo=repo, status="error",
                error="repo must be in 'owner/name' format"))
            continue
        r = _poll_one_repo(repo, token, req.dry_run)
        results.append(r)

    # Summary
    changed = sum(1 for r in results if r.status in ("changed", "analysed"))
    breaking = sum(len(r.breaking_changes) for r in results)
    summary = (f"{len(results)} repos checked, {changed} changed, "
               f"{breaking} breaking changes found")

    return PollResponse(results=results, summary=summary)


@router.get("/poll/status")
async def poll_status():
    """List all watched repos and their last-known state."""
    return {"watched": poll_store.get_all_watched()}


@router.delete("/poll/{owner}/{repo}")
async def poll_unwatch(owner: str, repo: str):
    """Stop watching a repo (remove from poll state)."""
    full = f"{owner}/{repo}"
    removed = poll_store.remove_repo(full)
    if not removed:
        raise HTTPException(status_code=404, detail=f"{full} is not being watched")
    return {"removed": full}
