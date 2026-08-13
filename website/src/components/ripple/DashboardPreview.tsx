import { DASHBOARD_CONTRACT_STATUS, DASHBOARD_LINKS, DASHBOARD_TILES, LINKS } from "@/data/ripple";

export function DashboardPreview() {
  return (
    <div className="grid gap-4 lg:grid-cols-[1.35fr_1fr]">
      {/* Mock dashboard window */}
      <div className="card-surface overflow-hidden">
        <div className="flex items-center justify-between border-b border-border bg-[var(--surface-2)] px-4 py-3">
          <div className="flex items-center gap-2.5">
            <span className="size-2 rounded-full bg-primary" />
            <span className="font-mono text-xs text-muted-foreground">
              ripple-production · /dashboard
            </span>
          </div>
          <a
            href={LINKS.dashboard}
            target="_blank"
            rel="noreferrer"
            className="font-mono text-[11px] text-primary transition-opacity hover:opacity-80"
          >
            open live →
          </a>
        </div>

        <div className="grid grid-cols-2 gap-px bg-border sm:grid-cols-4">
          {DASHBOARD_TILES.map((t) => (
            <div key={t.label} className="bg-[var(--surface)] px-4 py-5">
              <div className="font-mono text-2xl font-semibold">{t.value}</div>
              <div className="mt-1 text-[11px] text-muted-foreground">{t.label}</div>
            </div>
          ))}
        </div>

        <div className="border-t border-border p-5">
          <p className="eyebrow">Monitored repos</p>
          <div className="mt-3 rounded-md border border-dashed border-border px-4 py-5 text-sm text-muted-foreground">
            No repos monitored yet — install the GitHub App to start the pipeline.
          </div>

          <p className="eyebrow mt-6">Supported contracts</p>
          <div className="mt-3 divide-y divide-border overflow-hidden rounded-md border border-border">
            {DASHBOARD_CONTRACT_STATUS.map((c) => (
              <div key={c} className="flex items-center justify-between gap-3 px-4 py-2.5">
                <span className="text-sm text-muted-foreground">{c}</span>
                <span className="flex items-center gap-2 font-mono text-[10.5px] text-[var(--success)]">
                  <span className="size-1.5 rounded-full bg-[var(--success)]" />
                  active · detect → find → fix → PR
                </span>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Links panel */}
      <div className="flex flex-col gap-4">
        <div className="card-surface p-6">
          <p className="eyebrow">Control plane</p>
          <p className="mt-3 text-sm leading-relaxed text-muted-foreground">
            The dashboard is where every monitored repo, generated PR and detected break shows up in
            real time — plus the API surface behind it.
          </p>
          <a
            href={LINKS.dashboard}
            target="_blank"
            rel="noreferrer"
            className="mt-5 block rounded-md bg-primary px-4 py-3 text-center text-sm font-semibold text-primary-foreground transition-opacity hover:opacity-90"
          >
            Open the dashboard →
          </a>
        </div>

        <div className="card-surface divide-y divide-border">
          {DASHBOARD_LINKS.map((l) => (
            <a
              key={l.href}
              href={l.href}
              target="_blank"
              rel="noreferrer"
              className="flex items-center justify-between gap-3 px-5 py-3.5 transition-colors hover:bg-[var(--surface-2)]"
            >
              <span className="text-sm">{l.label}</span>
              <span className="font-mono text-[11px] text-muted-foreground">{l.note} →</span>
            </a>
          ))}
        </div>
      </div>
    </div>
  );
}
