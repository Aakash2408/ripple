import { useEffect, useState } from "react";
import { LINKS } from "@/data/ripple";

type Line = { text: string; tone?: "muted" | "warn" | "ok" | "accent" };

const LINES: Line[] = [
  { text: "# push a breaking change to user.proto", tone: "muted" },
  { text: "$ git push origin main" },
  { text: "# removed field 'phone_number' from User message", tone: "muted" },
  { text: "⚠ 1 breaking change detected", tone: "warn" },
  { text: "  user.proto — removed required field 'phone_number'", tone: "muted" },
  { text: "🔍 finding consumers... found 4 across 3 repos", tone: "accent" },
  { text: "✓ auth-service/handler.go — reference removed", tone: "ok" },
  { text: "✓ billing-api/UserClient.ts — interface updated", tone: "ok" },
  { text: "✓ notifications/user_svc.py — parameter dropped", tone: "ok" },
  { text: "✓ tests/test_user_api.java — assertion fixed", tone: "ok" },
  { text: "done in 14.2 seconds. 4 PRs opened.", tone: "accent" },
];

const toneClass = {
  muted: "text-muted-foreground",
  warn: "text-[var(--warning)]",
  ok: "text-[var(--success)]",
  accent: "text-accent",
} as const;

export function Terminal() {
  const [count, setCount] = useState(0);

  useEffect(() => {
    if (count >= LINES.length) return;
    const t = setTimeout(() => setCount((c) => c + 1), count === 0 ? 400 : 520);
    return () => clearTimeout(t);
  }, [count]);

  return (
    <div className="card-surface overflow-hidden">
      <div className="flex items-center gap-2 border-b border-border bg-[var(--surface-2)] px-4 py-3">
        <span className="size-2.5 rounded-full bg-destructive/80" />
        <span className="size-2.5 rounded-full bg-[var(--warning)]/80" />
        <span className="size-2.5 rounded-full bg-[var(--success)]/80" />
        <span className="ml-3 font-mono text-xs text-muted-foreground">ripple — platform/user-service</span>
      </div>
      <div className="min-h-[290px] p-4 font-mono text-[12.5px] leading-7 sm:text-[13px]">
        {LINES.slice(0, count).map((line, i) => (
          <div key={i} className={line.tone ? toneClass[line.tone] : "text-foreground"}>
            {line.text}
          </div>
        ))}
        <span className="inline-block h-4 w-2 translate-y-0.5 animate-pulse bg-primary" />
      </div>
      <div className="flex flex-wrap gap-2 border-t border-border px-4 py-3">
        <a
          href="https://ripple-production-be7f.up.railway.app/dry-run"
          target="_blank"
          rel="noreferrer"
          className="rounded-md border border-primary/40 bg-primary/5 px-2.5 py-1.5 font-mono text-[11px] text-primary transition-colors hover:border-primary/60 hover:text-foreground"
        >
          Try it yourself (dry run) →
        </a>
        <a
          href={LINKS.source}
          target="_blank"
          rel="noreferrer"
          className="rounded-md border border-border px-2.5 py-1.5 font-mono text-[11px] text-muted-foreground transition-colors hover:border-primary/60 hover:text-foreground"
        >
          View source →
        </a>
      </div>
    </div>
  );
}
