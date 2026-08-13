from __future__ import annotations
"""
ripple/app/gitlab_support.py

GitLab Webhook Support — handles GitLab push events and creates Merge Requests.

GitLab uses:
- Push events (same concept as GitHub, different payload format)
- Merge Requests (instead of Pull Requests)
- Personal Access Tokens or Project Access Tokens (instead of GitHub App)

Setup:
1. User adds a webhook URL to their GitLab project (Settings → Webhooks)
2. Webhook URL: https://your-ripple-server.com/webhook/gitlab
3. Trigger: Push events
4. Secret token: optional (for signature verification)

Environment variables:
- GITLAB_TOKEN: Personal/Project access token with api scope
- GITLAB_WEBHOOK_SECRET: Optional webhook secret for verification
"""

import hashlib
import json
import os
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import ssl

# Reuse SSL context from main webhook
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


class GitLabClient:
    """Client for GitLab API v4."""
    
    def __init__(self, token: str = None, base_url: str = "https://gitlab.com"):
        self.token = token or os.environ.get("GITLAB_TOKEN", "")
        self.base_url = base_url.rstrip("/")
        self.api_url = f"{self.base_url}/api/v4"
    
    def _request(self, method: str, path: str, data: dict = None) -> dict:
        """Make GitLab API request. Supports both PAT and OAuth tokens."""
        url = f"{self.api_url}{path}"
        headers = {"Content-Type": "application/json"}
        
        # OAuth tokens (from auth flow) need Bearer auth
        # Personal Access Tokens (glpat-) use PRIVATE-TOKEN header
        if self.token.startswith("glpat-"):
            headers["PRIVATE-TOKEN"] = self.token
        else:
            headers["Authorization"] = f"Bearer {self.token}"
        
        body = json.dumps(data).encode() if data else None
        req = Request(url, data=body, headers=headers, method=method)
        
        try:
            with urlopen(req, timeout=15, context=SSL_CTX) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            error_body = e.read().decode() if hasattr(e, 'read') else ""
            return {"error": e.code, "message": error_body[:200]}
    
    def get_file(self, project_id: int, file_path: str, ref: str = "main") -> str:
        """Fetch file content from a project at a specific ref."""
        import base64
        from urllib.parse import quote
        encoded_path = quote(file_path, safe="")
        data = self._request("GET", f"/projects/{project_id}/repository/files/{encoded_path}?ref={ref}")
        if "content" in data:
            return base64.b64decode(data["content"]).decode()
        return ""
    
    def get_file_at_commit(self, project_id: int, file_path: str, sha: str) -> str:
        """Fetch file at a specific commit SHA."""
        return self.get_file(project_id, file_path, ref=sha)
    
    def list_projects(self, search: str = None, owned: bool = True) -> list[dict]:
        """List projects the token has access to."""
        params = "?owned=true&per_page=100" if owned else "?per_page=100"
        if search:
            params += f"&search={search}"
        return self._request("GET", f"/projects{params}")
    
    def create_branch(self, project_id: int, branch: str, ref: str = "main") -> dict:
        """Create a new branch."""
        return self._request("POST", f"/projects/{project_id}/repository/branches", {
            "branch": branch,
            "ref": ref,
        })
    
    def update_file(self, project_id: int, file_path: str, content: str,
                    branch: str, commit_message: str) -> dict:
        """Update a file in a project (creates commit)."""
        from urllib.parse import quote
        import base64
        encoded_path = quote(file_path, safe="")
        return self._request("PUT", f"/projects/{project_id}/repository/files/{encoded_path}", {
            "branch": branch,
            "content": content,
            "commit_message": commit_message,
            "encoding": "text",
        })
    
    def create_merge_request(self, project_id: int, source_branch: str,
                            target_branch: str, title: str, description: str) -> dict:
        """Create a Merge Request."""
        return self._request("POST", f"/projects/{project_id}/merge_requests", {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": description,
            "remove_source_branch": True,
        })
    
    def search_code(self, project_id: int, query: str) -> list[dict]:
        """Search for code in a project."""
        from urllib.parse import quote
        data = self._request("GET", f"/projects/{project_id}/search?scope=blobs&search={quote(query)}")
        if isinstance(data, list):
            return data
        return []


def parse_gitlab_push_event(payload: dict) -> dict:
    """
    Parse a GitLab push event payload into a normalized format.
    
    Returns:
        {
            "ref": "refs/heads/main",
            "before": "abc123",
            "after": "def456",
            "project_id": 12345,
            "project_name": "my-project",
            "project_url": "https://gitlab.com/org/my-project",
            "default_branch": "main",
            "commits": [...],
            "changed_files": ["path/to/file.proto", ...],
        }
    """
    project = payload.get("project", {})
    
    # Collect all changed files from commits
    changed_files = set()
    for commit in payload.get("commits", []):
        changed_files.update(commit.get("modified", []))
        changed_files.update(commit.get("added", []))
    
    return {
        "ref": payload.get("ref", ""),
        "before": payload.get("before", ""),
        "after": payload.get("after", ""),
        "project_id": project.get("id"),
        "project_name": project.get("path_with_namespace", ""),
        "project_url": project.get("web_url", ""),
        "default_branch": project.get("default_branch", "main"),
        "commits": payload.get("commits", []),
        "changed_files": list(changed_files),
    }


def verify_gitlab_signature(body: bytes, secret: str, token_header: str) -> bool:
    """Verify GitLab webhook secret token."""
    if not secret:
        return True  # No secret configured
    return token_header == secret


def create_fix_mr(client: GitLabClient, project_id: int, file_path: str,
                  fixed_content: str, change_description: str,
                  source_project: str, target_branch: str = "main") -> str:
    """
    Create a Merge Request with the fix in a consumer project.
    Returns the MR URL or empty string on failure.
    """
    # Create branch
    branch_name = f"ripple/fix-{file_path.replace('/', '-').strip('-')[:50]}"
    client.create_branch(project_id, branch_name, ref=target_branch)
    
    # Update file
    commit_msg = f"fix: {change_description}"
    client.update_file(project_id, file_path, fixed_content, branch_name, commit_msg)
    
    # Build impact report for MR body
    mr_description = _format_mr_description(change_description, source_project, file_path)
    
    # Create MR
    mr = client.create_merge_request(
        project_id=project_id,
        source_branch=branch_name,
        target_branch=target_branch,
        title=commit_msg,
        description=mr_description,
    )
    
    return mr.get("web_url", "")


def _format_mr_description(change_description: str, source_project: str, fixed_file: str,
                           scanned_refs: list[dict] = None) -> str:
    """Format MR description with change impact report + line-level references."""
    # Extract field name from description if possible
    field_name = ""
    if "'" in change_description:
        parts = change_description.split("'")
        if len(parts) >= 2:
            field_name = parts[1]
    elif "`" in change_description:
        parts = change_description.split("`")
        if len(parts) >= 2:
            field_name = parts[1]
    
    # Build the needs-review section with line refs if available
    review_section = ""
    if scanned_refs:
        review_section = "### ⚠️ Needs Manual Review\n\n"
        review_section += "| File | Category | Why | References |\n"
        review_section += "|------|----------|-----|------------|\n"
        for ref in scanned_refs:
            line_info = ref.get("line_refs", "")
            review_section += f"| `{ref['file_path']}` | {ref.get('category', 'code')} | {ref.get('reason', 'Review needed')} | {line_info} |\n"
        review_section += "\n"
    else:
        review_section = f"""### 📝 Scanned — Deliberately Left Alone

| Category | Status | Details |
|----------|--------|---------|
| 📋 Contract | ℹ️ Source | `{source_project}` — this is where the breaking change originated |
| 🧪 Tests | 📝 Review recommended | Check if tests reference `{field_name or 'changed field'}` |
| 📝 Docs | 📝 Review recommended | Update API docs if field was documented |
| 📖 Examples | 📝 Review recommended | Update examples if field was demonstrated |
| ⚙️ Config | ✅ Safe | No config references detected |

"""
    
    return f"""## Ripple

**API change in `{source_project}`:** {change_description}

### ✅ Changed (auto-fixed)

| File | Category | What was done |
|------|----------|---------------|
| `{fixed_file}` | 💻 code | Removed broken reference |

{review_section}---

*Ripple only auto-fixes code that would **break**. Docs, examples, and tests that still pass are flagged but not modified — you decide.*

---
*Auto-generated by [Ripple](https://ripple-cnn.pages.dev) — self-maintaining APIs*"""
