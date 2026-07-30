# 🌊 Ripple — Self-Maintaining APIs

> When you change an API, Ripple finds every consumer and opens PRs to fix them. Automatically.

### 🌐 [**Live Landing Page**](https://aakash2408.github.io/ripple/) · [**Install GitHub App**](https://github.com/apps/ripple-api) · [**Dashboard**](https://ripple-production-be7f.up.railway.app/dashboard)

[![Install GitHub App](https://img.shields.io/badge/Install-GitHub%20App-blue)](https://github.com/apps/ripple-api)
[![Demo: Python](https://img.shields.io/badge/demo-Python%20PR-green)](https://github.com/Aakash2408/ripple-sdk-python/pull/1)
[![Demo: Node](https://img.shields.io/badge/demo-Node%20PR-yellow)](https://github.com/Aakash2408/ripple-sdk-node/pull/1)
[![Demo: Java](https://img.shields.io/badge/demo-Java%20PR-orange)](https://github.com/Aakash2408/ripple-sdk-java/pull/1)
[![Dashboard](https://img.shields.io/badge/live-Dashboard-purple)](https://ripple-production-be7f.up.railway.app/dashboard)

## The Problem

You change `POST /users` to require a new field. Three teams find out when their code breaks in production on Friday night.

## The Solution

Ripple detects breaking API changes, finds every consumer, generates the fix, and opens a PR — in seconds, not days.

```
🌊 RIPPLE — API Change Propagation

⚠️  1 breaking change detected:
  POST /payments — added required field 'idempotency_key' (string)

🔍 Found 3 consumers (Python SDK, Node SDK, Java SDK)
🔧 Generated 3 fixes (validated ✓)
📤 Opened 3 PRs

✅ Done in 15 seconds.
```

## Install

**Option 1: GitHub App (recommended)**

Install on your org — Ripple auto-triggers on every push:

👉 [**Install Ripple**](https://github.com/apps/ripple-api)

That's it. Push a breaking change to any API spec and fix PRs appear in consumer repos.

**Option 2: Run from source**

```bash
git clone https://github.com/Aakash2408/ripple.git
cd ripple
pip install -r requirements.txt

# Detect breaking changes
python -m app.cli diff old.yaml new.yaml

# Find consumers + generate fixes
python -m app.cli run old.yaml new.yaml --repos ./consumer1 ./consumer2

# Deploy as webhook (auto-triggers on push)
uvicorn app.webhook:app --port 8000
```

## How It Works

1. **Detect** — Parses API contracts (OpenAPI, Protobuf, GraphQL, database schemas) and finds breaking changes
2. **Find** — Scans your org's repos for code that calls the changed endpoint
3. **Fix** — Generates the minimal code fix for each consumer (TypeScript, Python, Java)
4. **Validate** — Syntax-checks every fix before opening a PR
5. **PR** — Opens a pull request with the fix + clear explanation

## It Learns Your Codebase

Ripple gets smarter the more you use it:

- **Co-change learning** — Scans git history on install. If two files always changed together, Ripple knows they're related.
- **Consumer graph** — Builds a persistent map of which services depend on which APIs. Updates on every push.
- **Multi-invoker detection** — Warns when a shared config/schema has multiple consumers (prevents the "I deleted a block and broke an unrelated service" problem).
- **Pattern playbooks** — Knows that proto changes also affect `*_pb2.py`, `*.pb.go`, and test files.
- **Custom playbooks** — Define your own patterns in `.ripple.yaml` (see below).

Result: 3x better consumer detection than grep alone.

## Custom Playbooks

Add a `.ripple.yaml` to your repo root to teach Ripple about YOUR codebase:

```yaml
playbooks:
  - name: "Our API gateway"
    trigger:
      files: ["api/openapi.yaml"]
      change_types: ["added_required_field", "removed_field"]
    consumers:
      - pattern: "sdk/python/**/*.py"
        confidence: 0.95
        reason: "Python SDK wraps this API"
      - pattern: "sdk/node/**/*.ts"
        confidence: 0.95
        reason: "Node SDK wraps this API"
      - pattern: "tests/integration/**/*"
        confidence: 0.85
        reason: "Integration tests call this API"

  - name: "Database migrations"
    trigger:
      files: ["db/migrations/*.sql", "prisma/schema.prisma"]
      change_types: ["*"]
    consumers:
      - pattern: "src/models/**/*"
        confidence: 0.90
        reason: "ORM models mirror DB schema"

ignore:
  - "*.lock"
  - "node_modules/**"
  - "dist/**"

settings:
  min_confidence: 0.6
  auto_learn: true
  max_prs_per_push: 10
```

Get the template: `GET /config/template`

## Supported Contracts

- [x] OpenAPI / Swagger
- [x] Protobuf (gRPC)
- [x] GraphQL
- [x] Database schemas (SQL DDL + Prisma)
- [x] AsyncAPI (Kafka, SNS/SQS, RabbitMQ, MQTT, NATS, WebSockets)

## Supported Change Types

**OpenAPI:**
- [x] Added required field
- [x] Removed field
- [x] Field type changed (string → integer breaks consumers)
- [x] Endpoint removed (consumers get 404)
- [x] Response field removed (consumers reading it get null)
- [x] Required header added (requests rejected without it)

**Protobuf:**
- [x] Field removed
- [x] Field type changed
- [x] Field number changed (wire incompatibility)
- [x] Required field added
- [x] Message removed
- [x] Message renamed (detected via field overlap)

**GraphQL:**
- [x] Field removed from type
- [x] Field made non-nullable (String → String!)
- [x] Type removed
- [x] Required argument added
- [x] Enum value removed
- [x] Union member removed

**Database:**
- [x] Column removed
- [x] Column type changed
- [x] NOT NULL column added (existing rows fail)
- [x] Column made NOT NULL
- [x] Table removed
- [x] Table renamed

**AsyncAPI:**
- [x] Channel removed (subscribers stop receiving)
- [x] Message payload field removed
- [x] Message payload field type changed
- [x] Required field added to message
- [x] Message removed from components
- [x] Server removed (connection config breaks)

## Supported Languages

- [x] TypeScript / JavaScript
- [x] Python
- [x] Java
- [ ] Go (coming soon)
- [ ] Rust (coming soon)

## Deploy

```bash
# Docker
docker build -t ripple .
docker run -p 8000:8000 -e GITHUB_TOKEN=ghp_xxx ripple

# Railway (one-click deploy)
# Live: https://ripple-production-be7f.up.railway.app
```

## GitLab Support

Ripple works with GitLab too. Setup:

1. **Set environment variables:**
   ```bash
   GITLAB_TOKEN=glpat-xxxxxxxxxxxx    # Personal/Project access token (api scope)
   GITLAB_WEBHOOK_SECRET=your-secret   # Optional
   ```

2. **Add webhook to your GitLab project:**
   - Go to Settings → Webhooks
   - URL: `https://your-ripple-server.com/webhook/gitlab`
   - Secret token: (same as GITLAB_WEBHOOK_SECRET)
   - Trigger: ✅ Push events
   - Click "Add webhook"

3. **Push a breaking change** — Ripple creates a Merge Request automatically.

Works with all 4 contract types (OpenAPI, Proto, GraphQL, Database) on GitLab.

## API Reference

```
POST /webhook           — GitHub push events (auto-triggered by GitHub App)
POST /webhook/gitlab    — GitLab push events
POST /webhook/install   — GitHub App installation (triggers learning)
POST /learn             — Manually trigger co-change learning
GET  /dashboard         — Web UI (monitored repos, activity, stats)
GET  /status/{org}      — What Ripple knows about your codebase (JSON)
GET  /rate-limit/{org}  — Rate limit status for an org
GET  /config/template   — Default .ripple.yaml template
GET  /health            — Server health
GET  /docs              — Swagger UI (auto-generated)
```

## Dashboard

See what Ripple has learned about your org:

```
GET /dashboard        — web UI (monitored repos, activity, stats)
GET /status/{org}     — what Ripple knows about your codebase (JSON)
GET /health           — server health
```

## Why Ripple?

- **Not Dependabot** — Dependabot bumps library versions. Ripple fixes YOUR code when YOUR APIs change.
- **Not a linter** — Linters find style issues. Ripple finds breaking contract violations across repos.
- **Not AI code review** — Code review finds bugs in what you wrote. Ripple fixes what you forgot to update.
- **Not SDK generation** — SDK generators rebuild from scratch. Ripple patches your EXISTING code.

## License

MIT
