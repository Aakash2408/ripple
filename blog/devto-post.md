---
title: I Built a Tool That Auto-Fixes Downstream Code When You Change an API
published: true
tags: api, opensource, devtools, microservices
cover_image: https://aakash2408.github.io/ripple/og-image.png
---

You change a field in your proto file. You push it. Then you spend the next two days pinging 4 teams on Slack asking them to update their consumers.

Sound familiar?

I built **Ripple** to eliminate that entire workflow. Push a breaking API change → fix PRs appear in every consumer repo. In 15 seconds. No manual coordination.

## The Problem Nobody Talks About

There are great tools for *detecting* API breaking changes:

- **buf** catches proto incompatibilities
- **oasdiff** diffs OpenAPI specs
- **GraphQL Inspector** flags schema changes

But detection is only step 1. The real pain is **propagation**:

1. You know `user.proto` removed `phone_number`
2. But WHO uses `phone_number`? Which repos? Which files?
3. And what's the correct fix in each consumer?

That coordination — finding consumers, understanding their usage, writing the fix, opening PRs — takes **2-3 days per breaking change** at most orgs I've observed.

## What Ripple Does

```
You push: removed `phone_number` from user.proto

Ripple:
  ✅ Detects: field 3 removed (breaking)
  ✅ Finds: python-sdk/client.py, node-api/handlers/user.ts, java-gateway/UserService.java
  ✅ Generates: correct fix for each file (removes the dead field reference)
  ✅ Opens: 3 PRs with explanation of what changed and why
  
Time: ~15 seconds
```

## How It Works Under the Hood

### 1. Diff Engines (one per contract type)

Ripple has custom parsers for 10 contract types:

- OpenAPI / Swagger
- Protobuf / gRPC
- GraphQL
- Database (SQL + Prisma)
- AsyncAPI (Kafka, SNS, MQTT)
- Avro (Confluent Schema Registry)
- tRPC (TypeScript)
- Thrift (Apache)
- JSON Schema
- Smithy (AWS)

Each engine understands the semantics of its format. Removing an optional field is fine. Removing a required field is breaking. Changing a type is breaking. Adding a required field without a default is breaking.

### 2. Consumer Finding (the hard part)

This is where most tools stop. Finding consumers is genuinely difficult because:

- Consumers might be in different repos
- They might reference the spec indirectly (through generated code)
- Naming conventions vary wildly between codebases

Ripple uses an **ensemble approach** combining 5 strategies:

```python
# Simplified version of the ensemble
consumers = set()
consumers |= grep_for_field_name(removed_field)        # Basic but fast
consumers |= check_import_graph(spec_file)              # Who imports this?
consumers |= query_git_history(spec_file)               # Who changed when this changed?
consumers |= check_playbooks(org_config)                # Custom rules
consumers |= multi_invoker_detection(spec_file)         # Same spec, multiple callers
```

The git history approach is the most interesting — if `user.proto` and `python-sdk/client.py` always change together in commits, they're probably coupled. This is based on research from **PropBench**, a benchmark I built for measuring engineering judgment in change propagation (268 real scenarios, 1,223 consequence files analyzed).

### 3. Fix Generation

For each consumer file, Ripple generates the fix using:

1. **Template-based fixes** for common patterns (field removal → remove reference)
2. **LLM-powered fixes** for complex cases (Claude generates the correct code)
3. **Validation** — the fix must pass basic syntax checks before opening a PR

### 4. PR/MR Creation

Opens a pull request (GitHub), merge request (GitLab), or PR (Bitbucket) with:

- Clear title: "fix: Remove `phone_number` reference (field removed in user.proto)"
- Explanation of what changed upstream
- The minimal diff to fix the consumer
- Link back to the original commit

## Installation (One Click)

**GitHub:** [Install the Ripple GitHub App](https://github.com/apps/ripple-api)

**GitLab:** Visit `your-ripple-server/auth/gitlab` → Click Authorize → Done

**Bitbucket:** Visit `your-ripple-server/auth/bitbucket` → Click Authorize → Done

That's it. Webhooks are auto-installed on all your repos. Push a breaking change and watch the fix PRs appear.

## What Makes This Different From Dependabot/Renovate?

| | Dependabot | Ripple |
|---|---|---|
| **What it updates** | Library versions | API consumer code |
| **Trigger** | New version published | Breaking spec change pushed |
| **Fix type** | Bump version number | Modify actual code |
| **Knowledge** | Package registry | Your repo's git history |
| **Scope** | Single repo | Cross-repo propagation |

Dependabot bumps `protobuf` from 4.0 to 4.1 in your `requirements.txt`.

Ripple rewrites your `user_service.py` to handle the fact that `user.proto` no longer has a `phone_number` field.

## The Research Behind It

Ripple is backed by **PropBench** — a benchmark of 268 real engineering changes I analyzed to understand WHY developers miss downstream impacts:

- **34%** of misses: test files with non-obvious naming
- **26%** of misses: same-package files with no naming relationship
- **16%** of misses: config/YAML/JSON requiring domain knowledge
- **39%** of consequences are **cross-package** (invisible to single-repo tools)

A simple grep finds 7% of affected files. Adding co-change history from git bumps that to 17-38%. The ensemble approach reaches 82% at the package level.

The takeaway: most of what we call "senior engineering judgment" in change propagation is actually **learnable patterns** — naming conventions + git history + domain rules.

## Try It

- **Landing page:** [aakash2408.github.io/ripple](https://aakash2408.github.io/ripple)
- **Source:** [github.com/Aakash2408/ripple](https://github.com/Aakash2408/ripple)
- **GitHub App:** [github.com/apps/ripple-api](https://github.com/apps/ripple-api)

Free. Open source. Looking for 10 teams to try it and give feedback.

If you've ever spent a day fixing downstream code after an API change, I'd love to hear about your workflow. What contract types do you use? How do you find consumers today? How long does propagation take at your org?

---

*Built in ~6 days as a side project. Currently a solo founder applying to YC. If this resonates, star the repo or install the app — it helps more than you'd think.*
