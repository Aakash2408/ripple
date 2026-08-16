from __future__ import annotations
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
import re
import ssl
import base64
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import http.client
import socket

# Transient network faults worth retrying. RemoteDisconnected is the one that
# killed live runs: GitHub closes the connection under a burst of calls, and
# it is NOT an HTTPError so it previously escaped _github_api entirely.
_TRANSIENT_ERRORS = (
    URLError,
    http.client.RemoteDisconnected,
    http.client.IncompleteRead,
    http.client.BadStatusLine,
    ConnectionResetError,
    ConnectionAbortedError,
    socket.timeout,
    TimeoutError,
    OSError,          # covers ssl.SSLEOFError and assorted socket errors
)
_API_MAX_RETRIES = 3
_BACKOFF_BASE = 0.75  # seconds; 0.75, 1.5, 3.0

try:
    from fastapi import FastAPI, Request as FastAPIRequest, HTTPException
    from fastapi.responses import JSONResponse
except ImportError:
    print("ERROR: Install fastapi + uvicorn:")
    print("  pip install fastapi uvicorn")
    raise

from .diff_engine import diff_specs, BreakingChange, DiffResult
from .proto_diff import diff_proto
from .graphql_diff import diff_graphql
from .migration_diff import diff_schema
from .asyncapi_diff import diff_asyncapi
from .avro_diff import diff_avro
from .trpc_diff import diff_trpc
from .thrift_diff import diff_thrift
from .jsonschema_diff import diff_jsonschema
from .smithy_diff import diff_smithy
from .github_app_auth import (
    is_app_configured, get_installation_token,
    list_installation_repositories, AppAuthError,
)
from .consumer_finder import find_consumers, ConsumerMatch
from .fix_generator import generate_fix, GeneratedFix, _generate_with_template
from .pr_engine import CreatedPR
from .history_learner import HistoryLearner
from .consumer_graph import ConsumerGraph
from .multi_invoker import MultiInvokerDetector
from .playbook_engine import PlaybookEngine, EnsembleConsumerFinder
from .custom_playbooks import parse_ripple_config, RippleConfig, DEFAULT_TEMPLATE
from .confidence import format_pr_body, classify_confidence, should_create_pr
from .expand_contract import advise as expand_contract_advise, analyze_changes as ec_analyze
from .rate_limiter import get_rate_limiter, get_github_rate_tracker
from .retry_queue import get_retry_queue, should_retry, should_retry_error
from .gitlab_support import GitLabClient, parse_gitlab_push_event, verify_gitlab_signature, create_fix_mr
from .bitbucket_support import BitbucketClient, parse_bitbucket_push_event, verify_bitbucket_signature, create_fix_pr as bb_create_fix_pr
from .dashboard import router as dashboard_router
from .gitlab_setup import router as gitlab_setup_router
from .gitlab_oauth import router as gitlab_oauth_router
from .bitbucket_oauth import router as bitbucket_oauth_router
from .dry_run import router as dry_run_router
from . import token_store

app = FastAPI(title="Ripple", description="Self-maintaining APIs")

# Initialize persistent token store (loads saved tokens from disk)
token_store.init()

# CORS — allow PropBench UI (GitHub Pages) to POST results
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://aakash2408.github.io", "http://localhost:8080", "*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(dashboard_router)
app.include_router(gitlab_setup_router)
app.include_router(gitlab_oauth_router)
app.include_router(bitbucket_oauth_router)
app.include_router(dry_run_router)

# SSL context for GitHub API calls (Amazon dev desktop fix)
from .tls import make_ssl_context, describe as _describe_tls

# TLS context with certificate verification ENABLED. This previously set
# check_hostname=False + verify_mode=CERT_NONE under a comment calling it an
# "SSL fix", which actually disabled TLS entirely. These requests now carry
# GitHub App installation tokens that grant write access to customer repos,
# so accepting any certificate is not acceptable. See app/tls.py.
SSL_CTX = make_ssl_context()

# Per-org learned knowledge (persists across requests)
_org_learners: dict[str, HistoryLearner] = {}
_org_graphs: dict[str, ConsumerGraph] = {}
_org_ensembles: dict[str, EnsembleConsumerFinder] = {}
_org_configs: dict[str, RippleConfig] = {}

# Shared engines
_playbook_engine = PlaybookEngine()


def get_learner(org: str) -> HistoryLearner:
    """Get or create a HistoryLearner for an org."""
    if org not in _org_learners:
        _org_learners[org] = HistoryLearner(min_confidence=0.2, min_co_changes=2)
    return _org_learners[org]


def get_graph(org: str) -> ConsumerGraph:
    """Get or create a ConsumerGraph for an org."""
    if org not in _org_graphs:
        _org_graphs[org] = ConsumerGraph(org)
    return _org_graphs[org]


def get_ensemble(org: str) -> EnsembleConsumerFinder:
    """Get or create the full EnsembleConsumerFinder for an org."""
    if org not in _org_ensembles:
        learner = get_learner(org)
        detector = MultiInvokerDetector(learner=learner)
        _org_ensembles[org] = EnsembleConsumerFinder(
            playbook_engine=_playbook_engine,
            learner=learner,
            detector=detector,
        )
    return _org_ensembles[org]


def get_config(org: str) -> RippleConfig:
    """Get the custom config for an org (from .ripple.yaml)."""
    if org not in _org_configs:
        _org_configs[org] = RippleConfig()  # default until loaded
    return _org_configs[org]


# === Health check ===

@app.get("/")
async def root():
    return {"status": "ok", "service": "ripple", "version": "0.1.0"}


# --- Activity log ---
# Delegates to app/activity.py, the single shared store. There used to be a
# second, disconnected _activity_log in dashboard.py that nothing wrote to,
# which is why the dashboard always showed zeros.
import time as _time
from . import activity as _activity


def _log_activity(action: str, details: dict = None):
    """Record an activity event in the shared, persisted store."""
    try:
        _activity.record(action, details)
    except Exception:
        # Telemetry must never break the pipeline it is observing.
        pass


class _ActivityLogProxy:
    """Backwards-compatible read-only view over the shared store.

    Existing code (and tests) treat _activity_log as a list. Keeping that
    interface avoids touching ~40 call sites while removing the duplicate
    state underneath.
    """

    def __iter__(self):
        return iter(_activity.all_events())

    def __len__(self):
        return len(_activity.all_events())

    def __getitem__(self, item):
        return _activity.all_events()[item]

    def __bool__(self):
        return bool(_activity.all_events())


_activity_log = _ActivityLogProxy()


@app.get("/health")
async def health():
    return {"healthy": True}


@app.get("/health/storage")
async def health_storage():
    """Report where state is stored and whether it is DURABLE.

    Railway container filesystems are ephemeral unless a volume is mounted,
    so activity/tokens written to /app/data survive restarts but are lost on
    every redeploy. That is exactly what erased the successful 08:49 run
    before it could be inspected.

    Rather than leaving that as an assumption, this reports it: `durable`
    is true only when the resolved directory is a real mount point (a
    Railway volume) rather than container-local scratch.
    """
    from . import activity as _act
    from .tls import describe as _tls_describe

    store_dir = str(_act._store_dir())
    path = _act._store_path()

    # A mounted volume shows up as a distinct device from the container root.
    durable = False
    reason = "container-local (lost on redeploy)"
    try:
        if os.path.ismount(store_dir):
            durable, reason = True, "volume mount detected"
        else:
            root_dev = os.stat("/").st_dev
            if os.stat(store_dir).st_dev != root_dev:
                durable, reason = True, "separate device (volume)"
    except OSError as e:
        reason = f"could not determine: {type(e).__name__}"

    return {
        "healthy": True,
        "storage": {
            "dir": store_dir,
            "activity_file": str(path),
            "activity_exists": path.exists(),
            "event_count": len(_act.all_events()),
            "durable": durable,
            "durability_reason": reason,
            "hint": None if durable else (
                "Mount a Railway volume at /app/data (or set RIPPLE_DATA_DIR "
                "to a mounted path) so activity and tokens survive redeploys."
            ),
        },
        "tls": _tls_describe(),
    }


@app.get("/logs/recent")
async def recent_logs():
    """Return recent activity log for debugging (last 50 events)."""
    return {"count": len(_activity_log), "logs": _activity_log[-30:]}


@app.get("/test-llm")
async def test_llm():
    """Quick test that the Anthropic API key works. Calls Claude with a minimal prompt."""
    import os
    
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"status": "error", "message": "ANTHROPIC_API_KEY not set"}
    
    try:
        import anthropic
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=20,
            messages=[{"role": "user", "content": "Reply with exactly: LLM_OK"}],
        )
        reply = response.content[0].text.strip()
        return {
            "status": "ok",
            "model": "claude-sonnet-4-20250514",
            "response": reply,
            "key_prefix": api_key[:12] + "...",
            "tokens_used": response.usage.input_tokens + response.usage.output_tokens,
        }
    except ImportError:
        return {"status": "error", "message": "anthropic package not installed"}
    except Exception as e:
        return {"status": "error", "message": str(e)[:200]}


# === GitHub App Installation Event ===

@app.post("/webhook/install")
async def github_install_webhook(request: FastAPIRequest):
    """
    Handles GitHub App installation events.
    When installed on a repo/org:
    1. Scans git history for co-change patterns (History Learner)
    2. Indexes merged PRs into RAG store (fix patterns)
    3. Pre-loads PropBench general knowledge
    """
    body = await request.body()
    payload = json.loads(body)
    event_type = request.headers.get("X-GitHub-Event", "")
    
    if event_type == "installation" and payload.get("action") == "created":
        repos = payload.get("repositories", [])
        org = payload.get("installation", {}).get("account", {}).get("login", "unknown")
        installation_id = payload.get("installation", {}).get("id")
        
        learner = get_learner(org)
        token = _get_token(installation_id)
        results = []
        
        # Initialize RAG store for this org
        from .rag_engine import RagStore, index_from_propbench
        store = RagStore(collection_name=f"ripple_{org}")
        
        # Pre-load PropBench general knowledge (882 patterns)
        try:
            propbench_dir = os.path.join(os.path.dirname(__file__), "..", "propbench_data")
            if os.path.exists(propbench_dir):
                pb_stats = index_from_propbench(propbench_dir, store)
                results.append({"source": "propbench", "status": "loaded", "patterns": pb_stats.get("indexed", 0)})
        except Exception as e:
            results.append({"source": "propbench", "status": "skipped", "reason": str(e)[:100]})
        
        # For each repo: scan merged PRs via GitHub API + index into RAG
        for repo in repos:
            repo_name = repo.get("full_name", "")
            try:
                # Scan merged PRs for fix patterns
                pr_patterns = _index_merged_prs(repo_name, token, store)
                results.append({
                    "repo": repo_name,
                    "status": "indexed",
                    "merged_prs_scanned": pr_patterns.get("prs_scanned", 0),
                    "patterns_extracted": pr_patterns.get("patterns_stored", 0),
                })
            except Exception as e:
                results.append({
                    "repo": repo_name,
                    "status": "error",
                    "reason": str(e)[:100],
                })
        
        return {
            "status": "installation_received",
            "org": org,
            "repos_to_learn": len(repos),
            "rag_store_size": store.count(),
            "results": results,
        }
    
    return {"status": "ignored"}


# === Learn endpoint (manually trigger learning for a repo) ===

@app.post("/learn")
async def learn_repo(request: FastAPIRequest):
    """
    Manually trigger learning for a specific repo.
    
    Body: {"repo": "owner/repo", "clone_url": "https://..."}
    
    In production, this clones the repo temporarily and scans history.
    For now, works with local repos (for demo/testing).
    """
    body = await request.body()
    payload = json.loads(body)
    
    repo = payload.get("repo", "")
    local_path = payload.get("local_path", "")  # For testing with local repos
    
    if not repo:
        return {"error": "repo is required"}
    
    org = repo.split("/")[0] if "/" in repo else "unknown"
    learner = get_learner(org)
    
    if local_path and os.path.isdir(local_path):
        stats = learner.learn_from_repo(local_path, since="12 months ago")
        return {
            "status": "learned",
            "repo": repo,
            "stats": stats,
            "learner": learner.stats(),
        }
    
    return {
        "status": "queued",
        "repo": repo,
        "message": "Learning will happen when repo is cloned (production feature)",
    }


# === Status endpoint (show what's been learned) ===

@app.get("/status/{org}")
async def org_status(org: str):
    """Show what Ripple has learned about an org."""
    learner = get_learner(org) if org in _org_learners else None
    graph = get_graph(org) if org in _org_graphs else None
    
    return {
        "org": org,
        "learner": learner.stats() if learner else "not initialized",
        "graph": graph.stats() if graph else "not initialized",
        "has_learned": org in _org_learners,
    }


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
    
    # Handle PR merge events → continuous learning
    if event_type == "pull_request" and payload.get("action") == "closed":
        pr = payload.get("pull_request", {})
        if pr.get("merged"):
            return _handle_pr_merged(payload, pr)
        else:
            return _handle_pr_rejected(payload, pr)
    
    # Only handle push events for detection pipeline
    _log_activity("webhook_received", {"event": event_type, "repo": payload.get("repository", {}).get("full_name", "?")})
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
        _log_activity("no_spec_files", {"commits": len(payload.get("commits", []))})
        return {"status": "ignored", "reason": "no spec files changed"}
    
    _log_activity("spec_files_found", {"count": len(spec_files), "files": [s[0] for s in spec_files]})
    
    # Rate limit check
    repo_full_name = payload["repository"]["full_name"]
    org = repo_full_name.split("/")[0] if "/" in repo_full_name else "unknown"
    limiter = get_rate_limiter()
    allowed, reason = limiter.check(org)
    if not allowed:
        _log_activity("rate_limited", {"org": org, "reason": reason})
        return {"status": "rate_limited", "reason": reason, "org": org}
    
    # Process each changed spec
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


# === Bitbucket Webhook ===

@app.post("/webhook/bitbucket")
async def bitbucket_webhook(request: FastAPIRequest):
    """
    Receives Bitbucket push events and triggers the Ripple pipeline.
    
    Setup: Repository → Settings → Webhooks → Add webhook
    URL: https://your-server/webhook/bitbucket
    Triggers: Repository Push
    """
    body = await request.body()
    payload = json.loads(body)
    
    # Verify signature
    secret = os.environ.get("BITBUCKET_WEBHOOK_SECRET", "")
    sig_header = request.headers.get("X-Hub-Signature", "")
    if not verify_bitbucket_signature(body, secret, sig_header):
        raise HTTPException(status_code=401, detail="Invalid Bitbucket signature")
    
    # Only handle push events
    event_type = request.headers.get("X-Event-Key", "")
    if event_type != "repo:push":
        return {"status": "ignored", "reason": f"event_type={event_type}"}
    
    # Parse event
    event = parse_bitbucket_push_event(payload)
    
    # Rate limit
    org = event["workspace"]
    limiter = get_rate_limiter()
    allowed, reason = limiter.check(org)
    if not allowed:
        return {"status": "rate_limited", "reason": reason}
    
    # Find spec files that changed
    # Bitbucket push events don't always include file lists — fetch diff
    client = BitbucketClient()
    
    if not event["before"] or not event["after"]:
        return {"status": "ignored", "reason": "missing before/after SHA"}
    
    # Process: fetch old/new spec, diff, find consumers, create PRs
    results = []
    
    # For now, check if any spec files were modified by comparing commits
    # Bitbucket requires fetching the diffstat
    diffstat = client._request(
        "GET",
        f"/repositories/{event['workspace']}/{event['repo_slug']}/diffstat/{event['before']}..{event['after']}"
    )
    
    spec_files = []
    if "values" in diffstat:
        for file_change in diffstat["values"]:
            file_path = file_change.get("new", {}).get("path", "") or file_change.get("old", {}).get("path", "")
            if _is_spec_file(file_path):
                spec_files.append(file_path)
    
    if not spec_files:
        return {"status": "ignored", "reason": "no spec files changed"}
    
    for spec_path in spec_files:
        old_content = client.get_file_at_commit(event["workspace"], event["repo_slug"], spec_path, event["before"])
        new_content = client.get_file_at_commit(event["workspace"], event["repo_slug"], spec_path, event["after"])
        
        if not old_content or not new_content:
            results.append({"spec": spec_path, "status": "error", "reason": "could not fetch versions"})
            continue
        
        # Route to correct diff engine
        contract_type = _detect_contract_type(spec_path)
        
        if contract_type == "proto":
            breaking_changes = diff_proto(old_content, new_content, file_path=spec_path)
        elif contract_type == "graphql":
            breaking_changes = diff_graphql(old_content, new_content, file_path=spec_path)
        elif contract_type == "database":
            breaking_changes = diff_schema(old_content, new_content, file_path=spec_path)
        elif contract_type == "asyncapi":
            breaking_changes = diff_asyncapi(old_content, new_content, file_path=spec_path)
        elif contract_type == "avro":
            breaking_changes = diff_avro(old_content, new_content, file_path=spec_path)
        elif contract_type == "trpc":
            breaking_changes = diff_trpc(old_content, new_content, file_path=spec_path)
        elif contract_type == "thrift":
            breaking_changes = diff_thrift(old_content, new_content, file_path=spec_path)
        elif contract_type == "jsonschema":
            breaking_changes = diff_jsonschema(old_content, new_content, file_path=spec_path)
        elif contract_type == "smithy":
            breaking_changes = diff_smithy(old_content, new_content, file_path=spec_path)
        else:
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
                f.write(old_content)
                old_path = f.name
            with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
                f.write(new_content)
                new_path = f.name
            diff_result = diff_specs(old_path, new_path)
            os.unlink(old_path)
            os.unlink(new_path)
            breaking_changes = diff_result.breaking_changes
        
        if not breaking_changes:
            results.append({"spec": spec_path, "status": "no_breaking_changes"})
            continue
        
        # Find consumers and create PRs
        prs_created = []
        for change in breaking_changes:
            consumers = client.search_code(event["workspace"], event["repo_slug"], change.path)
            for consumer in consumers[:5]:
                consumer_path = consumer.get("path", "")
                if consumer_path == spec_path or not consumer_path:
                    continue
                consumer_content = client.get_file(event["workspace"], event["repo_slug"], consumer_path)
                if consumer_content:
                    from .consumer_finder import ConsumerMatch
                    from .fix_generator import _generate_with_template
                    consumer_match = ConsumerMatch(
                        file_path=consumer_path, line_number=0, code_snippet="",
                        confidence="high", match_reason="Code search match",
                        language=_detect_lang(consumer_path),
                    )
                    fixed_code, explanation = _generate_fix_with_rag_fallback(consumer_content, consumer_match, change, org)
                    if fixed_code != consumer_content:
                        pr_url = bb_create_fix_pr(
                            client, event["workspace"], event["repo_slug"],
                            consumer_path, fixed_code,
                            f"Add required field '{change.field_name}' to {change.method} {change.path}",
                        )
                        if pr_url:
                            prs_created.append(pr_url)
                            limiter.record_pr_opened(org)
        
        results.append({
            "spec": spec_path,
            "status": "processed",
            "breaking_changes": len(breaking_changes),
            "prs_created": prs_created,
        })
    
    return {"status": "processed", "platform": "bitbucket", "results": results}


# === GitLab Webhook ===

@app.post("/webhook/gitlab")
async def gitlab_webhook(request: FastAPIRequest):
    """
    Receives GitLab push events and triggers the Ripple pipeline.
    
    Setup: Add webhook URL to GitLab project Settings → Webhooks
    URL: https://your-server/webhook/gitlab
    Trigger: Push events
    """
    body = await request.body()
    payload = json.loads(body)
    
    # Verify GitLab webhook token
    secret = os.environ.get("GITLAB_WEBHOOK_SECRET", "")
    token_header = request.headers.get("X-Gitlab-Token", "")
    if not verify_gitlab_signature(body, secret, token_header):
        raise HTTPException(status_code=401, detail="Invalid GitLab token")
    
    # Only handle push events
    event_type = request.headers.get("X-Gitlab-Event", "")
    if event_type != "Push Hook":
        return {"status": "ignored", "reason": f"event_type={event_type}"}
    
    # Parse the event
    event = parse_gitlab_push_event(payload)
    
    # Only handle default branch
    if event["ref"] != f"refs/heads/{event['default_branch']}":
        return {"status": "ignored", "reason": "not default branch"}
    
    # Rate limit check
    org = event["project_name"].split("/")[0] if "/" in event["project_name"] else "unknown"
    limiter = get_rate_limiter()
    allowed, reason = limiter.check(org)
    if not allowed:
        return {"status": "rate_limited", "reason": reason}
    
    # Find spec files in changed files
    spec_files = [f for f in event["changed_files"] if _is_spec_file(f)]
    if not spec_files:
        return {"status": "ignored", "reason": "no spec files changed"}
    
    # Process each spec change (reuses same diff engines)
    from .gitlab_oauth import get_token_for_project
    project_token = get_token_for_project(event["project_id"])
    # Fallback to env var if OAuth token not available
    gitlab_token = project_token or os.environ.get("GITLAB_TOKEN", "")
    client = GitLabClient(token=gitlab_token)
    results = []
    
    for spec_path in spec_files:
        # Handle 'before' being all zeros (new branch/first push)
        before_sha = event["before"]
        after_sha = event["after"]
        if before_sha == "0000000000000000000000000000000000000000":
            # Can't diff against nothing — skip (file was just created, not modified)
            results.append({"spec": spec_path, "status": "skipped", "reason": "new file (no previous version to diff)"})
            continue
        
        old_content = client.get_file_at_commit(event["project_id"], spec_path, before_sha)
        new_content = client.get_file_at_commit(event["project_id"], spec_path, after_sha)
        
        if not old_content or not new_content:
            # Try fetching new_content from default branch HEAD
            new_content = client.get_file(event["project_id"], spec_path, ref=event["default_branch"])
            if not old_content or not new_content:
                results.append({"spec": spec_path, "status": "error", "reason": "could not fetch versions"})
                continue
        
        # Route to correct diff engine
        contract_type = _detect_contract_type(spec_path)
        
        if contract_type == "proto":
            breaking_changes = diff_proto(old_content, new_content, file_path=spec_path)
        elif contract_type == "graphql":
            breaking_changes = diff_graphql(old_content, new_content, file_path=spec_path)
        elif contract_type == "database":
            breaking_changes = diff_schema(old_content, new_content, file_path=spec_path)
        elif contract_type == "asyncapi":
            breaking_changes = diff_asyncapi(old_content, new_content, file_path=spec_path)
        elif contract_type == "avro":
            breaking_changes = diff_avro(old_content, new_content, file_path=spec_path)
        elif contract_type == "trpc":
            breaking_changes = diff_trpc(old_content, new_content, file_path=spec_path)
        elif contract_type == "thrift":
            breaking_changes = diff_thrift(old_content, new_content, file_path=spec_path)
        elif contract_type == "jsonschema":
            breaking_changes = diff_jsonschema(old_content, new_content, file_path=spec_path)
        elif contract_type == "smithy":
            breaking_changes = diff_smithy(old_content, new_content, file_path=spec_path)
        else:
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
                f.write(old_content)
                old_path = f.name
            with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
                f.write(new_content)
                new_path = f.name
            diff_result = diff_specs(old_path, new_path)
            os.unlink(old_path)
            os.unlink(new_path)
            breaking_changes = diff_result.breaking_changes
        
        if not breaking_changes:
            results.append({"spec": spec_path, "status": "no_breaking_changes"})
            continue
        
        # Find consumers and create MRs
        mrs_created = []
        for change in breaking_changes:
            # Search for consumers in the same project
            consumers = client.search_code(event["project_id"], change.path)
            for consumer in consumers[:5]:
                consumer_path = consumer.get("path", "")
                if consumer_path == spec_path:
                    continue
                # Fetch consumer content
                consumer_content = client.get_file(event["project_id"], consumer_path)
                if consumer_content:
                    # Generate fix (reuse existing fix generator)
                    consumer_match = ConsumerMatch(
                        file_path=consumer_path, line_number=0, code_snippet="",
                        confidence="high", match_reason="Code search match",
                        language=_detect_lang(consumer_path),
                    )
                    fixed_code, explanation = _generate_fix_with_rag_fallback(consumer_content, consumer_match, change, org)
                    if fixed_code != consumer_content:
                        mr_url = create_fix_mr(
                            client, event["project_id"], consumer_path,
                            fixed_code, f"Add required field '{change.field_name}' to {change.method} {change.path}",
                            event["project_name"],
                        )
                        if mr_url:
                            mrs_created.append(mr_url)
                            limiter.record_pr_opened(org)
        
        results.append({
            "spec": spec_path,
            "status": "processed",
            "breaking_changes": len(breaking_changes),
            "mrs_created": mrs_created,
        })
    
    return {"status": "processed", "platform": "gitlab", "results": results}


# === Rate Limit Status ===

@app.get("/rate-limit/{org}")
async def rate_limit_status(org: str):
    """Check rate limit status for an org."""
    limiter = get_rate_limiter()
    return limiter.stats(org)


@app.get("/retry-queue")
async def retry_queue_status():
    """Check retry queue statistics."""
    queue = get_retry_queue()
    return queue.stats()


@app.get("/retry-queue/dead-letter")
async def retry_queue_dead_letter():
    """Inspect failed jobs that exhausted all retries."""
    queue = get_retry_queue()
    return {"dead_letter_jobs": queue.dead_letter_jobs()}


@app.post("/retry-queue/retry/{job_id}")
async def retry_dead_letter_job(job_id: str):
    """Move a dead-lettered job back to the queue for retry."""
    queue = get_retry_queue()
    success = queue.retry_dead_letter(job_id)
    if success:
        return {"status": "requeued", "job_id": job_id}
    return JSONResponse(status_code=404, content={"error": f"Job {job_id} not found in dead letter queue"})


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
    """Check if a file is an API contract (OpenAPI, Proto, GraphQL, or DB schema)."""
    lower = filepath.lower()
    
    # OpenAPI / Swagger
    openapi_indicators = [
        "openapi", "swagger", "api-spec", "api_spec",
        "spec.yaml", "spec.yml", "spec.json",
    ]
    if any(ind in lower for ind in openapi_indicators):
        return True
    if lower.endswith((".yaml", ".yml", ".json")) and "api" in lower:
        return True
    
    # Protobuf
    if lower.endswith(".proto"):
        return True
    
    # GraphQL
    if lower.endswith((".graphql", ".gql")):
        return True
    if "schema" in lower and lower.endswith(".graphql"):
        return True
    
    # Database schemas
    if lower.endswith((".sql",)) and any(x in lower for x in ["migration", "schema", "ddl"]):
        return True
    if "prisma" in lower and lower.endswith(".prisma"):
        return True
    if lower.endswith("schema.prisma"):
        return True
    
    # AsyncAPI
    if "asyncapi" in lower:
        return True
    if lower.endswith((".yaml", ".yml", ".json")) and any(x in lower for x in ["event", "message", "kafka", "sns", "sqs", "mqtt", "nats", "amqp"]):
        return True
    
    # Avro
    if lower.endswith(".avsc") or ("avro" in lower and lower.endswith(".json")):
        return True
    
    # tRPC
    if "trpc" in lower or "router" in lower and lower.endswith(".ts"):
        return True
    
    # Thrift
    if lower.endswith(".thrift"):
        return True
    
    # JSON Schema
    if lower.endswith(".schema.json") or "jsonschema" in lower or "json-schema" in lower:
        return True
    
    # Smithy
    if lower.endswith(".smithy"):
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
    3. Find consumers using ENSEMBLE (grep + playbooks + history + multi-invoker)
    4. Apply custom playbooks from .ripple.yaml
    5. Generate fixes + open PRs
    """
    try:
        return await _process_spec_change_inner(repo, spec_path, before_sha, after_sha, installation_id)
    except Exception as e:
        _log_activity("process_spec_error", {"repo": repo, "spec": spec_path, "error": str(e)[:200]})
        return {"status": "error", "reason": str(e)[:200]}


async def _process_spec_change_inner(repo, spec_path, before_sha, after_sha, installation_id=None):
    """Inner processing -- separated so we can catch all exceptions."""
    token = _get_token(installation_id)
    org = repo.split("/")[0] if "/" in repo else "unknown"
    
    _log_activity("processing_spec", {"repo": repo, "spec": spec_path, "contract": _detect_contract_type(spec_path)})
    
    # Load .ripple.yaml from the repo if we haven't already
    if org not in _org_configs:
        config_content = _fetch_file_at_sha(repo, ".ripple.yaml", after_sha, token)
        if config_content:
            parsed = parse_ripple_config(config_content)
            if parsed:
                _org_configs[org] = parsed
    
    config = get_config(org)
    ensemble = get_ensemble(org)
    
    # Fetch old and new spec content
    old_content = _fetch_file_at_sha(repo, spec_path, before_sha, token)
    new_content = _fetch_file_at_sha(repo, spec_path, after_sha, token)
    
    if not old_content or not new_content:
        _log_activity("fetch_failed", {"repo": repo, "spec": spec_path, "old": bool(old_content), "new": bool(new_content)})
        return {"status": "error", "reason": "could not fetch spec versions"}
    
    # Parse specs and diff -- route to correct engine based on contract type
    import tempfile
    
    contract_type = _detect_contract_type(spec_path)
    
    if contract_type == "proto":
        breaking_changes = diff_proto(old_content, new_content, file_path=spec_path)
    elif contract_type == "graphql":
        breaking_changes = diff_graphql(old_content, new_content, file_path=spec_path)
    elif contract_type == "database":
        breaking_changes = diff_schema(old_content, new_content, file_path=spec_path)
    elif contract_type == "asyncapi":
        breaking_changes = diff_asyncapi(old_content, new_content, file_path=spec_path)
    elif contract_type == "avro":
        breaking_changes = diff_avro(old_content, new_content, file_path=spec_path)
    elif contract_type == "trpc":
        breaking_changes = diff_trpc(old_content, new_content, file_path=spec_path)
    elif contract_type == "thrift":
        breaking_changes = diff_thrift(old_content, new_content, file_path=spec_path)
    elif contract_type == "jsonschema":
        breaking_changes = diff_jsonschema(old_content, new_content, file_path=spec_path)
    elif contract_type == "smithy":
        breaking_changes = diff_smithy(old_content, new_content, file_path=spec_path)
    else:
        # OpenAPI — needs temp files (legacy interface)
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(old_content)
            old_path = f.name
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(new_content)
            new_path = f.name
        diff_result = diff_specs(old_path, new_path)
        os.unlink(old_path)
        os.unlink(new_path)
        breaking_changes = diff_result.breaking_changes
    
    if not breaking_changes:
        _log_activity("no_breaking_changes", {"spec": spec_path})
        return {"status": "no_breaking_changes", "spec": spec_path}
    
    _log_activity("breaking_changes_detected", {
        "spec": spec_path,
        "count": len(breaking_changes),
        "changes": [{"type": c.change_type, "field": c.field_name} for c in breaking_changes[:5]],
    })
    
    # Find consumer repos (from GitHub App installation)
    # Find consumer repos. With App auth this is the authoritative
    # installation scope, not a guess at which repos might be consumers.
    consumer_repos = _find_consumer_repos(repo, token, installation_id)
    _log_activity("consumer_repos_found", {"count": len(consumer_repos), "repos": consumer_repos[:5]})
    
    # For each breaking change, use ENSEMBLE to find consumers
    prs_created = []
    warnings = []
    ensemble_stats = {"grep": 0, "playbook": 0, "history": 0, "multi_invoker": 0, "custom": 0}
    
    for change in breaking_changes:
        # Determine contract type from spec file
        contract_type = _detect_contract_type(spec_path)
        change_type = _map_change_type(change)
        
        # --- ENSEMBLE CONSUMER FINDING ---
        # Step 1: Grep-based search across repos.
        # Searched ONCE and cached -- this used to run twice (once here,
        # once in the fix loop below), doubling GitHub API calls.
        consumer_files_by_repo = {}
        grep_results = []
        # Shared API-call budget for this change. The tree fallback costs one
        # call per candidate file, so without a ceiling a wide installation
        # scope issues hundreds of rapid requests and GitHub drops the
        # connection mid-run.
        tree_budget = {"remaining": int(os.environ.get("RIPPLE_TREE_CALL_BUDGET", "150"))}
        for consumer_repo in consumer_repos:
            consumer_files = _search_repo_for_consumers(
                consumer_repo, change, token, exclude_path=spec_path, budget=tree_budget
            )
            consumer_files_by_repo[consumer_repo] = consumer_files
            for file_path, content, _detect_conf in consumer_files:
                grep_results.append(file_path)
        
        _log_activity("consumer_search_complete", {
            "field": change.field_name,
            "repos_searched": len(consumer_repos),
            "files_found": len(grep_results),
            "files": grep_results[:5],
        })
        
        # Step 2: Ensemble prediction (grep + playbook + history + multi-invoker)
        ensemble_predictions = ensemble.find_all_consumers(
            changed_file=spec_path,
            contract_type=contract_type,
            change_type=change_type,
            grep_results=grep_results,
        )
        
        # Step 3: Custom playbook predictions
        custom_predictions = config.get_predictions_for_change(spec_path, change_type)
        for cp in custom_predictions:
            ensemble_stats["custom"] += 1
        
        # Step 4: Multi-invoker warning
        detector = MultiInvokerDetector(learner=get_learner(org))
        mi_warning = detector.check(spec_path)
        if mi_warning and mi_warning.risk_level in ("high", "medium"):
            warnings.append({
                "type": "multi_invoker",
                "file": spec_path,
                "risk": mi_warning.risk_level,
                "message": mi_warning.message,
                "consumers": len(mi_warning.invokers),
            })
        
        # Track sources for stats
        for pred in ensemble_predictions:
            for source in pred.get("sources", []):
                if source in ensemble_stats:
                    ensemble_stats[source] += 1
        
        # Step 5: Generate fixes for high-confidence consumers
        min_confidence = config.min_confidence
        high_confidence = [
            p for p in ensemble_predictions
            if p["confidence"] >= min_confidence and not p["file"].startswith("*")
        ]
        
        for consumer_repo in consumer_repos:
            consumer_files = consumer_files_by_repo.get(consumer_repo, [])
            
            for consumer_file, consumer_content, detector_confidence in consumer_files:
                # Check ignore patterns
                if config.should_ignore(consumer_file):
                    continue
                
                # Check PR limit
                if len(prs_created) >= config.max_prs_per_push:
                    break
                
                consumer = ConsumerMatch(
                    file_path=consumer_file,
                    line_number=0,
                    code_snippet="",
                    confidence="high",
                    match_reason="Ensemble prediction",
                    language=_detect_lang(consumer_file),
                )
                
                fixed_code, explanation = _generate_fix_with_rag_fallback(
                    consumer_content, consumer, change, org
                )
                
                _log_activity("fix_generated", {
                    "repo": consumer_repo,
                    "file": consumer_file,
                    "changed": fixed_code != consumer_content,
                    "source": explanation[:60] if explanation else "",
                })
                
                if fixed_code != consumer_content:
                    # Find this file's confidence from ensemble predictions
                    # Start from the confidence the consumer detector actually
                    # computed for THIS file (0.95 for a struct-field match,
                    # 0.70 for a weaker reference). This used to be a hardcoded
                    # 0.7 because the detector's score was discarded, so every
                    # PR claimed the same number regardless of match strength.
                    file_confidence = detector_confidence
                    # Record how the fix was actually produced, so the PR body
                    # does not mislabel a deterministic template fix as
                    # LLM-generated.
                    fix_source = "template" if "[template]" in explanation else (
                        "rag" if "[RAG" in explanation else "llm"
                    )
                    file_sources = ["grep", fix_source]
                    file_reasons = [
                        f"Field reference detected in {_detect_lang(consumer_file)} "
                        f"source (detector confidence {detector_confidence:.2f})"
                    ]
                    for pred in ensemble_predictions:
                        if pred.get("file") == consumer_file or consumer_file.endswith(pred.get("file", "")):
                            # Ensemble carries co-change history, which is a
                            # stronger signal than a static match -- prefer it.
                            file_confidence = max(pred["confidence"], detector_confidence)
                            file_sources = pred.get("sources", ["grep"]) + [fix_source]
                            file_reasons = pred.get("reasons", file_reasons)
                            break
                    
                    # Only create PR if confidence is high enough
                    if not should_create_pr(file_confidence, min_confidence):
                        _log_activity("pr_skipped_low_confidence", {
                            "file": consumer_file,
                            "confidence": file_confidence,
                            "min_required": min_confidence,
                        })
                        continue
                    
                    pr_url = _create_fix_pr(
                        consumer_repo, consumer_file,
                        fixed_code, change, repo, token,
                        confidence=file_confidence,
                        sources=file_sources,
                        reasons=file_reasons,
                        all_predictions=ensemble_predictions[:5],
                    )
                    _log_activity("pr_result", {
                        "repo": consumer_repo,
                        "file": consumer_file,
                        "url": pr_url or "FAILED",
                    })
                    if pr_url:
                        prs_created.append(pr_url)
                        # Track for lifecycle (pending -> merged -> reverted)
                        try:
                            from .pr_lifecycle import (
                                SourceChange, TrackedFixPR, UpstreamStatus,
                                track_fix_pr, LABEL_PENDING, LABEL_AUTO_FIX
                            )
                            source = SourceChange(
                                repo=repo,
                                commit_sha=(after_sha or "")[:12],
                                pr_number=None,
                                pr_url=None,
                                title=f"{change.change_type}: {change.field_name}",
                                status=UpstreamStatus.PENDING,
                            )
                            fix = TrackedFixPR(
                                repo=consumer_repo,
                                pr_number=0,  # extracted from URL if needed
                                pr_url=pr_url,
                                source=source,
                                labels=[LABEL_AUTO_FIX, LABEL_PENDING],
                            )
                            track_fix_pr(source, fix)
                        except Exception as e:
                            # Lifecycle tracking is optional, but it must not
                            # be INVISIBLE: an undefined `event` reference
                            # lived in this exact block for weeks, so the
                            # pending -> merged -> reverted state machine had
                            # never once run and nothing reported it.
                            _log_activity("lifecycle_tracking_failed", {
                                "repo": consumer_repo,
                                "pr": pr_url,
                                "err": f"{type(e).__name__}: {str(e)[:160]}",
                            })
    
    # Update consumer graph with observations
    graph = get_graph(org)
    
    # Expand+Contract analysis — suggest safer migration patterns
    ec_result = ec_analyze(breaking_changes)
    
    return {
        "status": "processed",
        "spec": spec_path,
        "breaking_changes": len(breaking_changes),
        "prs_created": prs_created,
        "ensemble_stats": ensemble_stats,
        "warnings": warnings,
        "config_loaded": org in _org_configs and bool(_org_configs[org].playbooks),
        "min_confidence": config.min_confidence,
        "expand_contract": {
            "avoidable": ec_result["avoidable"],
            "unavoidable": ec_result["unavoidable"],
            "summary": ec_result["summary"],
        },
    }


# === GitHub API Helpers ===

def _detect_contract_type(filepath: str) -> str:
    """Detect contract type from file path."""
    lower = filepath.lower()
    if any(x in lower for x in [".proto"]):
        return "proto"
    if any(x in lower for x in ["graphql", ".gql"]):
        return "graphql"
    if any(x in lower for x in [".sql", "prisma", "migration"]):
        return "database"
    if "asyncapi" in lower or any(x in lower for x in ["event", "kafka", "sns", "sqs", "mqtt", "nats", "amqp"]):
        return "asyncapi"
    if lower.endswith(".avsc") or "avro" in lower:
        return "avro"
    if "trpc" in lower or ("router" in lower and lower.endswith(".ts")):
        return "trpc"
    if lower.endswith(".thrift"):
        return "thrift"
    if lower.endswith(".schema.json") or "jsonschema" in lower or "json-schema" in lower:
        return "jsonschema"
    if lower.endswith(".smithy"):
        return "smithy"
    return "openapi"


def _map_change_type(change: BreakingChange) -> str:
    """Map a BreakingChange to a playbook change_type string."""
    change_str = str(change.change_type).lower() if hasattr(change, 'change_type') else ""
    if "added" in change_str or "required" in change_str:
        return "added_required_field"
    if "removed" in change_str:
        return "removed_field"
    if "type" in change_str:
        return "field_type_changed"
    if "rename" in change_str:
        return "field_renamed"
    return "added_required_field"  # default


@app.get("/config/template")
async def get_config_template():
    """Return the default .ripple.yaml template for users to customize."""
    from .custom_playbooks import DEFAULT_TEMPLATE
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(content=DEFAULT_TEMPLATE, media_type="text/yaml")


# === PropBench Human Baseline Collection ===

_propbench_results: list[dict] = []

@app.post("/propbench/submit")
async def submit_propbench_results(request: FastAPIRequest):
    """
    Receive PropBench human baseline results from the UI.
    Auto-called when participant finishes all challenges.
    """
    body = await request.body()
    data = json.loads(body)
    
    # Validate minimal structure
    if "session_id" not in data or "results" not in data:
        return JSONResponse(status_code=400, content={"error": "Missing session_id or results"})
    
    _propbench_results.append(data)
    
    participant = data.get("participant", {})
    n_challenges = len(data.get("results", []))
    
    return {
        "status": "saved",
        "participant": participant.get("name", "anonymous"),
        "challenges_completed": n_challenges,
        "total_submissions": len(_propbench_results),
        "message": "Thank you! Your results have been recorded.",
    }


@app.get("/propbench/results")
async def get_propbench_results():
    """View all collected PropBench human baseline results."""
    summaries = []
    for r in _propbench_results:
        participant = r.get("participant", {})
        results = r.get("results", [])
        completed = [x for x in results if not x.get("skipped")]
        avg_recall = sum(x.get("recall", 0) for x in completed) / len(completed) if completed else 0
        avg_precision = sum(x.get("precision", 0) for x in completed) / len(completed) if completed else 0
        
        summaries.append({
            "name": participant.get("name", "anonymous"),
            "role": participant.get("role", "unknown"),
            "challenges": len(completed),
            "skipped": len(results) - len(completed),
            "avg_recall": f"{avg_recall:.0%}",
            "avg_precision": f"{avg_precision:.0%}",
            "timestamp": r.get("timestamp", ""),
        })
    
    return {
        "total_participants": len(_propbench_results),
        "submissions": summaries,
    }


def _get_token(installation_id: int = None) -> str:
    """Get a GitHub token for this installation.

    Prefers a real GitHub App installation token, which is scoped to the
    repositories the customer actually granted, expires in an hour, and is
    minted per installation (so the service is multi-tenant).

    Falls back to GITHUB_TOKEN for local development and single-user
    self-hosting, where no App installation exists. That fallback is a
    supported mode, not a shortcut -- but it cannot scope discovery, so
    _find_consumer_repos reports when it is active.
    """
    if installation_id and is_app_configured():
        try:
            return get_installation_token(installation_id)
        except AppAuthError as e:
            # Loud, not silent: a misconfigured App would otherwise look
            # exactly like "this repo has no consumers".
            _log_activity("app_auth_failed", {
                "installation_id": installation_id,
                "err": str(e)[:200],
                "falling_back_to_pat": bool(os.environ.get("GITHUB_TOKEN")),
            })
    return os.environ.get("GITHUB_TOKEN", "")


def _github_api(method: str, path: str, token: str, data: dict = None,
                _attempt: int = 0) -> dict:
    """Make a GitHub API request, retrying transient failures.

    Previously only HTTPError was caught, so a dropped connection
    (http.client.RemoteDisconnected, socket timeout, URLError) propagated
    out and killed the ENTIRE spec run with
    "Remote end closed connection without response".

    That is easy to hit: the tree-walk fallback issues up to 41 calls per
    repo across every repo in scope, and GitHub starts closing connections
    under that burst. A single blip on call 200 of 370 should not discard
    all the work, so transient faults are retried with backoff and only
    become errors after the budget is exhausted.

    Retryable: connection resets, timeouts, 429, and 5xx.
    Not retryable: 404, 422 and friends -- retrying cannot change those.
    """
    url = f"https://api.github.com{path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "ripple-app",
    }

    body = json.dumps(data).encode() if data else None
    if body:
        headers["Content-Type"] = "application/json"

    req = Request(url, data=body, headers=headers, method=method)

    try:
        with urlopen(req, timeout=20, context=SSL_CTX) as resp:
            raw = resp.read().decode()
            # DELETE and some PUT/PATCH endpoints return 204 No Content with
            # an empty body -- json.loads("") raises JSONDecodeError.
            if not raw.strip():
                return {"status": getattr(resp, "status", 204)}
            try:
                return json.loads(raw)
            except ValueError:
                return {"status": getattr(resp, "status", 200), "raw": raw[:200]}

    except HTTPError as e:
        error_body = e.read().decode() if hasattr(e, 'read') else ""
        # Secondary rate limits and server errors are worth retrying.
        if e.code in (429, 500, 502, 503, 504) and _attempt < _API_MAX_RETRIES:
            delay = _retry_delay(e, _attempt)
            _time.sleep(delay)
            return _github_api(method, path, token, data, _attempt + 1)
        return {"error": e.code, "message": error_body[:200]}

    except _TRANSIENT_ERRORS as e:
        if _attempt < _API_MAX_RETRIES:
            _time.sleep(_BACKOFF_BASE * (2 ** _attempt))
            return _github_api(method, path, token, data, _attempt + 1)
        _log_activity("api_transient_failed", {
            "path": path[:80],
            "err": f"{type(e).__name__}: {str(e)[:120]}",
            "attempts": _attempt + 1,
        })
        return {"error": "transient", "message": f"{type(e).__name__}: {str(e)[:160]}"}


def _retry_delay(err, attempt: int) -> float:
    """Honour Retry-After / rate-limit reset when GitHub provides it."""
    try:
        retry_after = err.headers.get("Retry-After")
        if retry_after:
            return min(float(retry_after), 30.0)
        reset = err.headers.get("X-RateLimit-Reset")
        if reset:
            wait = float(reset) - _time.time()
            if 0 < wait <= 30:
                return wait
    except (AttributeError, TypeError, ValueError):
        pass
    return _BACKOFF_BASE * (2 ** attempt)


def _fetch_file_at_sha(repo: str, path: str, sha: str, token: str) -> str:
    """Fetch file content at a specific commit SHA."""
    data = _github_api("GET", f"/repos/{repo}/contents/{path}?ref={sha}", token)
    if "content" in data:
        return base64.b64decode(data["content"]).decode()
    return ""


def _handle_pr_merged(payload: dict, pr: dict) -> dict:
    """
    When a Ripple-generated PR gets merged, learn from it.
    This is how Ripple gets smarter over time — every merge confirms a pattern.
    """
    # Only learn from Ripple's own PRs
    pr_body = pr.get("body", "")
    if "Generated by" not in pr_body and "Ripple" not in pr_body:
        return {"status": "ignored", "reason": "not a Ripple PR"}
    
    repo = payload.get("repository", {}).get("full_name", "")
    org = repo.split("/")[0] if "/" in repo else "unknown"
    
    try:
        from .rag_engine import RagStore, FixExample
        from .rag_retriever import learn_from_merged_pr
        
        store = RagStore(collection_name=f"ripple_{org}")
        
        # Extract fix info from PR
        title = pr.get("title", "")
        files_changed = [f.get("filename", "") for f in pr.get("files", [])] if "files" in pr else []
        
        # Record the successful pattern
        learn_from_merged_pr(
            trigger_diff=title,
            fix_diff=f"Merged: {title} ({len(files_changed)} files)",
            language=_detect_lang(files_changed[0]) if files_changed else "unknown",
            field_name=_extract_field_from_title(title),
            change_type=_extract_change_type_from_title(title),
            store=store,
        )
        
        _log_activity("pr_merged_learned", {"pr": pr.get("number"), "repo": repo})
        return {
            "status": "learned",
            "pr": pr.get("number"),
            "repo": repo,
            "message": f"Ripple learned from merged PR #{pr.get('number')}. Pattern confidence boosted.",
        }
    except Exception as e:
        return {"status": "learn_error", "reason": str(e)[:100]}


def _handle_pr_rejected(payload: dict, pr: dict) -> dict:
    """
    When a Ripple-generated PR gets closed without merge, record as negative signal.
    This reduces confidence in the pattern that generated it.
    """
    pr_body = pr.get("body", "")
    if "Generated by" not in pr_body and "Ripple" not in pr_body:
        return {"status": "ignored", "reason": "not a Ripple PR"}
    
    repo = payload.get("repository", {}).get("full_name", "")
    org = repo.split("/")[0] if "/" in repo else "unknown"
    
    try:
        from .rag_engine import RagStore
        from .rag_retriever import learn_from_rejected_pr
        
        store = RagStore(collection_name=f"ripple_{org}")
        title = pr.get("title", "")
        
        learn_from_rejected_pr(
            trigger_diff=title,
            field_name=_extract_field_from_title(title),
            change_type=_extract_change_type_from_title(title),
            store=store,
        )
        
        _log_activity("pr_rejected_learned", {"pr": pr.get("number"), "repo": repo})
        return {
            "status": "learned_negative",
            "pr": pr.get("number"),
            "repo": repo,
            "message": f"Pattern confidence reduced for rejected PR #{pr.get('number')}.",
        }
    except Exception as e:
        return {"status": "learn_error", "reason": str(e)[:100]}


def _extract_field_from_title(title: str) -> str:
    """Extract field name from PR title like 'fix: Add required field phone to POST /users'."""
    import re
    match = re.search(r"field['\s]+(\w+)", title, re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"remove[ds]?\s+(\w+)", title, re.IGNORECASE)
    if match:
        return match.group(1)
    return ""


def _extract_change_type_from_title(title: str) -> str:
    """Extract change type from PR title."""
    title_lower = title.lower()
    if "remove" in title_lower or "delet" in title_lower:
        return "field_removed"
    if "add" in title_lower or "require" in title_lower:
        return "added_required_field"
    if "rename" in title_lower:
        return "field_renamed"
    if "type" in title_lower or "chang" in title_lower:
        return "field_type_changed"
    return "unknown"


def _generate_fix_with_rag_fallback(content: str, consumer, change, org: str = "") -> tuple[str, str]:
    """
    Generate a fix using the RAG-first stack:
    1. RAG exact match (similarity > 0.7) → apply learned pattern
    2. RAG cluster archetype (score > 0.5) → apply archetype strategy
    3. Fix templates (deterministic, per-language) → regex-based
    4. Claude LLM (ONLY if 1-3 all fail) → last resort
    
    Returns (fixed_code, explanation).
    """
    # Try RAG first
    try:
        from .rag_engine import RagStore
        from .rag_retriever import generate_fix_rag
        
        store = RagStore(collection_name=f"ripple_{org}" if org else "ripple_default")
        
        change_description = f"{change.change_type}: {change.field_name} in {change.method} {change.path}"
        language = consumer.language or _detect_lang(consumer.file_path)
        
        result = generate_fix_rag(
            code=content,
            file_path=consumer.file_path,
            field_name=change.field_name,
            change_type=change.change_type,
            change_description=change_description,
            store=store,
        )
        
        if result and result.fixed_code != content:
            return result.fixed_code, f"[RAG/{result.source_type}] {result.explanation}"
    except Exception as e:
        # RAG is layers 1-2 of a 4-layer fix stack (~1000 lines across
        # rag_engine.py + rag_retriever.py). Swallowing this silently meant
        # the entire retrieval subsystem could be broken while the pipeline
        # looked healthy -- every PR would just say [template] and nobody
        # would know the learned-pattern path had stopped working.
        _log_activity("rag_unavailable", {
            "file": getattr(consumer, "file_path", ""),
            "err": f"{type(e).__name__}: {str(e)[:160]}",
            "falling_back_to": "template",
        })
    
    # Fallback to template engine
    fixed_code, explanation = _generate_with_template(content, consumer, change)
    if fixed_code != content:
        # NOTE: deliberately NOT logged here. The caller logs fix_generated
        # with repo + changed context; logging in both places double-counted
        # every fix (handler.go x2, UserClient.ts x2, ...) and inflated the
        # dashboard's fixes_generated counter.
        return fixed_code, f"[template] {explanation}"
    
    return content, ""


def _index_merged_prs(repo: str, token: str, store, platform: str = "github", max_prs: int = 100) -> dict:
    """
    Scan merged PRs/MRs from any platform and index fix patterns into RAG store.
    
    Supports: GitHub, GitLab, Bitbucket, and any platform via adapter pattern.
    Looks for PRs where a spec/contract file changed alongside consumer files.
    """
    from .rag_engine import FixExample
    from .smart_consumer_finder import generate_variants
    
    prs_scanned = 0
    patterns_stored = 0
    
    # Spec file patterns (files that trigger propagation)
    spec_patterns = [
        ".proto", "openapi.yaml", "openapi.json", "swagger.yaml", "swagger.json",
        "schema.graphql", ".graphql", "schema.prisma", "asyncapi.yaml",
        ".avro", ".thrift", ".smithy", "trpc.ts",
    ]
    
    try:
        if platform == "github":
            merged_prs = _fetch_github_merged_prs(repo, token, max_prs)
        elif platform == "gitlab":
            merged_prs = _fetch_gitlab_merged_mrs(repo, token, max_prs)
        elif platform == "bitbucket":
            merged_prs = _fetch_bitbucket_merged_prs(repo, token, max_prs)
        else:
            # Self-hosted / generic -- use git log if available
            merged_prs = _fetch_git_log_prs(repo, max_prs)
    except Exception:
        return {"prs_scanned": 0, "patterns_stored": 0}
    
    for pr in merged_prs:
        prs_scanned += 1
        files_changed = pr.get("files", [])
        
        # Find spec files in this PR
        spec_files = [f for f in files_changed if any(f.endswith(p) or p in f for p in spec_patterns)]
        consumer_files = [f for f in files_changed if f not in spec_files and _is_code_file(f)]
        
        if spec_files and consumer_files:
            # This PR changed a spec + consumers → it's a propagation example
            for spec_file in spec_files:
                example = FixExample(
                    trigger_description=f"Changed {spec_file} in PR #{pr.get('number', '?')}: {pr.get('title', '')}",
                    trigger_file=spec_file,
                    trigger_diff=pr.get("title", ""),
                    fix_file=consumer_files[0],
                    fix_diff=f"Modified {len(consumer_files)} consumer file(s): {', '.join(consumer_files[:5])}",
                    language=_detect_lang(consumer_files[0]),
                    change_type="unknown",  # Would need diff analysis to determine
                    field_name="",
                )
                store.add_example(example)
                patterns_stored += 1
    
    return {"prs_scanned": prs_scanned, "patterns_stored": patterns_stored}


def _fetch_github_merged_prs(repo: str, token: str, max_prs: int) -> list[dict]:
    """Fetch merged PRs from GitHub API."""
    prs = []
    data = _github_api("GET", f"/repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page={min(max_prs, 100)}", token)
    
    if not isinstance(data, list):
        return []
    
    for pr_data in data:
        if not pr_data.get("merged_at"):
            continue  # Only merged PRs
        
        # Fetch files changed in this PR
        files_data = _github_api("GET", f"/repos/{repo}/pulls/{pr_data['number']}/files", token)
        files = [f["filename"] for f in (files_data if isinstance(files_data, list) else [])]
        
        prs.append({
            "number": pr_data["number"],
            "title": pr_data.get("title", ""),
            "files": files,
            "merged_at": pr_data.get("merged_at", ""),
        })
        
        if len(prs) >= max_prs:
            break
    
    return prs


def _fetch_gitlab_merged_mrs(repo: str, token: str, max_prs: int) -> list[dict]:
    """Fetch merged MRs from GitLab API."""
    import urllib.request
    
    prs = []
    # repo format for GitLab: "group/project" → URL-encoded
    project_path = repo.replace("/", "%2F")
    gitlab_url = os.environ.get("GITLAB_URL", "https://gitlab.com")
    
    try:
        req = urllib.request.Request(
            f"{gitlab_url}/api/v4/projects/{project_path}/merge_requests?state=merged&order_by=updated_at&per_page={min(max_prs, 100)}",
            headers={"PRIVATE-TOKEN": token}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            mrs_data = json.loads(resp.read().decode())
    except Exception as e:
        # Install-time indexing feeds the RAG store. Silent failure here
        # meant Ripple learned nothing from this repo's merged PRs and
        # reported no reason -- fixes would silently fall back to templates
        # forever.
        _log_activity("pr_index_fetch_failed", {
            "repo": repo,
            "platform": "gitlab",
            "err": f"{type(e).__name__}: {str(e)[:140]}",
            "impact": "RAG learns nothing from this repo",
        })
        return []
    
    for mr in mrs_data[:max_prs]:
        # Fetch MR changes
        try:
            req = urllib.request.Request(
                f"{gitlab_url}/api/v4/projects/{project_path}/merge_requests/{mr['iid']}/changes",
                headers={"PRIVATE-TOKEN": token}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                changes_data = json.loads(resp.read().decode())
            files = [c["new_path"] for c in changes_data.get("changes", [])]
        except Exception as e:
            _log_activity("pr_index_files_failed", {
                "repo": repo, "platform": "gitlab",
                "err": f"{type(e).__name__}: {str(e)[:120]}",
            })
            files = []
        
        prs.append({
            "number": mr["iid"],
            "title": mr.get("title", ""),
            "files": files,
            "merged_at": mr.get("merged_at", ""),
        })
    
    return prs


def _fetch_bitbucket_merged_prs(repo: str, token: str, max_prs: int) -> list[dict]:
    """Fetch merged PRs from Bitbucket Cloud API."""
    import urllib.request
    
    prs = []
    try:
        req = urllib.request.Request(
            f"https://api.bitbucket.org/2.0/repositories/{repo}/pullrequests?state=MERGED&pagelen={min(max_prs, 50)}",
            headers={"Authorization": f"Bearer {token}"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        _log_activity("pr_index_fetch_failed", {
            "repo": repo, "platform": "bitbucket",
            "err": f"{type(e).__name__}: {str(e)[:140]}",
            "impact": "RAG learns nothing from this repo",
        })
        return []
    
    for pr_data in data.get("values", [])[:max_prs]:
        # Fetch diffstat for file list
        try:
            req = urllib.request.Request(
                pr_data.get("links", {}).get("diffstat", {}).get("href", ""),
                headers={"Authorization": f"Bearer {token}"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                diff_data = json.loads(resp.read().decode())
            files = [d.get("new", {}).get("path", "") for d in diff_data.get("values", [])]
        except Exception as e:
            _log_activity("pr_index_files_failed", {
                "repo": repo, "platform": "bitbucket",
                "err": f"{type(e).__name__}: {str(e)[:120]}",
            })
            files = []
        
        prs.append({
            "number": pr_data.get("id", 0),
            "title": pr_data.get("title", ""),
            "files": files,
            "merged_at": pr_data.get("updated_on", ""),
        })
    
    return prs


def _fetch_git_log_prs(repo_path: str, max_prs: int) -> list[dict]:
    """Fallback for self-hosted: extract PR-like info from git log merge commits."""
    import subprocess
    
    prs = []
    try:
        result = subprocess.run(
            ["git", "log", "--merges", f"--max-count={max_prs}", "--format=%H|%s", "--name-only"],
            cwd=repo_path, capture_output=True, text=True, timeout=60
        )
        
        current_pr = None
        for line in result.stdout.strip().split("\n"):
            if "|" in line and line.count("|") == 1:
                if current_pr:
                    prs.append(current_pr)
                sha, title = line.split("|", 1)
                current_pr = {"number": sha[:8], "title": title, "files": [], "merged_at": ""}
            elif current_pr and line.strip():
                current_pr["files"].append(line.strip())
        
        if current_pr:
            prs.append(current_pr)
    except Exception as e:
        _log_activity("pr_index_gitlog_failed", {
            "repo": repo_path,
            "err": f"{type(e).__name__}: {str(e)[:140]}",
        })
    
    return prs[:max_prs]


def _find_consumer_repos(source_repo: str, token: str,
                         installation_id: int = None) -> list[str]:
    """Return the repos to search for consumers of `source_repo`.

    App mode (correct): GitHub tells us exactly which repositories the
    customer granted this installation. That list is authoritative and
    already scoped, which is why this function no longer needs a repo cap,
    a fork filter, or a self-repo blocklist -- three heuristics that
    existed only because a personal access token could see every repo the
    human owned. The cap in particular silently dropped consumers for
    anyone with more repos than the limit.

    PAT mode (degraded): no installation scope exists. Prefer an explicit
    allowlist; otherwise enumerate owned repos and SAY SO, because an
    unscoped guess must never look like an authoritative answer.

    The source repo is always included for monorepo support -- consumers
    can live alongside the spec.
    """
    # --- authoritative path -------------------------------------------
    if installation_id and is_app_configured():
        try:
            installed = list_installation_repositories(installation_id)
            repos = [source_repo] + [r for r in installed if r != source_repo]
            _log_activity("consumer_scope", {
                "mode": "app_installation",
                "authoritative": True,
                "count": len(repos),
            })
            return repos
        except AppAuthError as e:
            _log_activity("consumer_scope_error", {
                "mode": "app_installation",
                "err": str(e)[:200],
            })

    # --- explicit allowlist -------------------------------------------
    allowlist = [
        r.strip() for r in os.environ.get("RIPPLE_CONSUMER_REPOS", "").split(",")
        if r.strip()
    ]
    if allowlist:
        repos = [source_repo] + [r for r in allowlist if r != source_repo]
        _log_activity("consumer_scope", {
            "mode": "explicit_allowlist",
            "authoritative": True,
            "count": len(repos),
        })
        return repos

    # --- degraded: unscoped enumeration -------------------------------
    owner = source_repo.split("/")[0]
    data = _github_api(
        "GET",
        f"/users/{owner}/repos?per_page=100&sort=pushed&direction=desc",
        token,
    )
    repos = [source_repo]
    truncated = False
    if isinstance(data, list):
        candidates = [
            r["full_name"] for r in data
            if r["full_name"] != source_repo and not r.get("archived")
        ]
        # Paging beyond one page is not attempted here; report it rather
        # than pretend the list is complete.
        truncated = len(data) >= 100
        repos.extend(candidates)

    _log_activity("consumer_scope", {
        "mode": "unscoped_owner_enumeration",
        "authoritative": False,
        "count": len(repos),
        "possibly_truncated": truncated,
        "hint": "configure GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY, "
                "or set RIPPLE_CONSUMER_REPOS, for scoped discovery",
    })
    return repos


def _search_repo_for_consumers(repo: str, change: BreakingChange, token: str,
                               exclude_path: str = "",
                               budget: dict = None) -> list[tuple[str, str, float]]:
    """Search a repo for files that consume the changed endpoint.
    
    For monorepo support: exclude_path filters out the spec file itself
    so we don't try to 'fix' the spec that was just changed.
    """
    from .smart_consumer_finder import generate_variants, file_is_consumer
    from urllib.parse import quote
    
    # Generate search terms from the field name (not just the path)
    field_name = change.field_name
    variants = generate_variants(field_name)
    
    # Search for ALL naming variants, not just the proto snake_case name.
    # GitHub code search treats phone_number / phoneNumber / PhoneNumber as
    # distinct tokens, so a snake_case-only query silently misses Go
    # (PhoneNumber) and TypeScript (phoneNumber) consumers entirely.
    # Capped because GitHub rejects overly complex OR queries.
    core_variants = []
    for v in variants:
        # Skip accessor-prefixed forms -- the bare name already matches the
        # declaration site, which is what we need to rewrite.
        if v.lower().startswith(("get", "set", "has")):
            continue
        if v not in core_variants:
            core_variants.append(v)
    core_variants = core_variants[:4] or [field_name]
    
    term_query = " OR ".join(core_variants)
    query = quote(f"{term_query} repo:{repo}", safe="")
    
    # GitHub search API -- search for field name in code.
    # Code search is limited to ~30 req/min and enforces a burst
    # ("secondary") limit, so a fan-out across many repos gets throttled.
    # Retry once on a rate-limit response rather than silently returning [].
    data = _github_api("GET", f"/search/code?q={query}", token)
    
    if "error" in data and data.get("error") in (403, 429):
        _time.sleep(2)
        data = _github_api("GET", f"/search/code?q={query}", token)
    
    if "error" in data:
        _log_activity("search_error", {
            "repo": repo,
            "terms": core_variants,
            "err": str(data.get("message", data))[:150],
        })
        return []
    
    results = []
    if "items" in data:
        for item in data["items"][:10]:  # Check up to 10 files
            file_path = item["path"]
            # Skip the spec file itself (monorepo: don't fix the source)
            if file_path == exclude_path:
                continue
            if not _is_code_file(file_path):
                continue
            
            # Fetch file content
            file_data = _github_api("GET", f"/repos/{repo}/contents/{file_path}", token)
            if "content" not in file_data:
                continue
            
            content = base64.b64decode(file_data["content"]).decode()
            language = _detect_lang(file_path)
            
            # Smart filtering: only include if it's a REAL consumer (not just a comment)
            is_consumer, confidence, matches = file_is_consumer(
                content, file_path, field_name, language, min_confidence=0.5
            )
            
            if is_consumer:
                # Propagate the COMPUTED confidence. This used to be
                # discarded, so every PR fell back to a hardcoded 0.7 even
                # when the detector had scored a struct-field match at 0.95.
                results.append((file_path, content, confidence))
    
    # Code search depends on GitHub's search INDEX, which lags for newly
    # created or recently pushed repos -- it returns total_count=0 even
    # though the files exist. That silently breaks the most important
    # moment: the first push after a customer installs Ripple.
    # Fall back to walking the repo tree, which is always current.
    if not results:
        _log_activity("search_fallback_tree", {"repo": repo, "reason": "code search returned 0"})
        results = _scan_repo_tree_for_consumers(repo, change, token, exclude_path, budget)
    
    return results


def _scan_repo_tree_for_consumers(
    repo: str, change: BreakingChange, token: str, exclude_path: str = "",
    budget: dict = None,
) -> list[tuple[str, str, float]]:
    """Find consumers by listing the repo tree directly (no search index).

    Slower than code search but always up to date. In practice this is now
    the PRIMARY path, not a rare fallback: code search returns 0 results for
    installation-token requests and for recently created repos.

    Because each candidate file costs one API call, an unbounded scan across
    every repo in scope issues hundreds of rapid requests and GitHub starts
    closing connections. `budget` caps total content fetches per spec change
    and is reported when exhausted, so a truncated scan is never silent.
    """
    from .smart_consumer_finder import file_is_consumer

    field_name = change.field_name
    max_files = int(os.environ.get("RIPPLE_MAX_TREE_FILES", "40"))
    max_blob_bytes = int(os.environ.get("RIPPLE_MAX_BLOB_BYTES", "200000"))

    repo_data = _github_api("GET", f"/repos/{repo}", token)
    if "error" in repo_data:
        return []
    default_branch = repo_data.get("default_branch", "main")

    tree = _github_api(
        "GET", f"/repos/{repo}/git/trees/{default_branch}?recursive=1", token
    )
    if "tree" not in tree:
        return []

    candidates = [
        node["path"] for node in tree["tree"]
        if node.get("type") == "blob"
        and node["path"] != exclude_path
        and _is_code_file(node["path"])
        # Tree API reports size; giant files are not hand-edited consumers
        # and are expensive to fetch.
        and node.get("size", 0) <= max_blob_bytes
    ][:max_files]

    results = []
    for file_path in candidates:
        if budget is not None:
            if budget.get("remaining", 0) <= 0:
                _log_activity("tree_scan_budget_exhausted", {
                    "repo": repo,
                    "skipped_from": file_path,
                    "note": "raise RIPPLE_TREE_CALL_BUDGET if consumers are being missed",
                })
                break
            budget["remaining"] -= 1

        file_data = _github_api(
            "GET", f"/repos/{repo}/contents/{file_path}?ref={default_branch}", token
        )
        if "content" not in file_data:
            continue
        try:
            content = base64.b64decode(file_data["content"]).decode()
        except (ValueError, UnicodeDecodeError):
            continue

        is_consumer, confidence, matches = file_is_consumer(
            content, file_path, field_name, _detect_lang(file_path), min_confidence=0.5
        )
        if is_consumer:
            results.append((file_path, content, confidence))

    if results:
        _log_activity("tree_scan_found", {
            "repo": repo,
            "scanned": len(candidates),
            "found": len(results),
            "files": [f for f, _, _ in results][:5],
        })
    return results


def _is_code_file(filepath: str) -> bool:
    """Is this a file a human would hand-edit to adapt to a contract change?

    Extension alone is insufficient: vendored dependencies and generated
    code carry real source extensions but must never be patched by a PR.
    """
    exts = {".ts", ".tsx", ".js", ".py", ".java", ".go", ".rs", ".rb"}
    if not any(filepath.endswith(ext) for ext in exts):
        return False
    
    lowered = filepath.lower()
    
    # Vendored dependencies and build output. Excluding these is not a
    # heuristic -- they are never hand-edited source, so a PR touching them
    # is always wrong.
    non_source_dirs = (
        "node_modules/", "vendor/", "third_party/", "dist/", "build/",
        ".next/", "coverage/", "target/debug/", "target/release/",
        "site-packages/", ".venv/", "venv/",
    )
    if any(seg in lowered for seg in non_source_dirs):
        return False
    
    # Generated code: regenerated from the contract, so editing it is
    # pointless -- the generator output changes when the spec changes.
    if lowered.endswith((".min.js", ".d.ts", ".pb.go", "_pb2.py",
                         "_pb2_grpc.py", ".generated.ts", ".g.dart")):
        return False
    
    # NOTE: 'website/', 'docs/', 'marketing/', 'examples/' were previously
    # excluded here to stop Ripple opening a PR against its own landing
    # page. That was suppressing a symptom of unscoped repo discovery, not
    # a real rule -- a customer can legitimately call an API from an
    # example app or a docs site. Now that installation scope is
    # authoritative, cross-repo false positives are gone, and per-customer
    # exclusions belong in .ripple.yaml `ignore:` (already honoured via
    # config.should_ignore) rather than in a hardcoded list here.
    
    return True


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
    change: BreakingChange, source_repo: str, token: str,
    confidence: float = 0.8, sources: list = None,
    reasons: list = None, all_predictions: list = None,
) -> str:
    """Create a PR with the fix in the consumer repo, including confidence report."""
    # Get default branch
    repo_data = _github_api("GET", f"/repos/{repo}", token)
    if "error" in repo_data:
        _log_activity("pr_error", {"step": "get_repo", "repo": repo, "err": str(repo_data)[:150]})
        return ""
    default_branch = repo_data.get("default_branch", "main")
    
    # Get HEAD sha
    ref_data = _github_api("GET", f"/repos/{repo}/git/ref/heads/{default_branch}", token)
    if "object" not in ref_data:
        _log_activity("pr_error", {"step": "get_ref", "repo": repo, "branch": default_branch, "err": str(ref_data)[:150]})
        return ""
    base_sha = ref_data["object"]["sha"]
    
    # Create branch. Sanitize to a valid git ref: git refuses names with
    # consecutive/trailing dots, trailing dashes, or path separators.
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{change.field_name}-{change.path or ''}")
    slug = re.sub(r"-+", "-", slug).strip("-.") or "fix"
    branch = f"ripple/fix-{slug}"
    ref_result = _github_api("POST", f"/repos/{repo}/git/refs", token, {
        "ref": f"refs/heads/{branch}",
        "sha": base_sha,
    })
    # 422 "Reference already exists" is fine -- we reuse the branch.
    if "error" in ref_result and "already exists" not in ref_result.get("message", ""):
        _log_activity("pr_error", {"step": "create_branch", "repo": repo, "branch": branch, "err": str(ref_result)[:150]})
        return ""
    
    # Get current file sha
    file_data = _github_api("GET", f"/repos/{repo}/contents/{file_path}?ref={branch}", token)
    file_sha = file_data.get("sha", "")
    if not file_sha:
        _log_activity("pr_error", {"step": "get_file_sha", "repo": repo, "file": file_path, "err": str(file_data)[:150]})
        return ""
    
    # Push fix. Message must match what actually happened -- this used to
    # always say "Add required field" even for removals.
    if change.change_type in ("field_removed", "removed_field"):
        commit_msg = f"fix: Remove references to deleted field '{change.field_name}'"
    elif change.change_type == "field_renamed":
        commit_msg = f"fix: Rename field '{change.field_name}' to '{getattr(change, 'new_name', '') or 'new name'}'"
    elif change.change_type in ("type_changed", "field_type_changed"):
        commit_msg = f"fix: Update type of field '{change.field_name}'"
    else:
        commit_msg = f"fix: Adapt to breaking change in '{change.field_name}'"
    
    # Detect references to the field that survived the automated fix.
    # These need a human decision, so the PR must say so rather than
    # presenting itself as a finished fix.
    from .smart_consumer_finder import find_residual_references
    residual_refs = find_residual_references(
        fixed_content, change.field_name, _detect_lang(file_path)
    )
    if residual_refs:
        commit_msg = f"{commit_msg} (partial — {len(residual_refs)} call site(s) need review)"
        _log_activity("residual_refs_flagged", {
            "repo": repo,
            "file": file_path,
            "count": len(residual_refs),
            "lines": [r[0] for r in residual_refs[:5]],
        })
    
    put_result = _github_api("PUT", f"/repos/{repo}/contents/{file_path}", token, {
        "message": commit_msg,
        "content": base64.b64encode(fixed_content.encode()).decode(),
        "branch": branch,
        "sha": file_sha,
    })
    if "error" in put_result:
        _log_activity("pr_error", {"step": "commit_fix", "repo": repo, "file": file_path, "err": str(put_result)[:150]})
        return ""
    
    # Generate PR body with confidence scoring
    change_description = f"`{change.change_type}` on field `{change.field_name}` in `{change.path or 'spec'}`"
    pr_body = format_pr_body(
        change_description=change_description,
        source_repo=source_repo,
        confidence=confidence,
        sources=sources or ["grep"],
        reasons=reasons or ["Direct API endpoint reference found"],
        all_predictions=all_predictions,
        residual_refs=residual_refs,
        consumer_file=file_path,
    )
    
    # Create PR
    pr_data = _github_api("POST", f"/repos/{repo}/pulls", token, {
        "title": commit_msg,
        "body": pr_body,
        "head": branch,
        "base": default_branch,
    })
    if "error" in pr_data:
        # A Ripple PR for this same field can already be open -- either the
        # customer re-pushed the same change, or a prior run created it.
        # GitHub returns 422 here. The fix commit was already pushed to the
        # branch above, so that PR is now up to date: return it instead of
        # reporting failure and producing nothing.
        existing = _find_open_pr_for_branch(repo, branch, default_branch, token)
        if existing:
            _log_activity("pr_updated_existing", {
                "repo": repo, "branch": branch, "url": existing,
            })
            return existing
        _log_activity("pr_error", {"step": "open_pr", "repo": repo, "branch": branch, "err": str(pr_data)[:150]})
        return ""
    
    return pr_data.get("html_url", "")


def _find_open_pr_for_branch(
    repo: str, branch: str, base: str, token: str
) -> str:
    """Return the URL of an open PR whose head is `branch`, if any."""
    owner = repo.split("/")[0]
    prs = _github_api(
        "GET", f"/repos/{repo}/pulls?state=open&head={owner}:{branch}&base={base}", token
    )
    if isinstance(prs, list) and prs:
        return prs[0].get("html_url", "")
    return ""
