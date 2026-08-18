from __future__ import annotations
"""
ripple/app/gitlab_setup.py

GitLab Setup Page — a simple HTML page that guides users through
connecting their GitLab project to Ripple's hosted instance.

Steps:
1. Generate a GitLab Personal Access Token (api scope)
2. Register it with Ripple (stored per-org)
3. Add webhook URL to their GitLab project
4. Done — push a breaking change and watch the MR appear
"""

import json
import os
from typing import Optional
from .experimental import experimental_enabled, experimental_disabled

try:
    from fastapi import APIRouter, Request as FastAPIRequest
    from fastapi.responses import HTMLResponse, JSONResponse
except ImportError:
    pass

router = APIRouter()

# In-memory token storage (swap to DB in production)
_gitlab_tokens: dict[str, str] = {}  # org/project → token


@router.get("/setup/gitlab", response_class=HTMLResponse)
async def gitlab_setup_page():
    """Render the GitLab setup page."""
    # Switched off for the 30-day push -- see app/experimental.py.
    if not experimental_enabled():
        return experimental_disabled("gitlab", "setup page")
    return HTMLResponse(content=GITLAB_SETUP_HTML)


@router.post("/setup/gitlab/register")
async def register_gitlab_token(request: FastAPIRequest):
    """
    Register a GitLab token for a project.
    Body: {"project": "org/repo", "token": "glpat-xxx"}
    """
    # Switched off for the 30-day push -- see app/experimental.py.
    if not experimental_enabled():
        return experimental_disabled("gitlab", "setup register")
    body = await request.body()
    data = json.loads(body)
    
    project = data.get("project", "").strip()
    token = data.get("token", "").strip()
    
    if not project or not token:
        return JSONResponse(
            status_code=400,
            content={"error": "Both 'project' and 'token' are required"}
        )
    
    if not token.startswith("glpat-") and not token.startswith("glpat"):
        return JSONResponse(
            status_code=400,
            content={"error": "Token should be a GitLab Personal Access Token (starts with glpat-)"}
        )
    
    _gitlab_tokens[project] = token
    
    webhook_url = os.environ.get("RAILWAY_PUBLIC_URL", "https://ripple-production-be7f.up.railway.app")
    
    return {
        "status": "registered",
        "project": project,
        "next_step": "Add this webhook URL to your GitLab project",
        "webhook_url": f"{webhook_url}/webhook/gitlab",
        "instructions": f"Go to {project} → Settings → Webhooks → Add webhook",
    }


@router.get("/setup/gitlab/tokens")
async def list_registered_projects():
    """List registered GitLab projects (without exposing tokens)."""
    # Switched off for the 30-day push -- see app/experimental.py.
    if not experimental_enabled():
        return experimental_disabled("gitlab", "setup tokens")
    return {
        "registered_projects": [
            {"project": p, "token_prefix": t[:10] + "..."} 
            for p, t in _gitlab_tokens.items()
        ],
        "count": len(_gitlab_tokens),
    }


def get_gitlab_token(project: str) -> Optional[str]:
    """Get stored token for a GitLab project."""
    return _gitlab_tokens.get(project)


# === Setup Page HTML ===

GITLAB_SETUP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Ripple — GitLab Setup</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; color: #1a1a1a; line-height: 1.6; max-width: 700px; margin: 0 auto; padding: 40px 24px; }
        h1 { font-size: 2rem; margin-bottom: 8px; }
        .subtitle { color: #555; margin-bottom: 32px; }
        .step { background: #f8f9fa; border-radius: 12px; padding: 24px; margin-bottom: 20px; border-left: 4px solid #0066ff; }
        .step h3 { margin-bottom: 12px; }
        .step code { background: #0d1117; color: #e6edf3; padding: 2px 8px; border-radius: 4px; font-size: 0.9rem; }
        .step pre { background: #0d1117; color: #e6edf3; padding: 16px; border-radius: 8px; margin-top: 12px; overflow-x: auto; font-size: 0.85rem; }
        input, button { padding: 12px 16px; border-radius: 8px; font-size: 1rem; border: 1px solid #ddd; width: 100%; margin-top: 8px; }
        button { background: #0066ff; color: white; border: none; cursor: pointer; font-weight: 600; }
        button:hover { background: #0052cc; }
        .form-group { margin-bottom: 16px; }
        label { font-weight: 600; display: block; margin-bottom: 4px; }
        .result { background: #dcfce7; border: 1px solid #16a34a; border-radius: 8px; padding: 16px; margin-top: 16px; display: none; }
        .result.error { background: #fef2f2; border-color: #dc2626; }
        .webhook-url { background: #0d1117; color: #3fb950; padding: 12px; border-radius: 8px; font-family: monospace; margin-top: 8px; word-break: break-all; }
        a { color: #0066ff; }
        .back { margin-top: 32px; text-align: center; }
    </style>
</head>
<body>

<h1>Ripple — GitLab Setup</h1>
<p class="subtitle">Connect your GitLab project to Ripple in 2 minutes.</p>

<div class="step">
    <h3>Step 1: Create a GitLab Access Token</h3>
    <p>Go to <a href="https://gitlab.com/-/user_settings/personal_access_tokens" target="_blank">GitLab → Settings → Access Tokens</a></p>
    <p>Create a token with these settings:</p>
    <ul style="margin-top: 8px; padding-left: 20px;">
        <li>Name: <code>ripple-api</code></li>
        <li>Scopes: ✅ <code>api</code> (read/write access)</li>
        <li>Expiry: 1 year (or no expiry)</li>
    </ul>
    <p style="margin-top: 8px;">Copy the token (starts with <code>glpat-</code>).</p>
</div>

<div class="step">
    <h3>Step 2: Register your project with Ripple</h3>
    <form id="registerForm">
        <div class="form-group">
            <label>GitLab Project (org/repo)</label>
            <input type="text" id="project" placeholder="my-org/my-api" required>
        </div>
        <div class="form-group">
            <label>GitLab Access Token</label>
            <input type="password" id="token" placeholder="glpat-xxxxxxxxxxxx" required>
        </div>
        <button type="submit">Register Project</button>
    </form>
    <div id="result" class="result"></div>
</div>

<div class="step">
    <h3>Step 3: Add Webhook to your GitLab project</h3>
    <p>Go to your project → <strong>Settings → Webhooks</strong> → Add webhook:</p>
    <div class="webhook-url" id="webhookUrl">https://ripple-production-be7f.up.railway.app/webhook/gitlab</div>
    <ul style="margin-top: 12px; padding-left: 20px;">
        <li>Trigger: ✅ <strong>Push events</strong></li>
        <li>Branch filter: <code>main</code> (or your default branch)</li>
        <li>SSL verification: ✅ Enable</li>
    </ul>
    <p style="margin-top: 8px;">Click <strong>"Add webhook"</strong>.</p>
</div>

<div class="step">
    <h3>Step 4: Push a breaking change — watch the MR appear! 🎉</h3>
    <p>Edit an API spec file (.proto, .graphql, openapi.yaml, or schema.sql), remove or add a required field, and push to main.</p>
    <p>Ripple will detect the break and open a Merge Request with the fix.</p>
</div>

<div class="back">
    <a href="/">← Back to Ripple</a>
</div>

<script>
document.getElementById('registerForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const result = document.getElementById('result');
    const project = document.getElementById('project').value;
    const token = document.getElementById('token').value;
    
    try {
        const res = await fetch('/setup/gitlab/register', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({project, token})
        });
        const data = await res.json();
        
        if (res.ok) {
            result.className = 'result';
            result.innerHTML = '✅ <strong>Registered!</strong> Now add the webhook URL above to your GitLab project settings.';
        } else {
            result.className = 'result error';
            result.innerHTML = '❌ ' + (data.error || 'Registration failed');
        }
    } catch (err) {
        result.className = 'result error';
        result.innerHTML = '❌ Network error: ' + err.message;
    }
    result.style.display = 'block';
});
</script>

</body>
</html>
"""
