"""
ripple/app/webhook.py

GitHub Webhook Server — auto-triggers Ripple when API specs change.

When installed as a GitHub App:
1. Listens for push events to main/master
2. Checks if any OpenAPI spec files changed
3. If yes: runs the full pipeline (diff → find → fix → PR)

Run locally:
    uvicorn app.webhook:app --port 8000

Deploy:
    Railway/Fly.io/Render with this as the entry point.
"""

import hashlib
import hmac
import json
import os
import ssl
import base64
from urllib.request import Request, urlopen
from urllib.error import HTTPError

try:
    from fastapi import FastAPI, Request as FastAPIRequest, HTTPException
    from fastapi.responses import JSONResponse
except ImportError:
    print("ERROR: Install fastapi + uvicorn:")
    print("  pip install fastapi uvicorn")
    raise

from .diff_engine import diff_specs, BreakingChange, DiffResult
from .consumer_finder import find_consumers, ConsumerMatch
from .fix_generator import generate_fix, GeneratedFix, _generate_with_template
from .pr_engine import CreatedPR

app = FastAPI(title="Ripple", description="Self-maintaining APIs")

# SSL context for GitHub API calls (Amazon dev desktop fix)
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


# === Health check ===

@app.get("/")
async def root():
    return {"status": "ok", "service": "ripple", "version": "0.1.0"}


@app.get("/health")
async def health():
    return {"healthy": True}


# === GitHub Webhook ===

@app.post("/webhook")
async def github_webhook(request: FastAPIRequest):
    """
    Receives GitHub push events and triggers the Ripple pipeline.
    
    Expected flow:
    1. GitHub App sends push event when someone pushes to main
    2. We check if any spec files changed (*.yaml, *.json with openapi)
    3. If yes, we fetch old vs new spec, run diff, find consumers, generate fixes, open PRs
    """
    # Verify webhook signature (if secret is configured)
    body = await request.body()
    _verify_signature(request, body)
    
    payload = json.loads(body)
    event_type = request.headers.get("X-GitHub-Event", "")
    
    # Only handle push events
    if event_type != "push":
        return {"status": "ignored", "reason": f"event_type={event_type}"}
    
    # Only handle pushes to default branch
    ref = payload.get("ref", "")
    default_branch = payload.get("repository", {}).get("default_branch", "main")
    if ref != f"refs/heads/{default_branch}":
        return {"status": "ignored", "reason": f"not default branch (ref={ref})"}
    
    # Check if any OpenAPI spec files changed
    spec_files = _find_changed_specs(payload)
    if not spec_files:
        return {"status": "ignored", "reason": "no spec files changed"}
    
    # Process each changed spec
    repo_full_name = payload["repository"]["full_name"]
    results = []
    
    for spec_path, before_sha, after_sha in spec_files:
        result = await _process_spec_change(
            repo=repo_full_name,
            spec_path=spec_path,
            before_sha=before_sha,
            after_sha=after_sha,
            installation_id=payload.get("installation", {}).get("id"),
        )
        results.append(result)
    
    return {"status": "processed", "results": results}


# === Internal Logic ===

def _verify_signature(request: FastAPIRequest, body: bytes):
    """Verify GitHub webhook signature."""
    secret = os.environ.get("WEBHOOK_SECRET")
    if not secret:
        return  # No secret configured, skip verification
    
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not signature:
        raise HTTPException(status_code=401, detail="Missing signature")
    
    expected = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()
    
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=401, detail="Invalid signature")


def _find_changed_specs(payload: dict) -> list[tuple[str, str, str]]:
    """
    Find OpenAPI spec files that changed in this push.
    Returns: [(file_path, before_sha, after_sha)]
    """
    spec_files = []
    before = payload.get("before", "")
    after = payload.get("after", "")
    
    # Check commits for modified/added files
    for commit in payload.get("commits", []):
        for filepath in commit.get("modified", []) + commit.get("added", []):
            if _is_spec_file(filepath):
                spec_files.append((filepath, before, after))
    
    return spec_files


def _is_spec_file(filepath: str) -> bool:
    """Check if a file is likely an OpenAPI spec."""
    lower = filepath.lower()
    spec_indicators = [
        "openapi", "swagger", "api-spec", "api_spec",
        "spec.yaml", "spec.yml", "spec.json",
    ]
    if any(ind in lower for ind in spec_indicators):
        return True
    # Also match common patterns
    if lower.endswith((".yaml", ".yml", ".json")) and "api" in lower:
        return True
    return False


async def _process_spec_change(
    repo: str,
    spec_path: str,
    before_sha: str,
    after_sha: str,
    installation_id: int = None,
) -> dict:
    """
    Process a spec file change:
    1. Fetch old and new versions
    2. Detect breaking changes
    3. Find consumers in connected repos
    4. Generate fixes + open PRs
    """
    token = _get_token(installation_id)
    
    # Fetch old and new spec content
    old_content = _fetch_file_at_sha(repo, spec_path, before_sha, token)
    new_content = _fetch_file_at_sha(repo, spec_path, after_sha, token)
    
    if not old_content or not new_content:
        return {"status": "error", "reason": "could not fetch spec versions"}
    
    # Parse specs and diff
    import tempfile
    import yaml
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(old_content)
        old_path = f.name
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(new_content)
        new_path = f.name
    
    diff_result = diff_specs(old_path, new_path)
    
    # Clean up temp files
    os.unlink(old_path)
    os.unlink(new_path)
    
    if not diff_result.has_breaking_changes:
        return {"status": "no_breaking_changes", "spec": spec_path}
    
    # Find consumer repos (from GitHub App installation)
    # For now: check repos in the same org/user
    consumer_repos = _find_consumer_repos(repo, token)
    
    # For each breaking change, find consumers and fix them
    prs_created = []
    
    for change in diff_result.breaking_changes:
        for consumer_repo in consumer_repos:
            # Search for consumers in this repo
            consumer_files = _search_repo_for_consumers(consumer_repo, change, token)
            
            for consumer_file, consumer_content in consumer_files:
                # Generate fix
                consumer = ConsumerMatch(
                    file_path=consumer_file,
                    line_number=0,
                    code_snippet="",
                    confidence="high",
                    match_reason="API endpoint reference found",
                    language=_detect_lang(consumer_file),
                )
                
                fixed_code, explanation = _generate_with_template(
                    consumer_content, consumer, change
                )
                
                if fixed_code != consumer_content:
                    # Create PR
                    pr_url = _create_fix_pr(
                        consumer_repo, consumer_file,
                        fixed_code, change, repo, token
                    )
                    if pr_url:
                        prs_created.append(pr_url)
    
    return {
        "status": "processed",
        "spec": spec_path,
        "breaking_changes": len(diff_result.breaking_changes),
        "prs_created": prs_created,
    }


# === GitHub API Helpers ===

def _get_token(installation_id: int = None) -> str:
    """Get GitHub token (personal or installation)."""
    # For now: use personal token. With a real GitHub App, you'd exchange
    # the installation_id for an installation access token.
    return os.environ.get("GITHUB_TOKEN", "")


def _github_api(method: str, path: str, token: str, data: dict = None) -> dict:
    """Make GitHub API request with SSL fix."""
    url = f"https://api.github.com{path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    
    body = json.dumps(data).encode() if data else None
    if body:
        headers["Content-Type"] = "application/json"
    
    req = Request(url, data=body, headers=headers, method=method)
    
    try:
        with urlopen(req, timeout=15, context=SSL_CTX) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        error_body = e.read().decode() if hasattr(e, 'read') else ""
        return {"error": e.code, "message": error_body[:200]}


def _fetch_file_at_sha(repo: str, path: str, sha: str, token: str) -> str:
    """Fetch file content at a specific commit SHA."""
    data = _github_api("GET", f"/repos/{repo}/contents/{path}?ref={sha}", token)
    if "content" in data:
        return base64.b64decode(data["content"]).decode()
    return ""


def _find_consumer_repos(source_repo: str, token: str) -> list[str]:
    """Find repos that might consume APIs from the source repo."""
    # Get all repos for the user/org
    owner = source_repo.split("/")[0]
    data = _github_api("GET", f"/users/{owner}/repos?per_page=100", token)
    
    if isinstance(data, list):
        return [
            r["full_name"] for r in data
            if r["full_name"] != source_repo and not r.get("archived")
        ]
    return []


def _search_repo_for_consumers(repo: str, change: BreakingChange, token: str) -> list[tuple[str, str]]:
    """Search a repo for files that consume the changed endpoint."""
    # Use GitHub code search or contents API
    # For demo: search for the endpoint path in the repo
    endpoint = change.path  # e.g., "/users"
    
    # GitHub search API
    data = _github_api(
        "GET",
        f"/search/code?q={endpoint}+in:file+repo:{repo}",
        token
    )
    
    results = []
    if "items" in data:
        for item in data["items"][:5]:  # Limit to 5 files
            file_path = item["path"]
            if _is_code_file(file_path):
                # Fetch file content
                file_data = _github_api("GET", f"/repos/{repo}/contents/{file_path}", token)
                if "content" in file_data:
                    content = base64.b64decode(file_data["content"]).decode()
                    results.append((file_path, content))
    
    return results


def _is_code_file(filepath: str) -> bool:
    """Check if file is code."""
    exts = {".ts", ".tsx", ".js", ".py", ".java", ".go", ".rs", ".rb"}
    return any(filepath.endswith(ext) for ext in exts)


def _detect_lang(filepath: str) -> str:
    """Detect language from extension."""
    if filepath.endswith((".ts", ".tsx")): return "typescript"
    if filepath.endswith((".js", ".jsx")): return "javascript"
    if filepath.endswith(".py"): return "python"
    if filepath.endswith(".java"): return "java"
    if filepath.endswith(".go"): return "go"
    return "unknown"


def _create_fix_pr(
    repo: str, file_path: str, fixed_content: str,
    change: BreakingChange, source_repo: str, token: str
) -> str:
    """Create a PR with the fix in the consumer repo."""
    # Get default branch
    repo_data = _github_api("GET", f"/repos/{repo}", token)
    default_branch = repo_data.get("default_branch", "main")
    
    # Get HEAD sha
    ref_data = _github_api("GET", f"/repos/{repo}/git/ref/heads/{default_branch}", token)
    if "object" not in ref_data:
        return ""
    base_sha = ref_data["object"]["sha"]
    
    # Create branch
    branch = f"ripple/fix-{change.field_name}-{change.path.replace('/', '-').strip('-')}"
    _github_api("POST", f"/repos/{repo}/git/refs", token, {
        "ref": f"refs/heads/{branch}",
        "sha": base_sha,
    })
    
    # Get current file sha
    file_data = _github_api("GET", f"/repos/{repo}/contents/{file_path}?ref={branch}", token)
    file_sha = file_data.get("sha", "")
    
    # Push fix
    commit_msg = f"fix: Add required field '{change.field_name}' to {change.method.upper()} {change.path}"
    _github_api("PUT", f"/repos/{repo}/contents/{file_path}", token, {
        "message": commit_msg,
        "content": base64.b64encode(fixed_content.encode()).decode(),
        "branch": branch,
        "sha": file_sha,
    })
    
    # Create PR
    pr_data = _github_api("POST", f"/repos/{repo}/pulls", token, {
        "title": commit_msg,
        "body": f"## 🌊 Ripple\n\nAPI `{source_repo}` added required field `{change.field_name}` to `{change.method.upper()} {change.path}`.\n\nThis PR updates the consumer code to include the new field.\n\n---\n*Auto-generated by Ripple*",
        "head": branch,
        "base": default_branch,
    })
    
    return pr_data.get("html_url", "")
