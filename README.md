# 🌊 Ripple — Self-Maintaining APIs

> When you change an API, we find every consumer and open PRs to fix them.

[![Demo PR](https://img.shields.io/badge/demo-PR%20%231-green)](https://github.com/Aakash2408/ripple-demo-frontend/pull/1)

## The Problem

You change `POST /users` to require a new field. Three teams find out when their code breaks in production on Friday night.

## The Solution

Ripple detects breaking API changes, finds every consumer, generates the fix, and opens a PR — in seconds, not days.

```
$ ripple run old-spec.yaml new-spec.yaml --repos ./frontend ./mobile ./analytics

🌊 RIPPLE — API Change Propagation

⚠️  1 breaking change detected:
  POST /users — added required field 'country' (string)

🔍 Found 3 consumers
🔧 Generated 3 fixes
📤 Opened 3 PRs

✅ Done in 12 seconds.
```

## Quick Start

```bash
# Install
pip install ripple-api  # coming soon

# Or run from source
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

1. **Detect** — Parses OpenAPI specs, finds breaking changes (added required fields, removed fields, renames)
2. **Find** — Scans your org's repos for code that calls the changed endpoint
3. **Fix** — Generates the minimal code fix for each consumer (TypeScript, Python, Java, Go)
4. **PR** — Opens a pull request with the fix + explanation

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

# Railway (one-click)
# Fly.io
flyctl launch
```

## Why Ripple?

- **Not Dependabot** — Dependabot bumps library versions. Ripple fixes YOUR code when YOUR APIs change.
- **Not a linter** — Linters find style issues. Ripple finds breaking contract violations across repos.
- **Not AI code review** — Code review finds bugs in what you wrote. Ripple fixes what you forgot to update.

## License

MIT
