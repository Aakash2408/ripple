# How It Works

Ripple's pipeline has 4 stages: **Detect → Find → Fix → Ship**.

## 1. Detect

Ripple monitors pushes and identifies breaking changes across **10 contract types**:

| Contract Type | Extensions |
|---|---|
| OpenAPI / Swagger | `.yaml`, `.json` |
| Protocol Buffers | `.proto` |
| GraphQL | `.graphql`, `.gql` |
| Database Migrations | `.sql`, migration files |
| AsyncAPI | `.yaml`, `.json` |
| Apache Avro | `.avsc` |
| tRPC | `.ts` router files |
| Apache Thrift | `.thrift` |
| JSON Schema | `.json` |
| Smithy | `.smithy` |

Detection is AST-aware — it understands semantic changes (renamed field, removed endpoint, type change), not just text diffs.

## 2. Find

The **5-channel ensemble consumer finder** locates every downstream repository that depends on the changed contract:

1. **Import graph** — static analysis of import/require/use statements
2. **Registry lookup** — package manager dependency trees
3. **Code search** — grep for endpoint URLs, type names, method calls
4. **History learner** — repos that previously consumed this contract
5. **LLM inference** — catches indirect consumers missed by static analysis

Results are ranked by confidence and deduplicated.

## 3. Fix

Ripple generates idiomatic fixes in **12 languages + LLM fallback**:

Python, TypeScript, Java, Go, Rust, Ruby, Kotlin, C#, Swift, PHP, Scala, Dart

For any other language, the LLM generates a fix using the contract diff and consumer code context.

Fixes follow the **expand-and-contract** pattern when possible — backward-compatible changes that let consumers migrate gradually.

## 4. Ship

Ripple opens a pull request in each consuming repository with:

- **AI Confidence Score** — how certain the fix is correct (based on PropBench training data)
- **Change Impact Report** — what changed, why, and what breaks without the fix
- **Lifecycle Labels** — `ripple/pending`, `ripple/merged`, `ripple/reverted` for tracking
- **Upstream Status** — link back to the source change and its merge state

PRs are opened immediately on push (same as Dependabot). They don't wait for the source CR to merge.
