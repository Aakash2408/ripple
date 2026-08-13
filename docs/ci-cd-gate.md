# CI/CD Gate

Block breaking API changes before they merge with the **Ripple Check** GitHub Action.

## What It Does

The Ripple Check runs on every pull request and:

1. Scans the diff for contract changes (OpenAPI, Proto, GraphQL, etc.)
2. Determines if the change is breaking
3. Posts a comment with the impact analysis
4. Blocks the merge if breaking changes are detected without a fix plan

## Setup

Add this workflow to your repository:

```yaml
# .github/workflows/ripple-check.yml
name: Ripple Check

on:
  pull_request:
    branches: [main, master]

jobs:
  ripple-check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Ripple Breaking Change Check
        uses: Aakash2408/ripple@main
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
          fail-on-breaking: true
          post-comment: true
```

## Inputs

| Input | Required | Default | Description |
|---|---|---|---|
| `github-token` | Yes | — | Token for posting comments and status checks |
| `fail-on-breaking` | No | `true` | Fail the check if breaking changes detected |
| `post-comment` | No | `true` | Post impact analysis as a PR comment |
| `contract-paths` | No | auto-detect | Comma-separated paths to contract files |
| `ignore-paths` | No | — | Paths to exclude from scanning |

## Example Output

When a breaking change is detected, Ripple posts a comment like:

```
⚠️ Breaking Change Detected

File: api/openapi.yaml
Change: Removed field `user.email` from GET /users/{id} response

Impact: 3 downstream consumers found
  • frontend-app (TypeScript) — uses user.email in profile display
  • mobile-api (Kotlin) — maps to UserDTO.email
  • analytics-service (Python) — logs user.email for tracking

Recommendation: Use expand-and-contract pattern.
Mark field as deprecated, add new field, migrate consumers, then remove.
```

## Monorepo Support

For monorepos, Ripple automatically detects which contracts changed based on the diff path. No additional configuration needed — it only checks contracts modified in the current PR.

## Skipping the Check

Add `[ripple-skip]` to your commit message to bypass the check for intentional breaking changes.
