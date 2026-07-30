from __future__ import annotations
"""
ripple/app/gitlab_oauth.py

GitLab OAuth Flow — enables one-click installation for GitLab users.

Flow:
1. User clicks "Install on GitLab" → redirected to GitLab OAuth page
2. User authorizes Ripple → GitLab redirects back with auth code
3. Ripple exchanges code for access token
4. Ripple uses token to list user's projects and auto-add webhooks
5. Done — user's projects are now monitored

Setup (you need to register a GitLab OAuth Application):
1. Go to: https://gitlab.com/-/user_settings/applications
2. Name: Ripple
3. Redirect URI: https://ripple-production-be7f.up.railway.app/auth/gitlab/callback
4. Scopes: api
5. Save → get Application ID + Secret

Environment variables:
- GITLAB_APP_ID: OAuth Application ID
- GITLAB_APP_SECRET: OAuth Application Secret
- GITLAB_REDIRECT_URI: https://your-server/auth/gitlab/callback
- BASE_URL: https://ripple-production-be7f.up.railway.app (your server)
"""

import json
import os
import ssl
import secrets
from urllib.request import Request, urlopen
from urllib.parse import urlencode, quote
from urllib.error import HTTPError

try:
    from fastapi import APIRouter, Request as FastAPIRequest
    from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
except ImportError:
    pass

router = APIRouter()

# SSL context
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# In-memory storage (swap to DB in production)
_oauth_states: dict[str, dict] = {}  # state → metadata
_user_tokens: dict[str, dict] = {}   # user_id → {token, projects, ...}
_monitored_projects: dict[int, dict] = {}  # project_id → {token, webhook_id, ...}

# Config
GITLAB_URL = "https://gitlab.com"
GITLAB_APP_ID = os.environ.get("GITLAB_APP_ID", "")
GITLAB_APP_SECRET = os.environ.get("GITLAB_APP_SECRET", "")
BASE_URL = os.environ.get("BASE_URL", "https://ripple-production-be7f.up.railway.app")
REDIRECT_URI = f"{BASE_URL}/auth/gitlab/callback"


def _gitlab_api(method: str, path: str, token: str, data: dict = None) -> dict:
    """Make GitLab API request."""
    url = f"{GITLAB_URL}/api/v4{path}"
    headers = {
        "Authorization": f"Bearer {token}",
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


# === OAuth Endpoints ===

@router.get("/auth/gitlab")
async def gitlab_auth_start():
    """
    Step 1: Redirect user to GitLab authorization page.
    User clicks "Install on GitLab" → lands here → redirected to GitLab.
    """
    if not GITLAB_APP_ID:
        return HTMLResponse(content=NO_CREDENTIALS_HTML, status_code=200)
    
    state = secrets.token_urlsafe(32)
    _oauth_states[state] = {"created": True}
    
    params = urlencode({
        "client_id": GITLAB_APP_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "state": state,
        "scope": "api",
    })
    
    return RedirectResponse(url=f"{GITLAB_URL}/oauth/authorize?{params}")


@router.get("/auth/gitlab/callback")
async def gitlab_auth_callback(code: str = "", state: str = "", error: str = ""):
    """
    Step 2: GitLab redirects back with auth code.
    Exchange code for token, then auto-install webhooks.
    """
    if error:
        return HTMLResponse(content=f"<h1>Authorization failed</h1><p>{error}</p>")
    
    if state not in _oauth_states:
        return HTMLResponse(content="<h1>Invalid state</h1><p>Try again.</p>", status_code=400)
    
    del _oauth_states[state]
    
    # Exchange code for token
    token_data = _exchange_code(code)
    if "error" in token_data:
        return HTMLResponse(content=f"<h1>Token exchange failed</h1><p>{token_data}</p>")
    
    access_token = token_data.get("access_token", "")
    if not access_token:
        return HTMLResponse(content="<h1>No access token received</h1>")
    
    # Get user info
    user = _gitlab_api("GET", "/user", access_token)
    user_id = str(user.get("id", "unknown"))
    username = user.get("username", "unknown")
    
    # Store token
    _user_tokens[user_id] = {
        "token": access_token,
        "username": username,
        "refresh_token": token_data.get("refresh_token", ""),
    }
    
    # List user's projects and auto-install webhooks
    projects = _gitlab_api("GET", "/projects?owned=true&per_page=50", access_token)
    installed_projects = []
    
    if isinstance(projects, list):
        for project in projects:
            project_id = project.get("id")
            project_name = project.get("path_with_namespace", "")
            
            # Add webhook to each project
            webhook_result = _add_webhook(project_id, access_token)
            if webhook_result and "id" in webhook_result:
                _monitored_projects[project_id] = {
                    "token": access_token,
                    "webhook_id": webhook_result["id"],
                    "name": project_name,
                }
                installed_projects.append(project_name)
    
    # Return success page
    return HTMLResponse(content=_success_html(username, installed_projects))


@router.get("/auth/gitlab/status")
async def gitlab_auth_status():
    """Check how many GitLab projects are connected."""
    return {
        "connected_users": len(_user_tokens),
        "monitored_projects": len(_monitored_projects),
        "projects": [
            {"id": pid, "name": info["name"]}
            for pid, info in _monitored_projects.items()
        ],
    }


# === Helper Functions ===

def _exchange_code(code: str) -> dict:
    """Exchange OAuth authorization code for access token."""
    url = f"{GITLAB_URL}/oauth/token"
    data = urlencode({
        "client_id": GITLAB_APP_ID,
        "client_secret": GITLAB_APP_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    }).encode()
    
    req = Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    
    try:
        with urlopen(req, timeout=15, context=SSL_CTX) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        return {"error": e.code, "message": e.read().decode()[:200]}


def _add_webhook(project_id: int, token: str) -> dict:
    """Add Ripple webhook to a GitLab project."""
    webhook_url = f"{BASE_URL}/webhook/gitlab"
    
    return _gitlab_api("POST", f"/projects/{project_id}/hooks", token, {
        "url": webhook_url,
        "push_events": True,
        "push_events_branch_filter": "",  # all branches (filter in our handler)
        "enable_ssl_verification": True,
        "token": secrets.token_urlsafe(16),  # webhook secret
    })


def get_token_for_project(project_id: int) -> str:
    """Get stored OAuth token for a monitored project."""
    info = _monitored_projects.get(project_id)
    return info["token"] if info else ""


# === HTML Templates ===

def _success_html(username: str, projects: list) -> str:
    project_list = "".join(f"<li>✅ {p}</li>" for p in projects)
    if not projects:
        project_list = "<li>No projects found (you may need to create one first)</li>"
    
    return f"""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ripple — GitLab Connected!</title>
<style>
body {{ font-family: -apple-system, sans-serif; max-width: 600px; margin: 0 auto; padding: 40px 24px; text-align: center; }}
h1 {{ font-size: 2.5rem; margin-bottom: 16px; }}
.projects {{ text-align: left; background: #f8f9fa; border-radius: 12px; padding: 24px; margin: 24px 0; }}
.projects ul {{ list-style: none; padding: 0; }}
.projects li {{ padding: 8px 0; font-size: 1.1rem; }}
.next {{ background: #0066ff; color: white; padding: 14px 32px; border-radius: 8px; text-decoration: none; font-weight: 600; display: inline-block; margin-top: 16px; }}
</style>
</head><body>
<h1>🎉 Connected!</h1>
<p>Hey <strong>{username}</strong> — Ripple is now monitoring your GitLab projects.</p>
<div class="projects">
<h3>Webhooks installed on:</h3>
<ul>{project_list}</ul>
</div>
<p>Push a breaking change to any API spec (.proto, .graphql, openapi.yaml) and Ripple will open a Merge Request with the fix.</p>
<a href="/" class="next">← Back to Ripple</a>
</body></html>"""


NO_CREDENTIALS_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ripple — GitLab Setup Required</title>
<style>
body { font-family: -apple-system, sans-serif; max-width: 600px; margin: 0 auto; padding: 40px 24px; }
h1 { margin-bottom: 16px; }
code { background: #f0f0f0; padding: 2px 6px; border-radius: 4px; }
.steps { background: #f8f9fa; border-radius: 12px; padding: 24px; margin: 20px 0; }
a { color: #0066ff; }
</style>
</head><body>
<h1>🌊 GitLab OAuth Not Configured</h1>
<p>The server admin needs to register a GitLab OAuth Application.</p>
<div class="steps">
<h3>Setup (for server admin):</h3>
<ol>
<li>Go to <a href="https://gitlab.com/-/user_settings/applications">GitLab → Settings → Applications</a></li>
<li>Name: <code>Ripple</code></li>
<li>Redirect URI: <code>https://ripple-production-be7f.up.railway.app/auth/gitlab/callback</code></li>
<li>Scopes: ✅ <code>api</code></li>
<li>Save → copy Application ID + Secret</li>
<li>Set env vars: <code>GITLAB_APP_ID</code> and <code>GITLAB_APP_SECRET</code></li>
</ol>
</div>
<p>Meanwhile, you can use the <a href="/setup/gitlab">manual setup</a> (takes 2 minutes).</p>
<a href="/">← Back to Ripple</a>
</body></html>"""
