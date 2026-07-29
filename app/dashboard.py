"""
ripple/app/dashboard.py

Dashboard — shows what Ripple is monitoring and what it's done.

Serves a single HTML page at /dashboard with:
- Installed repos
- Recent breaking changes detected
- PRs opened
- Learning status (co-change graph stats)
- API watcher status

No React. No framework. Just a clean HTML page served by FastAPI.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

# In-memory activity log (in production, this would be a database)
_activity_log: list[dict] = []
_installed_repos: list[str] = []


def log_activity(action: str, details: dict):
    """Log an activity event."""
    import time
    _activity_log.append({
        "timestamp": time.time(),
        "action": action,
        "details": details,
    })
    # Keep last 100 events
    if len(_activity_log) > 100:
        _activity_log.pop(0)


def register_repo(repo: str):
    """Register a monitored repo."""
    if repo not in _installed_repos:
        _installed_repos.append(repo)


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """Serve the dashboard HTML page."""
    import time
    
    # Build activity rows
    activity_html = ""
    for event in reversed(_activity_log[-20:]):
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(event["timestamp"]))
        action = event["action"]
        details = event["details"]
        
        if action == "breaking_change":
            icon = "⚠️"
            desc = f"Breaking change in <b>{details.get('spec', '?')}</b>: {details.get('change', '?')}"
        elif action == "pr_created":
            icon = "✅"
            desc = f"PR opened in <b>{details.get('repo', '?')}</b>: {details.get('title', '?')}"
        elif action == "learning_complete":
            icon = "🧠"
            desc = f"Learned from <b>{details.get('repo', '?')}</b>: {details.get('stats', '?')}"
        elif action == "install":
            icon = "📦"
            desc = f"Installed on <b>{details.get('repo', '?')}</b>"
        else:
            icon = "📋"
            desc = str(details)
        
        activity_html += f'<tr><td>{ts}</td><td>{icon}</td><td>{desc}</td></tr>\n'
    
    if not activity_html:
        activity_html = '<tr><td colspan="3" style="text-align:center;color:#888;">No activity yet. Install on a repo and push a spec change.</td></tr>'
    
    # Build repos list
    repos_html = ""
    for repo in _installed_repos:
        repos_html += f'<li><a href="https://github.com/{repo}" target="_blank">{repo}</a></li>\n'
    
    if not repos_html:
        repos_html = '<li style="color:#888;">No repos monitored yet. Install the GitHub App.</li>'
    
    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Ripple Dashboard</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0d1117; color: #e6edf3; padding: 24px; }}
        .container {{ max-width: 900px; margin: 0 auto; }}
        h1 {{ font-size: 1.8rem; margin-bottom: 8px; }}
        .subtitle {{ color: #8b949e; margin-bottom: 32px; }}
        .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 20px; margin-bottom: 20px; }}
        .card h2 {{ font-size: 1.2rem; margin-bottom: 12px; color: #58a6ff; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th, td {{ text-align: left; padding: 8px 12px; border-bottom: 1px solid #21262d; }}
        th {{ color: #8b949e; font-weight: 500; }}
        ul {{ list-style: none; }}
        li {{ padding: 6px 0; }}
        a {{ color: #58a6ff; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        .stat {{ display: inline-block; background: #21262d; border-radius: 6px; padding: 12px 20px; margin: 4px; text-align: center; }}
        .stat-value {{ font-size: 1.5rem; font-weight: 700; }}
        .stat-label {{ font-size: 0.8rem; color: #8b949e; }}
        .badge {{ display: inline-block; background: #238636; color: white; padding: 2px 8px; border-radius: 12px; font-size: 0.75rem; }}
        .install-btn {{ display: inline-block; background: #238636; color: white; padding: 10px 20px; border-radius: 6px; text-decoration: none; font-weight: 600; margin-top: 12px; }}
        .install-btn:hover {{ background: #2ea043; text-decoration: none; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🌊 Ripple Dashboard</h1>
        <p class="subtitle">Self-maintaining APIs — contract change propagation</p>
        
        <div style="margin-bottom: 24px;">
            <div class="stat">
                <div class="stat-value">{len(_installed_repos)}</div>
                <div class="stat-label">Repos Monitored</div>
            </div>
            <div class="stat">
                <div class="stat-value">{len([e for e in _activity_log if e['action'] == 'pr_created'])}</div>
                <div class="stat-label">PRs Created</div>
            </div>
            <div class="stat">
                <div class="stat-value">{len([e for e in _activity_log if e['action'] == 'breaking_change'])}</div>
                <div class="stat-label">Breaks Detected</div>
            </div>
            <div class="stat">
                <div class="stat-value">4</div>
                <div class="stat-label">Contract Types</div>
            </div>
        </div>

        <div class="card">
            <h2>📦 Monitored Repos</h2>
            <ul>
                {repos_html}
            </ul>
            <a href="https://github.com/apps/ripple-api" target="_blank" class="install-btn">+ Install on more repos</a>
        </div>

        <div class="card">
            <h2>📋 Recent Activity</h2>
            <table>
                <thead>
                    <tr><th>Time</th><th></th><th>Event</th></tr>
                </thead>
                <tbody>
                    {activity_html}
                </tbody>
            </table>
        </div>

        <div class="card">
            <h2>🧠 Supported Contracts</h2>
            <table>
                <tr><td>OpenAPI / Swagger</td><td><span class="badge">Active</span></td><td>Full pipeline (detect → find → fix → PR)</td></tr>
                <tr><td>Protobuf (gRPC)</td><td><span class="badge">Active</span></td><td>Breaking change detection</td></tr>
                <tr><td>GraphQL</td><td><span class="badge">Active</span></td><td>Breaking change detection</td></tr>
                <tr><td>Database (SQL + Prisma)</td><td><span class="badge">Active</span></td><td>Breaking change detection</td></tr>
            </table>
        </div>

        <div class="card">
            <h2>🔗 Links</h2>
            <ul>
                <li><a href="/docs">API Documentation (Swagger UI)</a></li>
                <li><a href="/health">Health Check</a></li>
                <li><a href="https://github.com/Aakash2408/ripple" target="_blank">Source Code</a></li>
                <li><a href="https://github.com/apps/ripple-api" target="_blank">Install GitHub App</a></li>
            </ul>
        </div>
    </div>
</body>
</html>"""
    
    return HTMLResponse(content=html)
