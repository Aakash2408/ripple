# Self-Hosted

Run Ripple on your own infrastructure for full control over data and network access.

## Quick Start

```bash
docker pull ghcr.io/aakash2408/ripple:latest

docker run -d \
  --name ripple \
  -p 8080:8080 \
  -e ADAPTER=github \
  -e GITHUB_TOKEN=ghp_xxxxxxxxxxxx \
  -v ripple-data:/app/data \
  ghcr.io/aakash2408/ripple:latest
```

## Environment Variables

### Required

| Var | Description |
|---|---|
| `ADAPTER` | Platform adapter: `github`, `gitlab`, `bitbucket`, `phabricator`, `gerrit`, `crux`, `generic` |

### Platform-Specific

| Var | Adapters | Description |
|---|---|---|
| `GITHUB_TOKEN` | github | Personal access token or app installation token |
| `GITLAB_TOKEN` | gitlab | OAuth or personal access token |
| `BITBUCKET_TOKEN` | bitbucket | OAuth token |
| `PHABRICATOR_URL` | phabricator | Instance URL |
| `PHABRICATOR_TOKEN` | phabricator | Conduit API token |
| `GERRIT_URL` | gerrit | Instance URL |
| `GERRIT_USERNAME` | gerrit | Bot account username |
| `GERRIT_PASSWORD` | gerrit | HTTP password |
| `GIT_REPO_PATH` | generic | Path to local repository |

### Optional

| Var | Default | Description |
|---|---|---|
| `PORT` | `8080` | Server port |
| `LOG_LEVEL` | `info` | Logging verbosity: `debug`, `info`, `warn`, `error` |
| `WEBHOOK_SECRET` | — | Secret for validating incoming webhooks |
| `LLM_API_KEY` | — | API key for LLM-based fix generation (falls back to template-based) |
| `MAX_CONSUMERS` | `50` | Maximum consumers to process per change |
| `DRY_RUN` | `false` | Analyze without opening PRs |

## Volume Mounts

| Path | Purpose |
|---|---|
| `/app/data` | Persistent storage for dependency graphs, consumer cache, and OAuth tokens |
| `/app/logs` | Application logs |

```bash
docker run -d \
  -v ripple-data:/app/data \
  -v ./logs:/app/logs \
  ghcr.io/aakash2408/ripple:latest
```

## Docker Compose

```yaml
version: '3.8'
services:
  ripple:
    image: ghcr.io/aakash2408/ripple:latest
    ports:
      - "8080:8080"
    environment:
      - ADAPTER=github
      - GITHUB_TOKEN=${GITHUB_TOKEN}
      - WEBHOOK_SECRET=${WEBHOOK_SECRET}
    volumes:
      - ripple-data:/app/data
    restart: unless-stopped

volumes:
  ripple-data:
```

## Webhook Configuration

For self-hosted deployments, configure your platform to send webhooks to:

```
https://your-host:8080/webhook
```

### GitHub Webhook Events

- `push`
- `pull_request`

### GitLab Webhook Events

- Push events
- Merge request events

## Health Check

```bash
curl http://localhost:8080/health
```

Returns `{"status": "ok", "version": "1.0.0"}` when healthy.

## Updating

```bash
docker pull ghcr.io/aakash2408/ripple:latest
docker stop ripple
docker rm ripple
# Re-run with same docker run command
```

Data is preserved in the `ripple-data` volume across updates.
