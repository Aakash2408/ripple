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
        activity_html = '<tr><td colspan="3" class="empty">No activity yet. Install on a repo and push a spec change to see Ripple in action.</td></tr>'
    
    # Build repos list
    repos_html = ""
    for repo in _installed_repos:
        repos_html += f'<li><a href="https://github.com/{repo}" target="_blank">{repo}</a></li>\n'
    
    if not repos_html:
        repos_html = '<li class="empty">No repos monitored yet. Install the GitHub App to get started.</li>'
    
    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Ripple Dashboard</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #09090b; color: #fafafa; padding: 0; min-height: 100vh; }}
        .header {{ background: linear-gradient(135deg, #09090b 0%, #18181b 100%); border-bottom: 1px solid #27272a; padding: 20px 32px; position: sticky; top: 0; z-index: 10; backdrop-filter: blur(12px); }}
        .header-inner {{ max-width: 1000px; margin: 0 auto; display: flex; align-items: center; justify-content: space-between; }}
        .logo {{ font-size: 1.4rem; font-weight: 700; }}
        .logo span {{ color: #3b82f6; }}
        .header-links a {{ color: #a1a1aa; text-decoration: none; margin-left: 20px; font-size: 0.85rem; transition: color 0.2s; }}
        .header-links a:hover {{ color: #fff; }}
        .container {{ max-width: 1000px; margin: 0 auto; padding: 32px; }}
        .stats-row {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 32px; }}
        .stat {{ background: #18181b; border: 1px solid #27272a; border-radius: 12px; padding: 24px; text-align: center; transition: border-color 0.2s, transform 0.2s; }}
        .stat:hover {{ border-color: #3b82f6; transform: translateY(-2px); }}
        .stat-value {{ font-size: 2rem; font-weight: 800; background: linear-gradient(135deg, #3b82f6, #8b5cf6); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
        .stat-label {{ font-size: 0.8rem; color: #71717a; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.5px; }}
        .card {{ background: #18181b; border: 1px solid #27272a; border-radius: 12px; padding: 24px; margin-bottom: 20px; transition: border-color 0.2s; }}
        .card:hover {{ border-color: #3f3f46; }}
        .card h2 {{ font-size: 1rem; font-weight: 600; margin-bottom: 16px; display: flex; align-items: center; gap: 8px; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th {{ text-align: left; padding: 10px 12px; color: #71717a; font-weight: 500; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid #27272a; }}
        td {{ padding: 12px; border-bottom: 1px solid #27272a; font-size: 0.9rem; }}
        tr:last-child td {{ border-bottom: none; }}
        tr:hover td {{ background: rgba(59, 130, 246, 0.03); }}
        ul {{ list-style: none; }}
        li {{ padding: 10px 0; border-bottom: 1px solid #27272a; }}
        li:last-child {{ border-bottom: none; }}
        a {{ color: #60a5fa; text-decoration: none; transition: color 0.2s; }}
        a:hover {{ color: #93c5fd; }}
        .badge {{ display: inline-block; background: rgba(34, 197, 94, 0.15); color: #4ade80; padding: 3px 10px; border-radius: 20px; font-size: 0.7rem; font-weight: 600; letter-spacing: 0.3px; }}
        .install-btn {{ display: inline-block; background: #3b82f6; color: white; padding: 10px 20px; border-radius: 8px; text-decoration: none; font-weight: 600; margin-top: 16px; font-size: 0.85rem; transition: all 0.2s; }}
        .install-btn:hover {{ background: #2563eb; transform: translateY(-1px); box-shadow: 0 4px 12px rgba(59,130,246,0.3); text-decoration: none; }}
        .empty {{ text-align: center; color: #52525b; padding: 32px; font-size: 0.9rem; }}
        @media (max-width: 768px) {{ .stats-row {{ grid-template-columns: repeat(2, 1fr); }} .container {{ padding: 16px; }} }}
    </style>
</head>
<body>
    <div class="header">
        <div class="header-inner">
            <div class="logo"><span>Ripple</span></div>
            <div class="header-links">
                <a href="https://ripple-cnn.pages.dev/">Home</a>
                <a href="/docs">API Docs</a>
                <a href="/health">Health</a>
                <a href="https://github.com/Aakash2408/ripple" target="_blank">GitHub ↗</a>
            </div>
        </div>
    </div>
    <div class="container">
        <div class="stats-row">
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
                <div class="stat-value">12</div>
                <div class="stat-label">Fix Languages</div>
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
                <tr><td>Protobuf (gRPC)</td><td><span class="badge">Active</span></td><td>Full pipeline (detect → find → fix → PR)</td></tr>
                <tr><td>GraphQL</td><td><span class="badge">Active</span></td><td>Full pipeline (detect → find → fix → PR)</td></tr>
                <tr><td>Database (SQL + Prisma)</td><td><span class="badge">Active</span></td><td>Full pipeline (detect → find → fix → PR)</td></tr>
                <tr><td>AsyncAPI (Kafka, SNS, MQTT)</td><td><span class="badge">Active</span></td><td>Full pipeline (detect → find → fix → PR)</td></tr>
                <tr><td>Avro (Confluent/Kafka)</td><td><span class="badge">Active</span></td><td>Full pipeline (detect → find → fix → PR)</td></tr>
                <tr><td>tRPC (TypeScript)</td><td><span class="badge">Active</span></td><td>Full pipeline (detect → find → fix → PR)</td></tr>
                <tr><td>Thrift (Apache/Meta)</td><td><span class="badge">Active</span></td><td>Full pipeline (detect → find → fix → PR)</td></tr>
                <tr><td>JSON Schema</td><td><span class="badge">Active</span></td><td>Full pipeline (detect → find → fix → PR)</td></tr>
                <tr><td>Smithy (AWS)</td><td><span class="badge">Active</span></td><td>Full pipeline (detect → find → fix → PR)</td></tr>
            </table>
        </div>

        <div class="card">
            <h2>🔗 Links</h2>
            <ul>
                <li><a href="https://ripple-cnn.pages.dev/" target="_blank">Landing Page</a></li>
                <li><a href="/docs">API Documentation (Swagger UI)</a></li>
                <li><a href="/health">Health Check</a></li>
                <li><a href="/auth/gitlab">Install on GitLab</a></li>
                <li><a href="/setup/gitlab">GitLab Manual Setup</a></li>
                <li><a href="/rate-limit/unknown">Rate Limit Status</a></li>
                <li><a href="https://github.com/Aakash2408/ripple" target="_blank">Source Code</a></li>
                <li><a href="https://github.com/apps/ripple-api" target="_blank">Install GitHub App</a></li>
            </ul>
        </div>
    </div>
</body>
</html>"""
    
    return HTMLResponse(content=html)
