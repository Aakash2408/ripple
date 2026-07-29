# YC Application — Ripple (Final Version)

## Company name
Ripple

## One-liner
When you change an API, we automatically find every consumer and open PRs to fix them.

## What does your company do?
Ripple monitors your API specs for breaking changes. When one happens, it automatically finds every downstream consumer across your org — in any language — generates the minimal code fix, and opens a PR. Developers review and merge instead of spending days on cross-team coordination.

Think Dependabot, but for your own API contracts instead of library versions.

## Why did you pick this idea to work on?
I'm an SDE at Amazon managing 18+ repositories across multiple teams. Every week, someone changes an API and 3-5 teams discover it when their code breaks — sometimes in production on a Friday night.

I tracked 257 real engineering changes across our team and measured: 81% of "what else needs updating" is predictable from code structure + organizational patterns. I automated that 81%.

This isn't a guess. I built a benchmark (PropBench) proving change propagation is predictable before writing a line of product code.

## Why now?
Three things converged in the last 12 months:

1. **OpenAPI adoption hit critical mass** — most APIs now have machine-readable specs. Five years ago, they didn't.
2. **LLMs can generate correct multi-language code fixes** — template-only approaches couldn't handle the diversity of consumer code. Now Claude/GPT can.
3. **GitHub Apps made org-wide installation trivial** — one click gives you access to every repo. The distribution mechanism didn't exist before 2020.

Two years ago, none of these were true. In two years, this will be table stakes. Now is the window.

## Demo URL
https://github.com/Aakash2408/ripple-sdk-python/pull/1

Full demo: one API change → 3 PRs in Python, Node, and Java SDKs:
- Python: https://github.com/Aakash2408/ripple-sdk-python/pull/1
- Node: https://github.com/Aakash2408/ripple-sdk-node/pull/1
- Java: https://github.com/Aakash2408/ripple-sdk-java/pull/1

## Product URL
https://github.com/apps/ripple-api (installable now)

## How far along are you?
- **Product**: Deployed, live, working end-to-end
- **GitHub App**: Registered, installable by anyone
- **Server**: https://ripple-production-be7f.up.railway.app (Railway, auto-deploys from GitHub)
- **Demo**: 4 real PRs across 4 repos in 3 languages
- **Source**: https://github.com/Aakash2408/ripple (open)
- **Time to build**: Idea to production in 12 hours

## How will you make money?
Free for public repos (viral growth). Paid for private repos:
- Starter: $49/month (1 org, private repos)
- Team: $199/month (unlimited repos, org-wide scanning, priority)
- Enterprise: Custom (SSO, audit log, self-hosted, SLA)

Target: $2K-$10K/month per mid-size company (5-50 services).

## How many users do you have?
Pre-launch. Built the complete MVP in one day. Currently onboarding first 5 beta users from developer communities.

## What's your growth plan?
**Week 1-2**: 10 GitHub App installs from dev communities (HN, Twitter, Reddit)
**Week 3-4**: 3 teams using it weekly, measuring PR merge rate
**Month 2**: First paying customer
**Month 3**: GitLab support (enterprise demand)

Growth loop: every merged Ripple PR saves a team 2-3 days → they install on more repos → more data → better predictions.

## Why will you win?
1. **I lived this pain** — 3 years managing 18+ repos at Amazon, dealing with API propagation weekly
2. **Research-backed** — built a 257-entry benchmark proving propagation is predictable before writing product code
3. **Moat grows with usage** — the consumer dependency graph I build per-org is proprietary data that improves over time
4. **Platform-agnostic** — GitHub today, GitLab and Bitbucket next month. No single platform will build cross-platform propagation
5. **I ship fast** — zero to production in 12 hours

## What's the market size?
- **Direct**: Every company with 5+ microservices (hundreds of thousands of companies)
- **Comparable**: API management is $5B+ (Postman valued at $5.6B, Kong at $1.4B)
- **Expansion**: Same engine works for Protobuf, GraphQL, database schemas, Terraform modules — every contract that ripples across codebases
- **Pricing reference**: Snyk ($8.5B valuation) charges $50-500/month for vulnerability fix PRs. We do the same for API contract fixes.

## Who are your competitors?
| Competitor | What they do | What they DON'T do |
|---|---|---|
| Dependabot/Renovate | Bump library versions | Don't fix YOUR API contract changes |
| Optic | Detect API changes | Don't fix consumers |
| Speakeasy | Generate SDKs | Don't propagate to EXISTING consumers |
| AI code review (Copilot) | Review what you wrote | Don't fix what you FORGOT |

**Nobody does: detect break → find consumers → generate fix → open PR.** That's the full loop only Ripple completes.

## What's the biggest risk? What do you do if it doesn't work?
**Risk**: GitHub adds "API propagation" as a native feature.

**Counter**: 
- GitHub only sees GitHub. We work across GitHub + GitLab + Bitbucket.
- GitHub only sees one org. We can propagate across orgs (API provider → external consumers).
- Platform features ship slow (2-3 years). We ship in days.

**If it doesn't work**: The core engine (detect change → find affected code → generate fix) applies to database migrations, Terraform modules, and SDK version upgrades. We pivot to whichever contract type has the most pull.

## Solo founder?
Yes. I built this from zero to deployed product in 12 hours because I move faster alone than most teams of 3. I'm looking for a technical co-founder with API/developer-tools experience — someone who's felt this pain at scale.

## Founder bio
**Aakash Sangwan**. SDE at Amazon (3+ years). Built production ML pipelines processing millions of delivery events daily. Manage 18+ repositories across multiple teams. Deep experience with cross-service API propagation pain.

Previously: SDE at Sortly. Open source contributor (63 public repos).

- GitHub: https://github.com/Aakash2408
- LinkedIn: https://linkedin.com/in/aakash-sangwan-0790aa172

## Is there anything else you'd like us to know?

I didn't start with a pitch deck. I started by measuring the problem.

I built PropBench — a benchmark of 257 real engineering change-propagation tasks — and discovered that 81% of "what else needs updating" is predictable from patterns + code structure. Only then did I build the product.

Then I went from idea to production in 12 hours: working webhook server, registered GitHub App, 4 real PRs across 3 languages, deployed on Railway.

I don't wait for perfect. I ship, measure, and iterate.

---

## Demo Video Script (60 seconds)

**[0-5s]** "Every week at every company with microservices, someone changes an API and three teams find out when their code breaks."

**[5-15s]** "Ripple fixes that automatically. Watch:"
*[show: pushing the v2 spec with idempotency_key]*

**[15-30s]** "I just added a required field to the Payments API. Ripple detected the breaking change, found 3 SDK consumers in Python, Node, and Java..."
*[show: terminal output or GitHub notifications]*

**[30-45s]** "...and opened fix PRs in all three repos — with the correct code change in each language."
*[show: click into Python PR, show the diff — clean, adds the parameter]*

**[45-55s]** "One API change. Three repos fixed. Fifteen seconds. No human coordination needed."

**[55-60s]** "Install at github.com/apps/ripple-api. Try it on your repos."
