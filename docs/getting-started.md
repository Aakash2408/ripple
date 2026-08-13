# Getting Started

Get Ripple running in under 2 minutes.

## Install on GitHub

1. Visit [github.com/apps/ripple-api](https://github.com/apps/ripple-api)
2. Click **Install**
3. Select the repositories you want Ripple to monitor
4. Done — Ripple is now watching for breaking changes

## Install on GitLab

1. Go to your Ripple dashboard
2. Click **Connect GitLab**
3. Authorize via OAuth
4. Select your projects

## Install on Bitbucket

1. Go to your Ripple dashboard
2. Click **Connect Bitbucket**
3. Authorize via OAuth
4. Select your repositories

## What Happens Next

Once installed, Ripple works automatically on every push:

```
You push a change
    ↓
Ripple detects contract changes (OpenAPI, Proto, GraphQL, etc.)
    ↓
Finds all downstream consumers using 5-channel ensemble finder
    ↓
Generates fixes in the consumer's language (12 languages + LLM)
    ↓
Opens a PR with:
  • AI confidence score
  • Change Impact Report
  • Lifecycle labels (pending/merged/reverted)
```

No configuration needed. Ripple infers contract types, discovers consumers, and generates idiomatic fixes automatically.

## Verify It's Working

After your first push with a breaking change:
- Check the **Pull Requests** tab in the consuming repos
- Visit your [Ripple Dashboard](https://ripple-production-be7f.up.railway.app/dashboard) for a full audit trail
