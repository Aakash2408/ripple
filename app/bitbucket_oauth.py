from __future__ import annotations
"""
ripple/app/bitbucket_oauth.py

Bitbucket OAuth Flow — enables one-click installation for Bitbucket users.

Flow:
1. User clicks "Install on Bitbucket" → redirected to Bitbucket OAuth
2. User grants access → Bitbucket redirects back with code
3. Ripple exchanges code for access token
4. Ripple lists user's repos and auto-adds webhooks
5. Done — user's repos are monitored

Setup:
1. Bitbucket Settings → OAuth consumers → Add consumer
2. Name: Ripple
3. Callback URL: https://your-server/auth/bitbucket/callback
4. Permissions: Repositories (R+W), Pull Requests (R+W), Webhooks (R+W)
5. Save → copy Key + Secret
6. Set env vars: BITBUCKET_CLIENT_ID + BITBUCKET_CLIENT_SECRET
"""

import json
import os
import ssl
import secrets
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError

try:
    from fastapi import APIRouter, Request as FastAPIRequest
    from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
except ImportError:
    pass

router = APIRouter()

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# Only OAuth states are ephemeral (short-lived)
_oauth_states: dict[str, dict] = {}

from . import token_store
from .experimental import experimental_enabled, experimental_disabled

# Config
BITBUCKET_CLIENT_ID = os.environ.get("BITBUCKET_CLIENT_ID", "")
BITBUCKET_CLIENT_SECRET = os.environ.get("BITBUCKET_CLIENT_SECRET", "")
BASE_URL = os.environ.get("BASE_URL", "https://ripple-production-be7f.up.railway.app")
CALLBACK_URL = f"{BASE_URL}/auth/bitbucket/callback"


def _bb_api(method: str, url: str, token: str, data: dict = None) -> dict:
    """Make Bitbucket API request with OAuth token."""
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
        return {"error": e.code, "message": e.read().decode()[:200] if hasattr(e, 'read') else ""}


@router.get("/auth/bitbucket")
async def bitbucket_auth_start():
    """Redirect user to Bitbucket OAuth authorization page."""
    # Switched off for the 30-day push -- see app/experimental.py.
    if not experimental_enabled():
        return experimental_disabled("bitbucket", "oauth start")
    if not BITBUCKET_CLIENT_ID:
        return HTMLResponse(content=NO_CREDENTIALS_HTML)
    
    state = secrets.token_urlsafe(32)
    _oauth_states[state] = {"created": True}
    
    params = urlencode({
        "client_id": BITBUCKET_CLIENT_ID,
        "response_type": "code",
        "state": state,
    })
    
    return RedirectResponse(url=f"https://bitbucket.org/site/oauth2/authorize?{params}")


@router.get("/auth/bitbucket/callback")
async def bitbucket_auth_callback(code: str = "", state: str = "", error: str = ""):
    """Handle OAuth callback — exchange code for token, install webhooks."""
    # Switched off for the 30-day push -- see app/experimental.py.
    if not experimental_enabled():
        return experimental_disabled("bitbucket", "oauth callback")
    if error:
        return HTMLResponse(content=f"<h1>Authorization failed</h1><p>{error}</p>")
    
    if state not in _oauth_states:
        return HTMLResponse(content="<h1>Invalid state</h1>", status_code=400)
    
    del _oauth_states[state]
    
    # Exchange code for token
    token_data = _exchange_code(code)
    if "error" in token_data:
        return HTMLResponse(content=f"<h1>Token exchange failed</h1><p>{token_data}</p>")
    
    access_token = token_data.get("access_token", "")
    if not access_token:
        return HTMLResponse(content="<h1>No access token</h1>")
    
    # Get user info
    user = _bb_api("GET", "https://api.bitbucket.org/2.0/user", access_token)
    username = user.get("username", user.get("display_name", "unknown"))
    
    token_store.save_bitbucket_user(username, access_token, username, token_data.get("refresh_token", ""))
    
    # List user's repos and install webhooks
    repos_data = _bb_api("GET", "https://api.bitbucket.org/2.0/repositories?role=admin&pagelen=50", access_token)
    installed_repos = []
    
    if "values" in repos_data:
        for repo in repos_data["values"]:
            workspace = repo.get("workspace", {}).get("slug", "")
            repo_slug = repo.get("slug", "")
            full_name = repo.get("full_name", f"{workspace}/{repo_slug}")
            
            # Add webhook
            webhook_result = _add_webhook(workspace, repo_slug, access_token)
            if webhook_result and "uuid" in webhook_result:
                token_store.save_bitbucket_repo(full_name, access_token, webhook_result["uuid"], full_name)
                installed_repos.append(full_name)
    
    return HTMLResponse(content=_success_html(username, installed_repos))


@router.get("/auth/bitbucket/status")
async def bitbucket_auth_status():
    """Check connected Bitbucket repos."""
    # Switched off for the 30-day push -- see app/experimental.py.
    if not experimental_enabled():
        return experimental_disabled("bitbucket", "status")
    users = token_store.get_bitbucket_users()
    repos = token_store.get_bitbucket_repos()
    return {
        "connected_users": len(users),
        "monitored_repos": len(repos),
        "repos": list(repos.keys()),
    }


def _exchange_code(code: str) -> dict:
    """Exchange OAuth code for access token."""
    import base64
    
    url = "https://bitbucket.org/site/oauth2/access_token"
    data = urlencode({
        "grant_type": "authorization_code",
        "code": code,
    }).encode()
    
    # Basic auth with client_id:client_secret
    credentials = base64.b64encode(f"{BITBUCKET_CLIENT_ID}:{BITBUCKET_CLIENT_SECRET}".encode()).decode()
    
    req = Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Basic {credentials}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    
    try:
        with urlopen(req, timeout=15, context=SSL_CTX) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        return {"error": e.code, "message": e.read().decode()[:200] if hasattr(e, 'read') else ""}


def _add_webhook(workspace: str, repo_slug: str, token: str) -> dict:
    """Add Ripple webhook to a Bitbucket repository."""
    webhook_url = f"{BASE_URL}/webhook/bitbucket"
    
    return _bb_api("POST", f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo_slug}/hooks", token, {
        "description": "Ripple - Self-maintaining APIs",
        "url": webhook_url,
        "active": True,
        "events": ["repo:push"],
    })


def _success_html(username: str, repos: list) -> str:
    repo_list = "".join(f"<li>✅ {r}</li>" for r in repos)
    if not repos:
        repo_list = "<li>No repos found (you need admin access)</li>"
    
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Ripple — Bitbucket Connected!</title>
<style>body {{ font-family: -apple-system, sans-serif; max-width: 600px; margin: 0 auto; padding: 40px 24px; text-align: center; }}
h1 {{ font-size: 2.5rem; }} .repos {{ text-align: left; background: #f8f9fa; border-radius: 12px; padding: 24px; margin: 24px 0; }}
.repos ul {{ list-style: none; padding: 0; }} .repos li {{ padding: 8px 0; font-size: 1.1rem; }}
.next {{ background: #0066ff; color: white; padding: 14px 32px; border-radius: 8px; text-decoration: none; font-weight: 600; display: inline-block; margin-top: 16px; }}</style>
</head><body>
<h1>🎉 Connected!</h1>
<p>Hey <strong>{username}</strong> — Ripple is now monitoring your Bitbucket repos.</p>
<div class="repos"><h3>Webhooks installed on:</h3><ul>{repo_list}</ul></div>
<p>Push a breaking change to any API spec and Ripple will open a Pull Request with the fix.</p>
<a href="/" class="next">← Back to Ripple</a>
</body></html>"""


NO_CREDENTIALS_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Ripple — Bitbucket Setup</title>
<style>body { font-family: -apple-system, sans-serif; max-width: 600px; margin: 0 auto; padding: 40px 24px; }
code { background: #f0f0f0; padding: 2px 6px; border-radius: 4px; } a { color: #0066ff; }</style>
</head><body>
<h1>Bitbucket OAuth Not Configured</h1>
<p>Set <code>BITBUCKET_CLIENT_ID</code> and <code>BITBUCKET_CLIENT_SECRET</code> env vars.</p>
<ol>
<li>Go to Bitbucket → Settings → OAuth consumers → Add consumer</li>
<li>Callback URL: <code>https://ripple-production-be7f.up.railway.app/auth/bitbucket/callback</code></li>
<li>Permissions: Repositories (R+W), Pull Requests (R+W), Webhooks (R+W)</li>
<li>Copy Key + Secret → add to Railway env vars</li>
</ol>
<a href="/">← Back</a>
</body></html>"""
