# Platforms

Ripple supports **7 platforms** — more than any competitor.

## Cloud Platforms

### GitHub (One-Click App)

Install directly from the GitHub Marketplace:

```
https://github.com/apps/ripple-api
```

Click Install → select repos → done. No tokens or configuration needed.

### GitLab (OAuth)

1. Navigate to your Ripple dashboard
2. Click **Connect GitLab**
3. Complete OAuth authorization
4. Select projects to monitor

Ripple creates Merge Requests in consuming repositories.

### Bitbucket (OAuth)

1. Navigate to your Ripple dashboard
2. Click **Connect Bitbucket**
3. Complete OAuth authorization
4. Select repositories to monitor

Ripple creates Pull Requests in consuming repositories.

## Self-Hosted Platforms

### Phabricator

For teams using `arc diff` + Conduit API (Meta, Uber, Pinterest).

```bash
docker run -d \
  -e ADAPTER=phabricator \
  -e PHABRICATOR_URL=https://phabricator.yourcompany.com \
  -e PHABRICATOR_TOKEN=cli-xxxxxxxxxxxx \
  -v ripple-data:/app/data \
  ghcr.io/aakash2408/ripple:latest
```

| Env Var | Description |
|---|---|
| `PHABRICATOR_URL` | Your Phabricator instance URL |
| `PHABRICATOR_TOKEN` | Conduit API token (Settings → Conduit API Tokens) |

Ripple creates Differential Revisions via `arc diff`.

### Gerrit

For teams using `refs/for/main` + REST API (Google, Android, Chromium).

```bash
docker run -d \
  -e ADAPTER=gerrit \
  -e GERRIT_URL=https://gerrit.yourcompany.com \
  -e GERRIT_USERNAME=ripple-bot \
  -e GERRIT_PASSWORD=your-http-password \
  -v ripple-data:/app/data \
  ghcr.io/aakash2408/ripple:latest
```

| Env Var | Description |
|---|---|
| `GERRIT_URL` | Your Gerrit instance URL |
| `GERRIT_USERNAME` | Bot account username |
| `GERRIT_PASSWORD` | HTTP password (Settings → HTTP Credentials) |

Ripple pushes to `refs/for/main` and creates Change Reviews via the REST API.

### CRUX (Amazon Internal)

For teams using Amazon's internal code review system.

```bash
docker run -d \
  -e ADAPTER=crux \
  -v ripple-data:/app/data \
  ghcr.io/aakash2408/ripple:latest
```

Uses the `cr` CLI to create code reviews in Brazil packages. Requires a workspace with the target package checked out.

### Generic Git

For any Git repository on disk — no platform integration needed.

```bash
docker run -d \
  -e ADAPTER=generic \
  -e GIT_REPO_PATH=/repos/my-service \
  -v /path/to/repos:/repos \
  -v ripple-data:/app/data \
  ghcr.io/aakash2408/ripple:latest
```

Ripple creates fix commits on a branch. You review and merge manually.

## Platform Comparison

| Platform | Auth | Fix Delivery | Setup Time |
|---|---|---|---|
| GitHub | One-click App | Pull Request | 30 seconds |
| GitLab | OAuth | Merge Request | 1 minute |
| Bitbucket | OAuth | Pull Request | 1 minute |
| Phabricator | Conduit Token | Differential | 5 minutes |
| Gerrit | HTTP Password | Change Review | 5 minutes |
| CRUX | cr CLI | Code Review | 5 minutes |
| Generic Git | None | Branch + Commit | 2 minutes |
