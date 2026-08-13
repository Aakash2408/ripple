import { useEffect, useState } from "react";
import { LINKS, NAV } from "@/data/ripple";

export function Nav() {
  const [scrolled, setScrolled] = useState(false);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 12);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  return (
    <header
      className={`fixed inset-x-0 top-0 z-50 transition-colors duration-300 ${
        scrolled ? "border-b border-border bg-background backdrop-blur-none" : "border-b border-transparent"
      }`}
    >
      <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-5">
        <a href="#top" className="flex items-center gap-2.5">
          <span className="relative flex size-3 items-center justify-center">
            <span className="absolute size-3 rounded-full bg-primary/50 animate-pulse-ring" />
            <span className="size-2 rounded-full bg-primary" />
          </span>
          <span className="font-mono text-[15px] font-semibold tracking-tight">Ripple</span>
        </a>

        <nav className="hidden items-center gap-7 lg:flex">
          {NAV.map((item) => (
            <a
              key={item.href}
              href={item.href}
              className="text-sm text-muted-foreground transition-colors hover:text-foreground"
            >
              {item.label}
            </a>
          ))}
        </nav>

        <div className="flex items-center gap-3">
          <a
            href={LINKS.dashboard}
            target="_blank"
            rel="noreferrer"
            className="hidden text-sm text-muted-foreground transition-colors hover:text-foreground sm:block"
          >
            Dashboard
          </a>
          <a
            href={LINKS.github}
            target="_blank"
            rel="noreferrer"
            className="rounded-md bg-primary px-3.5 py-2 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90"
          >
            Install
          </a>
          <button
            type="button"
            aria-label="Toggle menu"
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
            className="rounded-md border border-border p-2 lg:hidden"
          >
            <span className="block h-px w-4 bg-foreground" />
            <span className="mt-1 block h-px w-4 bg-foreground" />
          </button>
        </div>
      </div>

      {open && (
        <div className="border-t border-border bg-background/95 px-5 py-4 backdrop-blur-xl lg:hidden">
          <div className="grid gap-3">
            {NAV.map((item) => (
              <a
                key={item.href}
                href={item.href}
                onClick={() => setOpen(false)}
                className="text-sm text-muted-foreground transition-colors hover:text-foreground"
              >
                {item.label}
              </a>
            ))}
            <a
              href={LINKS.source}
              target="_blank"
              rel="noreferrer"
              className="text-sm text-muted-foreground hover:text-foreground"
            >
              Source
            </a>
          </div>
        </div>
      )}
    </header>
  );
}
