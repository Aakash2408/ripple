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
from .proto_diff import diff_proto
from .graphql_diff import diff_graphql
from .migration_diff import diff_schema
from .asyncapi_diff import diff_asyncapi
from .avro_diff import diff_avro
from .trpc_diff import diff_trpc
from .thrift_diff import diff_thrift
from .jsonschema_diff import diff_jsonschema
from .smithy_diff import diff_smithy
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

# SSL context for GitHub API calls (Amazon dev desktop fix)
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

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


@app.get("/health")
async def health():
    return {"healthy": True}


# === GitHub App Installation Event ===

@app.post("/webhook/install")
async def github_install_webhook(request: FastAPIRequest):
    """
    Handles GitHub App installation events.
    When installed on a repo/org, automatically scans git history
    to learn co-change patterns (PropBench Historian integration).
    """
    body = await request.body()
    payload = json.loads(body)
    event_type = request.headers.get("X-GitHub-Event", "")
    
    if event_type == "installation" and payload.get("action") == "created":
        # App was just installed — learn from all repos
        repos = payload.get("repositories", [])
        org = payload.get("installation", {}).get("account", {}).get("login", "unknown")
        
        learner = get_learner(org)
        results = []
        
        for repo in repos:
            repo_name = repo.get("full_name", "")
            # Trigger async learning (in production, this would be a background job)
            results.append({
                "repo": repo_name,
                "status": "learning_queued",
                "message": f"Will scan git history for co-change patterns",
            })
        
        return {
            "status": "installation_received",
            "org": org,
            "repos_to_learn": len(repos),
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
    
    # Rate limit check
    repo_full_name = payload["repository"]["full_name"]
    org = repo_full_name.split("/")[0] if "/" in repo_full_name else "unknown"
    limiter = get_rate_limiter()
    allowed, reason = limiter.check(org)
    if not allowed:
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
                    fixed_code, explanation = _generate_with_template(consumer_content, consumer_match, change)
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
                    fixed_code, explanation = _generate_with_template(consumer_content, consumer_match, change)
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
    token = _get_token(installation_id)
    org = repo.split("/")[0] if "/" in repo else "unknown"
    
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
        return {"status": "no_breaking_changes", "spec": spec_path}
    
    # Find consumer repos (from GitHub App installation)
    consumer_repos = _find_consumer_repos(repo, token)
    
    # For each breaking change, use ENSEMBLE to find consumers
    prs_created = []
    warnings = []
    ensemble_stats = {"grep": 0, "playbook": 0, "history": 0, "multi_invoker": 0, "custom": 0}
    
    for change in breaking_changes:
        # Determine contract type from spec file
        contract_type = _detect_contract_type(spec_path)
        change_type = _map_change_type(change)
        
        # --- ENSEMBLE CONSUMER FINDING ---
        # Step 1: Grep-based search across repos
        grep_results = []
        for consumer_repo in consumer_repos:
            consumer_files = _search_repo_for_consumers(consumer_repo, change, token, exclude_path=spec_path)
            for file_path, content in consumer_files:
                grep_results.append(file_path)
        
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
            consumer_files = _search_repo_for_consumers(consumer_repo, change, token, exclude_path=spec_path)
            
            for consumer_file, consumer_content in consumer_files:
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
                
                fixed_code, explanation = _generate_with_template(
                    consumer_content, consumer, change
                )
                
                if fixed_code != consumer_content:
                    # Find this file's confidence from ensemble predictions
                    file_confidence = 0.7  # default grep confidence
                    file_sources = ["grep"]
                    file_reasons = ["Direct API endpoint reference found"]
                    for pred in ensemble_predictions:
                        if pred.get("file") == consumer_file or consumer_file.endswith(pred.get("file", "")):
                            file_confidence = pred["confidence"]
                            file_sources = pred.get("sources", ["grep"])
                            file_reasons = pred.get("reasons", ["Pattern match"])
                            break
                    
                    # Only create PR if confidence is high enough
                    if not should_create_pr(file_confidence, min_confidence):
                        continue
                    
                    pr_url = _create_fix_pr(
                        consumer_repo, consumer_file,
                        fixed_code, change, repo, token,
                        confidence=file_confidence,
                        sources=file_sources,
                        reasons=file_reasons,
                        all_predictions=ensemble_predictions[:5],
                    )
                    if pr_url:
                        prs_created.append(pr_url)
    
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
    """Find repos that might consume APIs from the source repo.
    
    Includes the source repo itself (monorepo support) — consumers
    can live in the same repo as the spec.
    """
    # Always include the source repo (monorepo support)
    repos = [source_repo]
    
    # Get all other repos for the user/org
    owner = source_repo.split("/")[0]
    data = _github_api("GET", f"/users/{owner}/repos?per_page=100", token)
    
    if isinstance(data, list):
        repos.extend([
            r["full_name"] for r in data
            if r["full_name"] != source_repo and not r.get("archived")
        ])
    return repos


def _search_repo_for_consumers(repo: str, change: BreakingChange, token: str, exclude_path: str = "") -> list[tuple[str, str]]:
    """Search a repo for files that consume the changed endpoint.
    
    For monorepo support: exclude_path filters out the spec file itself
    so we don't try to 'fix' the spec that was just changed.
    """
    # Use GitHub code search or contents API
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
            # Skip the spec file itself (monorepo: don't fix the source)
            if file_path == exclude_path:
                continue
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
    change: BreakingChange, source_repo: str, token: str,
    confidence: float = 0.8, sources: list = None,
    reasons: list = None, all_predictions: list = None,
) -> str:
    """Create a PR with the fix in the consumer repo, including confidence report."""
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
    
    # Generate PR body with confidence scoring
    change_description = f"Added required field `{change.field_name}` to `{change.method.upper()} {change.path}`"
    pr_body = format_pr_body(
        change_description=change_description,
        source_repo=source_repo,
        confidence=confidence,
        sources=sources or ["grep"],
        reasons=reasons or ["Direct API endpoint reference found"],
        all_predictions=all_predictions,
    )
    
    # Create PR
    pr_data = _github_api("POST", f"/repos/{repo}/pulls", token, {
        "title": commit_msg,
        "body": pr_body,
        "head": branch,
        "base": default_branch,
    })
    
    return pr_data.get("html_url", "")
