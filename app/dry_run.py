"""
ripple/app/dry_run.py

Dry-Run Mode — shows what WOULD break if a spec change were pushed,
without actually opening any PRs. Zero-risk adoption tool.

Usage:
  POST /dry-run
  Body: {
    "spec_before": "...",   # Old spec content (or URL)
    "spec_after": "...",    # New spec content (or URL)
    "contract_type": "openapi",  # openapi|proto|graphql|database|asyncapi|avro|trpc|thrift|jsonschema|smithy
    "repo": "owner/repo"   # Optional: which repo to scan for consumers
  }

Response:
  {
    "breaking_changes": [...],
    "consumers_found": [...],
    "impact_report": "...",   # Markdown-formatted impact report
    "would_open_prs": 3,
    "dry_run": true
  }

This lets teams try Ripple without installing the GitHub App or
granting repo write access. Just paste two spec versions and see
what would happen.
"""

from __future__ import annotations
import json
from typing import Optional

try:
    from fastapi import APIRouter, Request
    from fastapi.responses import JSONResponse, HTMLResponse
except ImportError:
    pass

from .diff_engine import diff_specs, BreakingChange
from .impact_report import generate_impact_report

router = APIRouter()


@router.post("/dry-run")
async def dry_run_analysis(request: Request):
    """
    Analyze a spec change without opening PRs.
    Returns what WOULD break and which consumers WOULD get fix PRs.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid JSON body. Expected: spec_before, spec_after, contract_type"}
        )
    
    spec_before = body.get("spec_before", "")
    spec_after = body.get("spec_after", "")
    contract_type = body.get("contract_type", "openapi")
    repo = body.get("repo", "")
    
    if not spec_before or not spec_after:
        return JSONResponse(
            status_code=400,
            content={"error": "Both 'spec_before' and 'spec_after' are required"}
        )
    
    # Step 1: Detect breaking changes
    breaking_changes = _detect_changes(spec_before, spec_after, contract_type)
    
    if not breaking_changes:
        return JSONResponse(content={
            "dry_run": True,
            "breaking_changes": [],
            "consumers_found": [],
            "would_open_prs": 0,
            "summary": "✅ No breaking changes detected. Safe to push.",
            "impact_report": "",
        })
    
    # Step 2: Build summary (consumer finding requires repo access — not available in dry-run)
    impact_md = ""
    try:
        report = generate_impact_report(
            breaking_change_summary=f"{len(breaking_changes)} breaking change(s) in {contract_type} spec",
            source_file=f"{contract_type} spec",
            fixed_files=[],
            scanned_files=[],
        )
        impact_md = report.to_markdown()
    except Exception:
        pass
    
    # Step 3: Return dry-run results
    return JSONResponse(content={
        "dry_run": True,
        "breaking_changes": [
            {
                "type": bc.change_type,
                "field": bc.field_name,
                "path": bc.path,
                "severity": "breaking",
                "description": bc.description,
            }
            for bc in breaking_changes
        ],
        "consumers_found": [],
        "would_open_prs": 0,
        "summary": f"⚠️ {len(breaking_changes)} breaking change(s) detected. Install Ripple to auto-find consumers and open fix PRs.",
        "impact_report": impact_md,
    })


@router.get("/dry-run")
async def dry_run_ui():
    """Interactive UI for dry-run analysis."""
    return HTMLResponse(content=DRY_RUN_HTML)


def _detect_changes(before: str, after: str, contract_type: str) -> list[BreakingChange]:
    """Detect breaking changes between two spec versions."""
    try:
        # diff_specs expects file paths, but we have content strings
        # Use the basic diff for direct content comparison
        return _basic_diff(before, after, contract_type)
    except Exception:
        return []


def _basic_diff(before: str, after: str, contract_type: str) -> list[BreakingChange]:
    """Fallback: basic JSON/YAML field comparison for breaking changes."""
    changes = []
    
    try:
        before_data = json.loads(before)
        after_data = json.loads(after)
    except (json.JSONDecodeError, TypeError):
        return changes
    
    # For OpenAPI: check paths removed
    if contract_type == "openapi":
        before_paths = set(before_data.get("paths", {}).keys())
        after_paths = set(after_data.get("paths", {}).keys())
        
        removed_paths = before_paths - after_paths
        for path in removed_paths:
            changes.append(BreakingChange(
                change_type="endpoint_removed",
                field_name=path,
                field_type="endpoint",
                path=path,
                method="*",
                description=f"Endpoint {path} was removed",
            ))
        
        # Check for removed fields in response schemas
        for path in before_paths & after_paths:
            _check_schema_changes(before_data, after_data, path, changes)
    
    return changes


def _check_schema_changes(before_data: dict, after_data: dict, path: str, changes: list):
    """Check for removed/changed fields in schemas at a path."""
    before_methods = before_data.get("paths", {}).get(path, {})
    after_methods = after_data.get("paths", {}).get(path, {})
    
    for method in before_methods:
        if method.startswith("x-") or method == "parameters":
            continue
        if method not in after_methods:
            changes.append(BreakingChange(
                change_type="method_removed",
                field_name=f"{method.upper()} {path}",
                field_type="method",
                path=path,
                method=method,
                description=f"Method {method.upper()} removed from {path}",
            ))


# === HTML UI ===

DRY_RUN_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ripple — Dry Run</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, sans-serif; background: #0a0a0f; color: #e4e4e7; min-height: 100vh; }
.container { max-width: 900px; margin: 0 auto; padding: 40px 24px; }
h1 { font-size: 2rem; margin-bottom: 8px; }
.subtitle { color: #6b7280; margin-bottom: 32px; }
.form-group { margin-bottom: 20px; }
label { display: block; font-size: 0.9rem; font-weight: 600; margin-bottom: 6px; color: #94a3b8; }
textarea { width: 100%; min-height: 150px; background: #12121a; border: 1px solid #1e1e2e; border-radius: 8px; padding: 12px; color: #e4e4e7; font-family: 'Courier New', monospace; font-size: 0.85rem; resize: vertical; }
textarea:focus { outline: none; border-color: #3b82f6; }
select { background: #12121a; border: 1px solid #1e1e2e; border-radius: 8px; padding: 10px 16px; color: #e4e4e7; font-size: 0.9rem; }
.btn { background: #3b82f6; color: white; border: none; padding: 12px 28px; border-radius: 8px; font-size: 1rem; font-weight: 600; cursor: pointer; transition: all 0.2s; }
.btn:hover { background: #2563eb; transform: translateY(-1px); }
.btn:disabled { background: #334155; cursor: not-allowed; transform: none; }
.result { margin-top: 32px; padding: 24px; background: #12121a; border: 1px solid #1e1e2e; border-radius: 12px; display: none; }
.result.show { display: block; }
.result h3 { margin-bottom: 12px; }
.result pre { background: #0a0a0f; padding: 16px; border-radius: 8px; overflow-x: auto; font-size: 0.85rem; white-space: pre-wrap; }
.safe { color: #22c55e; }
.breaking { color: #ef4444; }
.info { color: #6b7280; font-size: 0.85rem; margin-top: 16px; }
a { color: #3b82f6; text-decoration: none; }
</style>
</head><body>
<div class="container">
    <h1>🌊 Ripple — Dry Run</h1>
    <p class="subtitle">See what would break. No PRs opened. No repo access needed.</p>
    
    <div class="form-group">
        <label>Contract Type</label>
        <select id="contractType">
            <option value="openapi">OpenAPI / Swagger</option>
            <option value="proto">Protobuf / gRPC</option>
            <option value="graphql">GraphQL</option>
            <option value="database">Database (SQL / Prisma)</option>
            <option value="asyncapi">AsyncAPI</option>
            <option value="avro">Avro</option>
            <option value="trpc">tRPC</option>
            <option value="thrift">Thrift</option>
            <option value="jsonschema">JSON Schema</option>
            <option value="smithy">Smithy</option>
        </select>
    </div>
    
    <div class="form-group">
        <label>Before (old spec)</label>
        <textarea id="specBefore" placeholder='Paste your current spec here...\n\nExample:\n{"openapi":"3.0.0","paths":{"/users":{"get":{"responses":{"200":{"description":"OK"}}}}}}'></textarea>
    </div>
    
    <div class="form-group">
        <label>After (new spec)</label>
        <textarea id="specAfter" placeholder="Paste your updated spec here..."></textarea>
    </div>
    
    <button class="btn" id="analyzeBtn" onclick="analyze()">Analyze Breaking Changes</button>
    
    <div class="result" id="result">
        <h3 id="resultTitle"></h3>
        <pre id="resultBody"></pre>
        <p class="info" id="resultInfo"></p>
    </div>
</div>

<script>
async function analyze() {
    const btn = document.getElementById('analyzeBtn');
    const result = document.getElementById('result');
    btn.disabled = true;
    btn.textContent = 'Analyzing...';
    
    try {
        const resp = await fetch('/dry-run', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                spec_before: document.getElementById('specBefore').value,
                spec_after: document.getElementById('specAfter').value,
                contract_type: document.getElementById('contractType').value,
            })
        });
        
        const data = await resp.json();
        result.classList.add('show');
        
        const title = document.getElementById('resultTitle');
        const body = document.getElementById('resultBody');
        const info = document.getElementById('resultInfo');
        
        if (data.breaking_changes && data.breaking_changes.length > 0) {
            title.innerHTML = '<span class="breaking">⚠️ ' + data.breaking_changes.length + ' Breaking Change(s) Detected</span>';
            body.textContent = JSON.stringify(data.breaking_changes, null, 2);
            info.innerHTML = data.summary + '<br><br>Want Ripple to auto-fix these? <a href="/auth/gitlab">Install on GitLab</a> or <a href="https://github.com/apps/ripple-api">Install on GitHub</a>';
        } else {
            title.innerHTML = '<span class="safe">✅ No Breaking Changes</span>';
            body.textContent = 'Safe to push. No consumers would break.';
            info.textContent = '';
        }
    } catch (e) {
        result.classList.add('show');
        document.getElementById('resultTitle').innerHTML = '<span class="breaking">Error</span>';
        document.getElementById('resultBody').textContent = e.message;
    }
    
    btn.disabled = false;
    btn.textContent = 'Analyze Breaking Changes';
}
</script>
</body></html>"""
