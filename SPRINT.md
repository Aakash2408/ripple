# 10-DAY SPRINT: Self-Maintaining APIs → YC Application

## Product Name: Ripple (working title)

**One-liner:** When you change an API, we find every consumer and open PRs to fix them.

---

## THE PLAN

### Day 1 (TODAY): OpenAPI Diff Engine
- [ ] Parse two versions of an OpenAPI spec
- [ ] Detect breaking changes (field removed, renamed, type changed, required added)
- [ ] Output: structured list of breaking changes with severity
- **Done when:** `ripple diff old.yaml new.yaml` prints breaking changes

### Day 2: Consumer Finder
- [ ] Scan a GitHub org/repos for files that reference the changed endpoints
- [ ] Search: SDK imports, endpoint URLs, type references, generated client code
- [ ] Output: list of (repo, file, line) that consume the changed API
- **Done when:** Given a breaking change, it finds 3+ real consumer files

### Day 3: Fix Generator
- [ ] For each consumer + breaking change, generate the fix
- [ ] Use Claude API: "This API changed X→Y. Here's the consumer code. Generate the minimal fix."
- [ ] Validate: generated code must be syntactically valid
- **Done when:** It generates correct fix diffs for 3 different consumer files

### Day 4: PR Engine
- [ ] Create branch in consumer repo
- [ ] Commit the generated fix
- [ ] Open PR with explanation (what changed, why, evidence)
- [ ] PR body includes: API change summary + confidence + link to source change
- **Done when:** Running the tool creates a real PR on GitHub

### Day 5: GitHub App (Webhook)
- [ ] Register GitHub App on github.com
- [ ] Webhook: trigger on push to main when spec files change
- [ ] Wire together: detect change → find consumers → generate fixes → open PRs
- **Done when:** Push a spec change → PRs appear in consumer repos automatically

### Day 6: Polish + Test
- [ ] Test on 3 real repos (create test repos if needed)
- [ ] Fix edge cases and failures
- [ ] Add README, install instructions
- [ ] Make the PR comments look professional
- **Done when:** End-to-end works reliably 3/3 times

### Day 7: Deploy + Landing Page
- [ ] Deploy to Railway/Fly.io
- [ ] Buy domain (ripple.dev or tryripple.com or similar)
- [ ] Single-page landing site (can be plain HTML)
- [ ] "Install on GitHub" button
- **Done when:** Someone can install your app from a URL

### Day 8: Record Demo + Get Users
- [ ] Record 60-second demo video (screen recording)
- [ ] Post on Twitter/X, share in dev communities
- [ ] Install on 2-3 friends' repos, get feedback
- **Done when:** Video exists, 3+ installs

### Day 9: Write YC Application
- [ ] Fill out the YC form (see below for answers)
- [ ] Attach demo video
- [ ] Describe traction (even if just 3 installs)
- **Done when:** Application is complete and reviewed

### Day 10: Submit + Buffer
- [ ] Final review of application
- [ ] Submit to YC
- [ ] Start working on anything that came up from Day 8 feedback
- **Done when:** SUBMITTED

---

## TECH STACK (no overthinking)

```
Language:    Python (fastest to iterate)
Framework:  FastAPI (webhook receiver)
LLM:        Claude API (fix generation)
GitHub:     PyGithub (API interactions)
Hosting:    Railway or Fly.io ($5/month)
Spec parse: openapi-spec-validator + pyyaml
Landing:    Single HTML file (no React, no framework)
```

---

## REPO STRUCTURE

```
ripple/
├── app/
│   ├── main.py           # FastAPI webhook handler
│   ├── diff_engine.py    # OpenAPI spec diff
│   ├── consumer_finder.py # Find affected repos/files
│   ├── fix_generator.py  # LLM-powered fix generation
│   ├── pr_engine.py      # Create branches + PRs
│   └── models.py         # Data models
├── tests/
│   └── test_diff.py
├── landing/
│   └── index.html
├── README.md
├── requirements.txt
└── Dockerfile
```

---

## YC APPLICATION KEY ANSWERS (draft)

**What does your company do?**
> Ripple automatically fixes downstream code when APIs change. When you push a breaking change to your API spec, Ripple finds every consumer across your org and opens PRs with the exact fixes needed — in seconds, not days.

**Why did you pick this idea?**
> I manage 18+ repositories at Amazon. Every week, someone changes an API and 3 teams find out when their code breaks. I spent 30% of my time propagating changes manually. I measured it: 81% of these changes are predictable from patterns + code structure. I automated it.

**What's your demo?**
> [60-second video showing: spec change pushed → 3 PRs appear in consumer repos within 30 seconds]

**How do you make money?**
> Free for public repos. $49/month for private repos. $199/month for org-wide. Enterprise custom pricing for SSO + audit.

**What's your unfair advantage?**
> I've built a benchmark of 257 real engineering propagation tasks proving this is predictable. The consumer dependency graph I build per-org becomes a moat — the more you use it, the better it knows your architecture.

---

## RULES FOR THE SPRINT

1. NO brainstorming. Plan is set. Execute only.
2. NO pivoting. This is the product. Ship it ugly.
3. NO perfection. 60% quality, 100% shipped > 100% quality, 0% shipped.
4. If stuck for >1 hour on anything, ask me. Don't spin.
5. Commit every few hours. Never lose work.
6. Every day ends with a working increment.
