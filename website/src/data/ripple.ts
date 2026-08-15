export const LINKS = {
  github: "https://github.com/apps/ripple-api",
  gitlab: "https://ripple-production-be7f.up.railway.app/auth/gitlab",
  bitbucket: "https://ripple-production-be7f.up.railway.app/auth/bitbucket",
  dashboard: "https://ripple-production-be7f.up.railway.app/dashboard",
  source: "https://github.com/Aakash2408/ripple",
  research: "https://github.com/Aakash2408/Propbench",
  researchPaper: "https://www.researchgate.net/publication/412220063_PropBench_A_Benchmark_for_Measuring_Engineering_Judgment_in_Change_Propagation",
  pythonPr: "https://github.com/Aakash2408/ripple-sdk-python/pull/1",
  nodePr: "https://github.com/Aakash2408/ripple-sdk-node/pull/1",
  javaPr: "https://github.com/Aakash2408/ripple-sdk-java/pull/1",
  gitlabMr: "https://gitlab.com/ripple-api2/Ripple/-/merge_requests/1",
  docs: "https://ripple-production-be7f.up.railway.app/docs",
  health: "https://ripple-production-be7f.up.railway.app/health",
  gitlabSetup: "https://ripple-production-be7f.up.railway.app/setup/gitlab",
  rateLimit: "https://ripple-production-be7f.up.railway.app/rate-limit/unknown",
  landing: "https://ripple-cnn.pages.dev/",
} as const;

export const DASHBOARD_TILES = [
  { value: "0", label: "Repos monitored" },
  { value: "0", label: "PRs created" },
  { value: "0", label: "Breaks detected" },
  { value: "12", label: "Fix languages" },
];

export const DASHBOARD_CONTRACT_STATUS = [
  "OpenAPI / Swagger",
  "Protobuf (gRPC)",
  "GraphQL",
  "Database (SQL + Prisma)",
  "AsyncAPI (Kafka, SNS, MQTT)",
  "Avro (Confluent/Kafka)",
  "tRPC (TypeScript)",
  "Thrift (Apache/Meta)",
  "JSON Schema",
  "Smithy (AWS)",
];

export const DASHBOARD_LINKS = [
  { label: "Open dashboard", href: LINKS.dashboard, note: "live control plane" },
  { label: "API docs (Swagger UI)", href: LINKS.docs, note: "/docs" },
  { label: "Health check", href: LINKS.health, note: "/health" },
  { label: "GitLab manual setup", href: LINKS.gitlabSetup, note: "/setup/gitlab" },
  { label: "Rate limit status", href: LINKS.rateLimit, note: "/rate-limit" },
  { label: "Install on more repos", href: LINKS.github, note: "GitHub App" },
];

export const NAV = [
  { label: "Contracts", href: "#contracts" },
  { label: "Intelligence", href: "#intelligence" },
  { label: "How it works", href: "#how-it-works" },
  { label: "Dashboard", href: "#dashboard" },
  { label: "Compare", href: "#compare" },
  { label: "Research", href: "#research" },
  { label: "Pricing", href: "#pricing" },
];


export const STATS = [
  { value: "10", label: "contract types" },
  { value: "50+", label: "detections" },
  { value: "7", label: "platforms" },
  { value: "82", label: "tests" },
];

export const CONTRACTS = [
  { icon: "OAS", name: "OpenAPI", sub: "REST APIs" },
  { icon: "PB", name: "Protobuf", sub: "gRPC" },
  { icon: "GQL", name: "GraphQL", sub: "Schemas" },
  { icon: "SQL", name: "Database", sub: "SQL + Prisma" },
  { icon: "ASY", name: "AsyncAPI", sub: "Kafka / SNS" },
  { icon: "AVR", name: "Avro", sub: "Confluent" },
  { icon: "tRPC", name: "tRPC", sub: "TypeScript" },
  { icon: "THR", name: "Thrift", sub: "Apache" },
  { icon: "JSN", name: "JSON Schema", sub: "Validation" },
  { icon: "SMI", name: "Smithy", sub: "AWS" },
];

export const INTELLIGENCE = [
  {
    title: "Co-change Learning",
    body: "Scans git history. Files that always change together? Ripple knows.",
  },
  {
    title: "Consumer Graph",
    body: "Persistent dependency map. Gets smarter every push.",
  },
  {
    title: "Multi-Invoker Detection",
    body: "Warns when shared configs have hidden consumers.",
  },
  {
    title: "Custom Playbooks",
    body: "Add .ripple.yaml to teach it YOUR patterns.",
  },
  {
    title: "Confidence Scoring",
    body: "Each PR shows WHY that file was chosen.",
  },
  {
    title: "Expand + Contract",
    body: "Warns when a breaking change could be done safely.",
  },
  {
    title: "Impact Report",
    body: "Every PR shows what was fixed, what was left alone, and why. Full transparency.",
  },
  {
    title: "CI/CD Gate",
    body: "Block merge if breaking changes have unfixed consumers. GitHub Action, one line to install.",
  },
  {
    title: "Monorepo",
    body: "Finds consumers within the same repo. 70% of companies use monorepos — now Ripple works inside them.",
  },
  {
    title: "AI Confidence",
    body: "Every PR shows why Ripple made this fix, with a confidence score based on repo-specific learning.",
  },
  {
    title: "12 Languages",
    body: "Template fixes for Python, TypeScript, Java, Go, Rust, Ruby, Kotlin, C#, Swift, PHP, Scala, Dart — plus LLM for anything else.",
  },
  {
    title: "Air-gapped Ready",
    body: "Self-hosted agent for on-prem Git, custom platforms and closed networks.",
  },
];

export const STEPS = [
  {
    n: "01",
    title: "Install",
    body: "One click. GitHub, GitLab, Bitbucket + self-hosted (Phabricator, Gerrit, CRUX, generic Git).",
  },
  { n: "02", title: "Push", body: "Change your spec. Push to main." },
  {
    n: "03",
    title: "Fixed",
    body: "PRs appear in every consumer with AI confidence badges. Review & merge.",
  },
];

export const WORKS_WITH = [
  "GitHub",
  "GitLab",
  "Bitbucket",
  "Phabricator",
  "Gerrit",
  "Kafka",
  "gRPC",
  "GraphQL",
  "PostgreSQL",
  "Prisma",
  "AWS",
  "TypeScript",
  "Python",
  "Java",
];

export const COMPARE_COLUMNS = ["Ripple", "Dependabot", "Optic", "buf"];

export const COMPARE_ROWS: { label: string; values: string[] }[] = [
  { label: "Detects breaking changes", values: ["✓", "—", "✓", "✓"] },
  { label: "Finds consumers", values: ["✓", "—", "—", "—"] },
  { label: "Generates fix code", values: ["✓", "—", "—", "—"] },
  { label: "Opens PRs automatically", values: ["✓", "✓", "—", "—"] },
  { label: "Contract types", values: ["10", "0", "1", "1"] },
  { label: "Learns from git history", values: ["✓", "—", "—", "—"] },
  { label: "Change Impact Report", values: ["✓", "—", "—", "—"] },
  { label: "Platforms", values: ["7", "1", "1", "1"] },
  { label: "Self-hosted option", values: ["✓", "—", "—", "—"] },
  { label: "CI/CD gate", values: ["✓", "—", "—", "✓"] },
  { label: "Monorepo support", values: ["✓", "—", "—", "—"] },
  { label: "Fix languages", values: ["12 + LLM", "10 (SDK gen)", "0", "0"] },
  { label: "Dependency graph", values: ["✓", "—", "—", "—"] },
];

export const RESEARCH_STATS = [
  { value: "874", label: "Real engineering changes analyzed" },
  { value: "5,200+", label: "Consequence files classified" },
  { value: "82%", label: "Package-level recall (ensemble)" },
  { value: "5", label: "Independent learning channels" },
];

export const PRICING = [
  {
    name: "Open Source",
    price: "Free",
    note: "Forever",
    features: ["All 10 contract types", "12 fix languages + LLM", "7 platforms", "Self-hosted agent", "Community support"],
    cta: { label: "Install on GitHub", href: LINKS.github },
    featured: true,
  },
  {
    name: "Team",
    price: "$49",
    note: "per month (coming soon)",
    features: ["Private repos", "Org-wide scanning", "Slack notifications", "Priority support"],
    cta: { label: "Join waitlist", href: "#" },
    featured: false,
  },
  {
    name: "Enterprise",
    price: "Custom",
    note: "Talk to us",
    features: ["SSO + audit log", "Custom adapters", "On-prem deployment", "Dedicated support"],
    cta: { label: "Contact us", href: "mailto:aakashsangwan024@gmail.com" },
    featured: false,
  },
];

export const SELF_HOSTED_TARGETS = ["Generic Git", "Amazon CRUX", "Phabricator", "Gerrit"];
