# 🌊 Ripple — Self-Maintaining APIs

> When you change an API, Ripple finds every consumer and opens PRs to fix them. Automatically.

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

Result: 3x better consumer detection than grep alone.

## Supported Contracts

- [x] OpenAPI / Swagger
- [x] Protobuf (gRPC)
- [x] GraphQL
- [x] Database schemas (SQL DDL + Prisma)

## Supported Change Types

- [x] Added required field
- [x] Removed field
- [ ] Renamed field (coming soon)
- [ ] Type change (coming soon)
- [ ] Endpoint rename (coming soon)

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
