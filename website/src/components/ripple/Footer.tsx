import { useState } from "react";
import { LINKS, NAV } from "@/data/ripple";

export function Footer() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);

  return (
    <footer className="border-t border-border">
      <div className="mx-auto max-w-6xl px-5 py-16">
        <div className="card-surface flex flex-col gap-6 p-8 md:flex-row md:items-center md:justify-between">
          <div>
            <h2 className="text-2xl font-semibold tracking-tight">Stay updated</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Get notified when we ship new contract types and adapters.
            </p>
          </div>
          <form
            className="flex w-full max-w-md gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              if (email.trim()) setSent(true);
            }}
          >
            <input
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@company.com"
              aria-label="Email address"
              className="h-11 flex-1 rounded-md border border-border bg-background px-3 font-mono text-sm outline-none transition-colors placeholder:text-muted-foreground focus:border-primary"
            />
            <button
              type="submit"
              className="h-11 rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90"
            >
              {sent ? "Subscribed" : "Subscribe"}
            </button>
          </form>
        </div>

        <div className="mt-14 grid gap-10 sm:grid-cols-2 lg:grid-cols-4">
          <div>
            <div className="flex items-center gap-2.5">
              <span className="size-2 rounded-full bg-primary" />
              <span className="font-mono text-sm font-semibold">Ripple</span>
            </div>
            <p className="mt-3 max-w-xs text-sm text-muted-foreground">
              Self-maintaining APIs. Detect the break, find the consumers, write the fix, open the PR.
            </p>
          </div>

          <FooterCol
            title="Product"
            links={NAV.slice(0, 4).map((n) => ({ label: n.label, href: n.href }))}
          />
          <FooterCol
            title="Install"
            links={[
              { label: "GitHub App", href: LINKS.github, external: true },
              { label: "GitLab", href: LINKS.gitlab, external: true },
              { label: "Bitbucket", href: LINKS.bitbucket, external: true },
              { label: "Dashboard", href: LINKS.dashboard, external: true },
            ]}
          />
          <FooterCol
            title="Resources"
            links={[
              { label: "Source code", href: LINKS.source, external: true },
              { label: "PropBench research", href: LINKS.research, external: true },
              { label: "Ripple-opened PRs", href: LINKS.allFixPrs, external: true },
              { label: "GitLab MR", href: LINKS.gitlabMr, external: true },
            ]}
          />
        </div>

        <div className="mt-12 flex flex-col gap-2 border-t border-border pt-6 font-mono text-xs text-muted-foreground sm:flex-row sm:items-center sm:justify-between">
          <span>© {new Date().getFullYear()} Ripple — Built by{" "}
            <a href="https://www.linkedin.com/in/aakash-sangwan/" target="_blank" rel="noreferrer" className="text-foreground hover:text-primary transition-colors">Aakash Sangwan</a>
          </span>
          <span>detect → find → fix → PR</span>
        </div>
      </div>
    </footer>
  );
}

function FooterCol({
  title,
  links,
}: {
  title: string;
  links: { label: string; href: string; external?: boolean }[];
}) {
  return (
    <div>
      <p className="eyebrow">{title}</p>
      <ul className="mt-3 space-y-2.5">
        {links.map((l) => (
          <li key={l.label}>
            <a
              href={l.href}
              target={l.external ? "_blank" : undefined}
              rel={l.external ? "noreferrer" : undefined}
              className="text-sm text-muted-foreground transition-colors hover:text-foreground"
            >
              {l.label}
            </a>
          </li>
        ))}
      </ul>
    </div>
  );
}
