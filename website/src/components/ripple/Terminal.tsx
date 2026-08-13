import { useEffect, useState } from "react";
import { LINKS } from "@/data/ripple";

type Line = { text: string; tone?: "muted" | "warn" | "ok" | "accent" };

const LINES: Line[] = [
  { text: "# push a breaking change", tone: "muted" },
  { text: "$ git push origin main" },
  { text: "# added required field 'idempotency_key' to POST /payments", tone: "muted" },
  { text: "⚠ 1 breaking change detected", tone: "warn" },
  { text: "  POST /payments — added required field 'idempotency_key'", tone: "muted" },
  { text: "🔍 finding consumers... found 3 across 3 repos", tone: "accent" },
  { text: "✓ Python SDK — parameter added", tone: "ok" },
  { text: "✓ Node SDK — interface updated", tone: "ok" },
  { text: "✓ Java SDK — method signature fixed", tone: "ok" },
  { text: "done in 14.2 seconds.", tone: "accent" },
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
        <span className="ml-3 font-mono text-xs text-muted-foreground">ripple — payments-api</span>
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
        {[
          { label: "Python PR →", href: LINKS.pythonPr },
          { label: "Node PR →", href: LINKS.nodePr },
          { label: "Java PR →", href: LINKS.javaPr },
          { label: "GitLab MR →", href: LINKS.gitlabMr },
        ].map((l) => (
          <a
            key={l.href}
            href={l.href}
            target="_blank"
            rel="noreferrer"
            className="rounded-md border border-border px-2.5 py-1.5 font-mono text-[11px] text-muted-foreground transition-colors hover:border-primary/60 hover:text-foreground"
          >
            {l.label}
          </a>
        ))}
      </div>
    </div>
  );
}
