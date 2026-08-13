import { createFileRoute } from "@tanstack/react-router";
import { Nav } from "@/components/ripple/Nav";
import { Terminal } from "@/components/ripple/Terminal";
import { PropagationGraph } from "@/components/ripple/PropagationGraph";
import { DashboardPreview } from "@/components/ripple/DashboardPreview";
import { Footer } from "@/components/ripple/Footer";
import {
  COMPARE_COLUMNS,
  COMPARE_ROWS,
  CONTRACTS,
  INTELLIGENCE,
  LINKS,
  PRICING,
  RESEARCH_STATS,
  SELF_HOSTED_TARGETS,
  STATS,
  STEPS,
  WORKS_WITH,
} from "@/data/ripple";

const TITLE = "Ripple — Self-maintaining APIs that fix breaking changes";
const DESCRIPTION =
  "Push a breaking change and Ripple detects it, traces every consumer across your repos, writes the fix and opens the PRs in about 15 seconds. 10 contract types, 7 platforms.";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: TITLE },
      { name: "description", content: DESCRIPTION },
      { property: "og:title", content: TITLE },
      { property: "og:description", content: DESCRIPTION },
    ],
  }),
  component: Index,
});

function Section({
  id,
  index,
  eyebrow,
  title,
  lead,
  children,
}: {
  id: string;
  index: string;
  eyebrow: string;
  title: string;
  lead?: string;
  children: React.ReactNode;
}) {
  return (
    <section id={id} className="border-t border-border py-20 sm:py-28">
      <div className="mx-auto max-w-6xl px-5">
        <div className="flex items-baseline gap-4">
          <span className="font-mono text-xs text-primary">{index}</span>
          <span className="eyebrow">{eyebrow}</span>
        </div>
        <h2 className="mt-4 max-w-3xl text-3xl font-semibold tracking-tight sm:text-4xl">{title}</h2>
        {lead && <p className="mt-3 max-w-2xl text-muted-foreground">{lead}</p>}
        <div className="mt-12">{children}</div>
      </div>
    </section>
  );
}

function Index() {
  return (
    <div id="top" className="min-h-screen bg-background">
      <Nav />

      <main>
        {/* Hero */}
        <section className="relative overflow-hidden pt-32 pb-20 sm:pt-40">
          <div className="pointer-events-none absolute inset-0 grid-bg opacity-40" />
          <div className="pointer-events-none absolute inset-x-0 top-0 h-[560px] glow" />
          <div className="relative mx-auto max-w-6xl px-5">
            <div className="flex items-center gap-4">
              <span className="eyebrow">Self-maintaining APIs</span>
              <span className="h-px flex-1 bg-border" />
              <span className="font-mono text-xs text-muted-foreground">v1 · open source</span>
            </div>

            <h1 className="mt-10 text-[clamp(2.5rem,8vw,5.5rem)] font-bold leading-[0.95] tracking-tight">
              <span className="block">API changes</span>
              <span className="block text-muted-foreground/50">break things.</span>
              <span className="block text-primary">Ripple fixes them.</span>
            </h1>

            <div className="mt-12 grid gap-10 lg:grid-cols-2 lg:items-start">
              <div>
                <p className="max-w-lg text-lg leading-relaxed text-muted-foreground">
                  Push a breaking change. Ripple detects it, traces every consumer across your repos,
                  writes the fix and opens the PRs — in about{" "}
                  <span className="font-mono text-foreground">15s</span>.
                </p>

                <div className="mt-8 flex flex-wrap gap-3">
                  <a
                    href={LINKS.github}
                    target="_blank"
                    rel="noreferrer"
                    className="rounded-md bg-primary px-5 py-3 text-sm font-semibold text-primary-foreground transition-opacity hover:opacity-90"
                  >
                    Install on GitHub
                  </a>
                  <a
                    href={LINKS.gitlab}
                    target="_blank"
                    rel="noreferrer"
                    className="rounded-md border border-border px-5 py-3 text-sm font-medium transition-colors hover:border-primary/60"
                  >
                    GitLab
                  </a>
                  <a
                    href={LINKS.bitbucket}
                    target="_blank"
                    rel="noreferrer"
                    className="rounded-md border border-border px-5 py-3 text-sm font-medium transition-colors hover:border-primary/60"
                  >
                    Bitbucket
                  </a>
                </div>

                <div className="mt-6 flex flex-wrap items-center gap-x-6 gap-y-2 font-mono text-xs text-muted-foreground">
                  <a href="#self-hosted" className="transition-colors hover:text-primary">
                    self-hosted →
                  </a>
                  <a
                    href={LINKS.source}
                    target="_blank"
                    rel="noreferrer"
                    className="transition-colors hover:text-primary"
                  >
                    source →
                  </a>
                  <a
                    href={LINKS.dashboard}
                    target="_blank"
                    rel="noreferrer"
                    className="transition-colors hover:text-primary"
                  >
                    dashboard →
                  </a>
                </div>

                <div className="mt-10">
                  <PropagationGraph />
                </div>
              </div>

              <Terminal />
            </div>

            <div className="mt-14 grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-border bg-border sm:grid-cols-4">
              {STATS.map((s) => (
                <div key={s.label} className="bg-[var(--surface)] px-5 py-6">
                  <div className="font-mono text-3xl font-semibold text-foreground">{s.value}</div>
                  <div className="mt-1 text-xs text-muted-foreground">{s.label}</div>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* Marquee */}
        <div className="overflow-hidden border-y border-border py-4">
          <div className="flex w-max animate-marquee gap-8 font-mono text-sm text-muted-foreground">
            {[...CONTRACTS, ...CONTRACTS].map((c, i) => (
              <span key={i} className="flex items-center gap-8 whitespace-nowrap">
                {c.name}
                <span className="text-primary">◆</span>
              </span>
            ))}
          </div>
        </div>

        {/* Contracts */}
        <Section
          id="contracts"
          index="01"
          eyebrow="One tool, every surface"
          title="10 contract types, natively understood"
          lead="Not just REST. Ripple parses each contract, diffs it semantically and knows what a break actually means downstream."
        >
          <div className="grid grid-cols-2 gap-4 md:grid-cols-3 lg:grid-cols-5">
            {CONTRACTS.map((c) => (
              <div key={c.name} className="card-surface card-surface-hover p-5">
                <div className="inline-flex items-center justify-center rounded border border-primary/40 bg-primary/10 px-2 py-1 font-mono text-[11px] font-semibold text-primary">{c.icon}</div>
                <div className="mt-4 font-medium">{c.name}</div>
                <div className="font-mono text-xs text-muted-foreground">{c.sub}</div>
              </div>
            ))}
          </div>
        </Section>

        {/* Intelligence */}
        <Section
          id="intelligence"
          index="02"
          eyebrow="It gets smarter"
          title="Not just grep. It learns your codebase."
          lead="Three independent learning channels combine into a persistent, repo-specific dependency map."
        >
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {INTELLIGENCE.map((f) => (
              <div key={f.title} className="card-surface card-surface-hover p-6">
                <span className="font-mono text-xs text-primary/60">
                  {String(INTELLIGENCE.indexOf(f) + 1).padStart(2, "0")}
                </span>
                <h3 className="mt-4 font-semibold">{f.title}</h3>
                <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{f.body}</p>
              </div>
            ))}
          </div>
        </Section>

        {/* How it works */}
        <Section
          id="how-it-works"
          index="03"
          eyebrow="How it works"
          title="Three steps, then it runs itself"
        >
          <div className="grid gap-4 md:grid-cols-3">
            {STEPS.map((s) => (
              <div key={s.n} className="card-surface card-surface-hover p-7">
                <span className="font-mono text-4xl font-semibold text-primary/40">{s.n}</span>
                <h3 className="mt-6 text-lg font-semibold">{s.title}</h3>
                <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{s.body}</p>
              </div>
            ))}
          </div>

          <div className="mt-10">
            <p className="eyebrow">Works with</p>
            <div className="mt-4 flex flex-wrap gap-2">
              {WORKS_WITH.map((w) => (
                <span
                  key={w}
                  className="rounded-full border border-border px-3.5 py-1.5 font-mono text-xs text-muted-foreground"
                >
                  {w}
                </span>
              ))}
            </div>
          </div>
        </Section>

        {/* Dashboard */}
        <Section
          id="dashboard"
          index="04"
          eyebrow="Live control plane"
          title="Your dashboard, always watching"
          lead="Repos monitored, PRs created, breaks detected and every contract adapter — in one place, with the API endpoints behind it one click away."
        >
          <DashboardPreview />
        </Section>

        {/* Compare */}
        <Section
          id="compare"
          index="05"

          eyebrow="How Ripple compares"
          title="The only tool that does the full loop"
          lead="detect → find → fix → PR. Everyone else stops at step one."
        >
          <div className="card-surface overflow-x-auto">
            <table className="w-full min-w-[640px] border-collapse text-sm">
              <thead>
                <tr className="border-b border-border">
                  <th className="px-5 py-4 text-left font-normal text-muted-foreground">Capability</th>
                  {COMPARE_COLUMNS.map((c, i) => (
                    <th
                      key={c}
                      className={`px-5 py-4 text-center font-mono text-xs ${
                        i === 0 ? "text-primary" : "text-muted-foreground"
                      }`}
                    >
                      {c}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {COMPARE_ROWS.map((row) => (
                  <tr key={row.label} className="border-b border-border last:border-0">
                    <td className="px-5 py-3.5 text-muted-foreground">{row.label}</td>
                    {row.values.map((v, i) => (
                      <td
                        key={i}
                        className={`px-5 py-3.5 text-center font-mono text-xs ${
                          i === 0
                            ? "bg-primary/5 font-semibold text-foreground"
                            : v === "—"
                              ? "text-muted-foreground/50"
                              : "text-muted-foreground"
                        }`}
                      >
                        {v}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>

        {/* Research */}
        <Section
          id="research"
          index="06"
          eyebrow="Research-backed"
          title="Built on PropBench"
          lead={`A benchmark proving most "engineering judgment" is learnable.`}
        >
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {RESEARCH_STATS.map((s) => (
              <div key={s.label} className="card-surface p-6">
                <div className="font-mono text-3xl font-semibold text-primary">{s.value}</div>
                <p className="mt-2 text-sm text-muted-foreground">{s.label}</p>
              </div>
            ))}
          </div>

          <div className="card-surface mt-4 p-7">
            <p className="text-lg">
              <span className="font-mono text-sm text-muted-foreground">Finding — </span>
              39% of propagation targets are cross-package, invisible to any single-repo tool. Tested on 874 real changes across 50 OSS repos + production codebases.
            </p>
            <p className="mt-4 font-mono text-sm text-muted-foreground">
              naming alone: 4.3% recall · + git history: 30% · + ensemble:{" "}
              <span className="text-[var(--success)]">82%</span>
            </p>
            <div className="mt-6 flex flex-wrap gap-3">
              <a
                href={LINKS.research}
                target="_blank"
                rel="noreferrer"
                className="inline-block rounded-md border border-border px-4 py-2.5 text-sm transition-colors hover:border-primary/60"
              >
                GitHub (dataset + code) →
              </a>
              <a
                href={LINKS.researchPaper}
                target="_blank"
                rel="noreferrer"
                className="inline-block rounded-md border border-primary/40 bg-primary/5 px-4 py-2.5 text-sm transition-colors hover:border-primary/60"
              >
                Read the paper (ResearchGate) →
              </a>
            </div>
          </div>
        </Section>

        {/* Self hosted */}
        <Section
          id="self-hosted"
          index="07"
          eyebrow="Self-hosted agent"
          title="For custom platforms, on-prem Git and air-gapped networks"
        >
          <div className="grid gap-4 lg:grid-cols-2">
            <div className="card-surface p-6 font-mono text-sm">
              <div className="text-muted-foreground">$ ripple-agent scan /path/to/repo</div>
              <div className="mt-3 text-muted-foreground">
                $ docker run -v /repos:/repos ripple-agent watch
              </div>
              <div className="mt-6 text-[var(--success)]">✓ agent connected · 0 external calls</div>
            </div>
            <div className="grid grid-cols-2 gap-4">
              {SELF_HOSTED_TARGETS.map((t) => (
                <div
                  key={t}
                  className="card-surface card-surface-hover flex items-center justify-center p-6 font-mono text-sm text-muted-foreground"
                >
                  {t}
                </div>
              ))}
            </div>
          </div>
        </Section>

        {/* Pricing */}
        <Section id="pricing" index="08" eyebrow="Pricing" title="Free to start. Scales with your org.">
          <div className="grid gap-4 lg:grid-cols-3">
            {PRICING.map((p) => (
              <div
                key={p.name}
                className={`card-surface flex flex-col p-7 ${
                  p.featured ? "border-primary/60 bg-primary/5" : "card-surface-hover"
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className="font-mono text-xs uppercase tracking-widest text-muted-foreground">
                    {p.name}
                  </span>
                  {p.featured && (
                    <span className="rounded-full bg-primary px-2.5 py-1 font-mono text-[10px] text-primary-foreground">
                      popular
                    </span>
                  )}
                </div>
                <div className="mt-6 flex items-baseline gap-2">
                  <span className="text-4xl font-semibold tracking-tight">{p.price}</span>
                  <span className="font-mono text-xs text-muted-foreground">{p.note}</span>
                </div>
                <ul className="mt-7 flex-1 space-y-3">
                  {p.features.map((f) => (
                    <li key={f} className="flex items-start gap-2.5 text-sm text-muted-foreground">
                      <span className="mt-0.5 text-[var(--success)]">✓</span>
                      {f}
                    </li>
                  ))}
                </ul>
                <a
                  href={p.cta.href}
                  target="_blank"
                  rel="noreferrer"
                  className={`mt-8 rounded-md px-4 py-3 text-center text-sm font-medium transition-opacity hover:opacity-90 ${
                    p.featured
                      ? "bg-primary text-primary-foreground"
                      : "border border-border text-foreground hover:border-primary/60"
                  }`}
                >
                  {p.cta.label}
                </a>
              </div>
            ))}
          </div>
        </Section>

        {/* CTA */}
        <section className="relative overflow-hidden border-t border-border py-24">
          <div className="pointer-events-none absolute inset-0 grid-bg opacity-30" />
          <div className="pointer-events-none absolute inset-x-0 bottom-0 h-[420px] glow" />
          <div className="relative mx-auto max-w-3xl px-5 text-center">
            <h2 className="text-3xl font-semibold tracking-tight sm:text-5xl">
              Ship the break. <span className="text-primary">Keep the peace.</span>
            </h2>
            <p className="mx-auto mt-4 max-w-xl text-muted-foreground">
              Install once and every future breaking change arrives with its fix already written.
            </p>
            <div className="mt-8 flex flex-wrap justify-center gap-3">
              <a
                href={LINKS.github}
                target="_blank"
                rel="noreferrer"
                className="rounded-md bg-primary px-6 py-3 text-sm font-semibold text-primary-foreground transition-opacity hover:opacity-90"
              >
                Install on GitHub
              </a>
              <a
                href={LINKS.dashboard}
                target="_blank"
                rel="noreferrer"
                className="rounded-md border border-border px-6 py-3 text-sm font-medium transition-colors hover:border-primary/60"
              >
                Open dashboard →
              </a>
            </div>
          </div>
        </section>
      </main>

      <Footer />
    </div>
  );
}
