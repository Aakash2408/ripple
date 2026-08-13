const NODES = [
  { id: "python-sdk", x: 78, y: 16 },
  { id: "node-sdk", x: 86, y: 42 },
  { id: "java-sdk", x: 80, y: 68 },
  { id: "billing-svc", x: 62, y: 90 },
];

export function PropagationGraph() {
  return (
    <div className="card-surface p-5">
      <p className="eyebrow">Propagation graph</p>
      <div className="relative mt-4 h-[240px] w-full">
        <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="absolute inset-0 h-full w-full">
          {NODES.map((n, i) => (
            <path
              key={n.id}
              d={`M 14 50 C 40 50, 45 ${n.y}, ${n.x - 4} ${n.y}`}
              fill="none"
              stroke="var(--primary)"
              strokeOpacity="0.45"
              strokeWidth="0.5"
              vectorEffect="non-scaling-stroke"
              strokeDasharray="3 3"
              style={{ animation: `marquee ${14 + i * 3}s linear infinite` }}
            />
          ))}
        </svg>

        <div className="absolute left-[6%] top-1/2 -translate-y-1/2">
          <div className="rounded-md border border-primary/60 bg-primary/10 px-3 py-2 text-center">
            <span className="font-mono text-[11px] text-primary">spec</span>
            <p className="font-mono text-[10px] text-muted-foreground">POST /payments</p>
          </div>
        </div>

        {NODES.map((n) => (
          <div
            key={n.id}
            className="absolute -translate-y-1/2"
            style={{ left: `${n.x - 16}%`, top: `${n.y}%` }}
          >
            <div className="flex items-center gap-2 rounded-md border border-border bg-[var(--surface-2)] px-2.5 py-1.5">
              <span className="size-1.5 rounded-full bg-[var(--success)]" />
              <span className="font-mono text-[10.5px] text-muted-foreground">{n.id}</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
