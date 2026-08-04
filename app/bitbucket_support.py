from __future__ import annotations
"""
ripple/app/bitbucket_support.py

Bitbucket Cloud Webhook Support — handles Bitbucket push events and creates Pull Requests.

Bitbucket uses:
- Webhooks (Settings → Webhooks → Add webhook)
- Pull Requests (similar to GitHub PRs)
- App passwords or OAuth consumers for API access

Setup:
1. Create Bitbucket App Password: Settings → App passwords → Create
   Permissions: Repositories (read+write), Pull requests (read+write)
2. Add webhook: Repository → Settings → Webhooks → Add webhook
   URL: https://your-ripple-server.com/webhook/bitbucket
   Triggers: Repository Push

Environment variables:
- BITBUCKET_USERNAME: your Bitbucket username
- BITBUCKET_APP_PASSWORD: App password with repo+PR permissions
- BITBUCKET_WEBHOOK_SECRET: Optional (Bitbucket signs with this)
"""

import base64
import hashlib
import hmac
import json
import os
import ssl
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


class BitbucketClient:
    """Client for Bitbucket Cloud REST API 2.0."""
    
    def __init__(self, username: str = None, app_password: str = None):
        self.username = username or os.environ.get("BITBUCKET_USERNAME", "")
        self.app_password = app_password or os.environ.get("BITBUCKET_APP_PASSWORD", "")
        self.api_url = "https://api.bitbucket.org/2.0"
    
    def _auth_header(self) -> str:
        """Generate Basic auth header."""
        credentials = f"{self.username}:{self.app_password}"
        encoded = base64.b64encode(credentials.encode()).decode()
        return f"Basic {encoded}"
    
    def _request(self, method: str, path: str, data: dict = None) -> dict:
        """Make Bitbucket API request."""
        url = f"{self.api_url}{path}"
        headers = {
            "Authorization": self._auth_header(),
            "Content-Type": "application/json",
        }
        
        body = json.dumps(data).encode() if data else None
        req = Request(url, data=body, headers=headers, method=method)
        
        try:
            with urlopen(req, timeout=15, context=SSL_CTX) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            error_body = e.read().decode() if hasattr(e, 'read') else ""
            return {"error": e.code, "message": error_body[:200]}
    
    def get_file(self, workspace: str, repo_slug: str, path: str, commit: str = None) -> str:
        """Fetch file content from a repository."""
        ref = f"/{commit}" if commit else ""
        url = f"{self.api_url}/repositories/{workspace}/{repo_slug}/src{ref}/{path}"
        headers = {"Authorization": self._auth_header()}
        req = Request(url, headers=headers)
        
        try:
            with urlopen(req, timeout=15, context=SSL_CTX) as resp:
                return resp.read().decode()
        except HTTPError:
            return ""
    
    def get_file_at_commit(self, workspace: str, repo_slug: str, path: str, sha: str) -> str:
        """Fetch file at a specific commit SHA."""
        return self.get_file(workspace, repo_slug, path, commit=sha)
    
    def create_branch(self, workspace: str, repo_slug: str, branch: str, target_sha: str) -> dict:
        """Create a new branch."""
        return self._request("POST", f"/repositories/{workspace}/{repo_slug}/refs/branches", {
            "name": branch,
            "target": {"hash": target_sha},
        })
    
    def update_file(self, workspace: str, repo_slug: str, path: str, 
                    content: str, branch: str, commit_message: str) -> dict:
        """Update a file (creates a commit on the branch)."""
        # Bitbucket uses multipart form for file uploads via src endpoint
        # For simplicity, use the commits API
        url = f"{self.api_url}/repositories/{workspace}/{repo_slug}/src"
        
        # Bitbucket's src endpoint accepts form data
        import urllib.parse
        form_data = urllib.parse.urlencode({
            path: content,
            "message": commit_message,
            "branch": branch,
        }).encode()
        
        headers = {
            "Authorization": self._auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        
        req = Request(url, data=form_data, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=15, context=SSL_CTX) as resp:
                return {"status": "ok"}
        except HTTPError as e:
            return {"error": e.code, "message": e.read().decode()[:200] if hasattr(e, 'read') else ""}
    
    def create_pull_request(self, workspace: str, repo_slug: str,
                           title: str, description: str,
                           source_branch: str, dest_branch: str = "main") -> dict:
        """Create a Pull Request."""
        return self._request("POST", f"/repositories/{workspace}/{repo_slug}/pullrequests", {
            "title": title,
            "description": description,
            "source": {"branch": {"name": source_branch}},
            "destination": {"branch": {"name": dest_branch}},
            "close_source_branch": True,
        })
    
    def search_code(self, workspace: str, repo_slug: str, query: str) -> list[dict]:
        """Search for code in a repository."""
        # Bitbucket code search via API
        from urllib.parse import quote
        data = self._request(
            "GET",
            f"/repositories/{workspace}/{repo_slug}/src?q=path+~+%22{quote(query)}%22&max_depth=5"
        )
        # Fallback: search using the search endpoint
        search_data = self._request(
            "GET",
            f"/workspaces/{workspace}/search/code?search_query={quote(query)}&page=1&pagelen=10"
        )
        
        results = []
        if "values" in search_data:
            for item in search_data["values"][:10]:
                file_obj = item.get("file", {})
                results.append({
                    "path": file_obj.get("path", ""),
                    "type": file_obj.get("type", ""),
                })
        return results
    
    def list_repos(self, workspace: str) -> list[dict]:
        """List repositories in a workspace."""
        data = self._request("GET", f"/repositories/{workspace}?pagelen=50")
        if "values" in data:
            return [{"slug": r["slug"], "full_name": r["full_name"]} for r in data["values"]]
        return []


def parse_bitbucket_push_event(payload: dict) -> dict:
    """
    Parse a Bitbucket push event payload into a normalized format.
    
    Returns:
        {
            "workspace": "my-workspace",
            "repo_slug": "my-repo",
            "full_name": "my-workspace/my-repo",
            "before": "abc123",
            "after": "def456",
            "branch": "main",
            "changed_files": [...],
            "commits": [...],
        }
    """
    repo = payload.get("repository", {})
    workspace = repo.get("workspace", {}).get("slug", "")
    repo_slug = repo.get("slug", "")
    full_name = repo.get("full_name", f"{workspace}/{repo_slug}")
    
    # Extract push changes
    changes = []
    changed_files = set()
    before = ""
    after = ""
    branch = ""
    
    for push_change in payload.get("push", {}).get("changes", []):
        new = push_change.get("new", {})
        old = push_change.get("old", {})
        
        if new:
            after = new.get("target", {}).get("hash", "")
            branch = new.get("name", "main")
        if old:
            before = old.get("target", {}).get("hash", "")
        
        # Collect commits
        for commit in push_change.get("commits", []):
            changes.append({
                "hash": commit.get("hash", ""),
                "message": commit.get("message", ""),
            })
    
    return {
        "workspace": workspace,
        "repo_slug": repo_slug,
        "full_name": full_name,
        "before": before,
        "after": after,
        "branch": branch,
        "changed_files": list(changed_files),
        "commits": changes,
    }


def verify_bitbucket_signature(body: bytes, secret: str, signature_header: str) -> bool:
    """Verify Bitbucket webhook signature (HMAC-SHA256)."""
    if not secret:
        return True
    
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature_header, expected)


def create_fix_pr(client: BitbucketClient, workspace: str, repo_slug: str,
                  file_path: str, fixed_content: str,
                  change_description: str, dest_branch: str = "main") -> str:
    """
    Create a Pull Request with the fix in a Bitbucket repository.
    Returns the PR URL or empty string on failure.
    """
    import time
    branch_name = f"ripple/fix-{int(time.time())}"
    
    # Get current HEAD
    repo_data = client._request("GET", f"/repositories/{workspace}/{repo_slug}")
    main_branch = repo_data.get("mainbranch", {}).get("name", "main")
    
    # Get HEAD commit
    branch_data = client._request("GET", f"/repositories/{workspace}/{repo_slug}/refs/branches/{main_branch}")
    head_sha = branch_data.get("target", {}).get("hash", "")
    
    if not head_sha:
        return ""
    
    # Create branch
    client.create_branch(workspace, repo_slug, branch_name, head_sha)
    
    # Push fix
    commit_msg = f"fix: {change_description}"
    client.update_file(workspace, repo_slug, file_path, fixed_content, branch_name, commit_msg)
    
    # Create PR
    pr_data = client.create_pull_request(
        workspace, repo_slug,
        title=commit_msg,
        description=f"## 🌊 Ripple\n\n{change_description}\n\n---\n*Auto-generated by Ripple*",
        source_branch=branch_name,
        dest_branch=main_branch,
    )
    
    return pr_data.get("links", {}).get("html", {}).get("href", "")
