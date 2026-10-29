"""`ripple impact`: what does this local change break, outside the diff?

The command a coding agent runs BEFORE it commits. Two layers, both
deterministic, neither calls a model:

  contract   Changed contract files (OpenAPI, proto, GraphQL, Avro, Thrift,
             Smithy, AsyncAPI, JSON Schema, SQL) are diffed with the existing
             engines, base vs working tree. Each breaking change is searched
             for in the repository's own source with the existing consumer
             finder.
  code       app.outside_diff: unchanged lines that branch on what the change
             branches on, call what it edited, or describe what it altered.

The report is written to `.ripple/impact.md` and `.ripple/impact.json` in the
repository so another tool -- a gstack `/review`, a CI step, the agent itself --
can read it without re-running anything.

WHAT THE REPORT MUST NEVER DO
-----------------------------
Say "nothing breaks". It can say "no candidates found", and it states what was
searched (files scanned, files it could not read, keys that were too ambiguous
to list) so that an empty result is read as the size of the search, not as a
guarantee.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field

from . import languages
from . import local_diff
from . import outside_diff

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

# The markdown report is read by a person or an agent with a context budget;
# a wall of low-severity lines is how a report stops being read. Everything is
# still in impact.json -- the cap is on what is PRINTED, never on what is found.
MD_LIST_CAP = {"high": 1000, "medium": 40, "low": 15}

# Files that are contract specs but carry no language extension languages.py
# knows about. Mirrors the spec detection the webhook and /poll already use.
_OPENAPI_HINTS = ("openapi", "swagger", "api-spec", "api_spec")


@dataclass
class ContractChange:
    spec_file: str
    contract_type: str
    change_type: str
    field_name: str
    description: str
    severity: str
    consumers: list = field(default_factory=list)   # [{path,line,snippet,confidence}]


@dataclass
class ImpactReport:
    repo_root: str
    base_ref: str
    base_sha: str
    changed_files: list
    contract_changes: list = field(default_factory=list)
    contract_errors: list = field(default_factory=list)   # [(spec, error)]
    findings: list = field(default_factory=list)           # [outside_diff.Finding]
    ambiguous_keys: dict = field(default_factory=dict)
    files_scanned: int = 0
    files_unreadable: int = 0

    def count(self, severity: str) -> int:
        n = sum(1 for f in self.findings if f.severity == severity)
        if severity == "high":
            n += sum(len(c.consumers) for c in self.contract_changes)
        return n

    def worst(self) -> str:
        for sev in ("high", "medium", "low"):
            if self.count(sev):
                return sev
        return "none"


# ─── contract layer ─────────────────────────────────────────────────────────

def contract_type_of(path: str) -> str:
    """Contract type for a path, or "" when it is not a contract file."""
    lower = path.lower()
    if lower.endswith(".proto"):
        return "protobuf"
    if lower.endswith((".graphql", ".gql")):
        return "graphql"
    if lower.endswith(".avsc"):
        return "avro"
    if lower.endswith(".thrift"):
        return "thrift"
    if lower.endswith(".smithy"):
        return "smithy"
    if "asyncapi" in lower and lower.endswith((".yaml", ".yml", ".json")):
        return "asyncapi"
    if lower.endswith(".schema.json"):
        return "jsonschema"
    if lower.endswith(".prisma") or (lower.endswith(".sql") and
                                     "schema" in lower):
        return "sql"
    if lower.endswith((".yaml", ".yml", ".json")) and any(
            h in lower for h in _OPENAPI_HINTS):
        return "openapi"
    return ""


def diff_contract(contract_type: str, old: str, new: str, path: str) -> list:
    """BreakingChange list from the right engine. Raises on engine failure."""
    if contract_type == "protobuf":
        from .proto_diff import diff_proto
        return diff_proto(old, new, path)
    if contract_type == "graphql":
        from .graphql_diff import diff_graphql
        return diff_graphql(old, new, path)
    if contract_type == "avro":
        from .avro_diff import diff_avro
        return diff_avro(old, new, path)
    if contract_type == "thrift":
        from .thrift_diff import diff_thrift
        return diff_thrift(old, new, path)
    if contract_type == "smithy":
        from .smithy_diff import diff_smithy
        return diff_smithy(old, new, path)
    if contract_type == "asyncapi":
        from .asyncapi_diff import diff_asyncapi
        return diff_asyncapi(old, new, path)
    if contract_type == "jsonschema":
        from .jsonschema_diff import diff_jsonschema
        return diff_jsonschema(old, new, path)
    if contract_type == "sql":
        from .migration_diff import diff_schema
        return diff_schema(old, new, path)
    if contract_type == "openapi":
        from .diff_engine import diff_specs
        suffix = ".json" if path.lower().endswith(".json") else ".yaml"
        paths = []
        try:
            for text in (old or "{}", new or "{}"):
                fd, p = tempfile.mkstemp(suffix=suffix)
                with os.fdopen(fd, "w") as fh:
                    fh.write(text)
                paths.append(p)
            return list(diff_specs(paths[0], paths[1]).breaking_changes)
        finally:
            for p in paths:
                os.unlink(p)
    raise ValueError(f"no diff engine for contract type {contract_type!r}")


def _contract_layer(cs, sources: dict, report: ImpactReport) -> None:
    from .smart_consumer_finder import find_field_consumers
    for fc in cs.files:
        ctype = contract_type_of(fc.path)
        if not ctype:
            continue
        old = local_diff.read_at_base(cs.repo_root, cs.base_sha, fc.path) or ""
        if fc.status == "deleted":
            new = ""
        else:
            new = local_diff.read_worktree(cs.repo_root, fc.path)
            if new is None:
                # Diffing "" would report every field as removed: a wall of
                # false breaking changes, from a file we merely failed to read.
                report.contract_errors.append(
                    (fc.path, "could not read the working-tree file (too "
                              "large, binary, or unreadable)"))
                continue
        try:
            changes = diff_contract(ctype, old, new, fc.path)
        except Exception as e:   # one bad spec must not hide the others
            report.contract_errors.append(
                (fc.path, f"{type(e).__name__}: {str(e)[:200]}"))
            continue
        for bc in changes:
            if bc.severity != "breaking":
                continue
            cc = ContractChange(
                spec_file=fc.path, contract_type=ctype,
                change_type=bc.change_type, field_name=bc.field_name,
                description=bc.description, severity=bc.severity)
            for path, text in sources.items():
                if path == fc.path:
                    continue
                lang = languages.detect(path)
                for m in find_field_consumers(text, path, bc.field_name, lang):
                    cc.consumers.append({
                        "path": path, "line": m.line_number,
                        "snippet": m.line_content[:160],
                        "confidence": round(m.confidence, 2),
                        "match_type": m.match_type,
                    })
            report.contract_changes.append(cc)


# ─── orchestration ──────────────────────────────────────────────────────────

def analyse(repo: str = ".", base: str = "") -> ImpactReport:
    cs = local_diff.collect_changes(repo, base)
    report = ImpactReport(
        repo_root=cs.repo_root, base_ref=cs.base_ref, base_sha=cs.base_sha,
        changed_files=[{"path": f.path, "status": f.status,
                        "added": len(f.added_lines),
                        "removed": len(f.removed_lines)} for f in cs.files])
    if not cs.files:
        return report

    sources = {}
    for path in local_diff.tracked_and_untracked_files(cs.repo_root):
        if not languages.is_scannable(path):
            continue
        text = local_diff.read_worktree(cs.repo_root, path)
        if text is None:
            if os.path.exists(os.path.join(cs.repo_root, path)):
                report.files_unreadable += 1
            continue
        sources[path] = text

    _contract_layer(cs, sources, report)

    files = dict(sources)
    for fc in cs.files:
        if fc.removed_lines and languages.is_scannable(fc.path):
            old = local_diff.read_at_base(cs.repo_root, cs.base_sha, fc.path)
            if old is not None:
                files[("__old__", fc.path)] = old
    od = outside_diff.find_outside_diff(cs.files, files)
    report.findings = od.findings
    report.ambiguous_keys = od.ambiguous_keys
    report.files_scanned = od.files_scanned
    return report


# ─── rendering ──────────────────────────────────────────────────────────────

def to_json(report: ImpactReport) -> str:
    data = asdict(report)
    data["summary"] = {s: report.count(s) for s in ("high", "medium", "low")}
    data["worst"] = report.worst()
    return json.dumps(data, indent=2, default=str)


def to_markdown(report: ImpactReport) -> str:
    out = ["# Ripple impact report", ""]
    out.append(f"Compared the working tree against `{report.base_ref}` "
               f"(`{report.base_sha[:10]}`). "
               f"{len(report.changed_files)} changed file(s); "
               f"{report.files_scanned} source file(s) searched"
               + (f", {report.files_unreadable} unreadable" if
                  report.files_unreadable else "") + ".")
    out.append("")
    h, m, l = (report.count(s) for s in ("high", "medium", "low"))
    if not (h or m or l):
        out.append("**No candidates found.** That is the result of the search "
                   "described above, not a guarantee that nothing breaks.")
    else:
        out.append(f"**{h} high, {m} medium, {l} low.** Every item is a "
                   f"candidate to check, not a confirmed break. Each names the "
                   f"rule that produced it and the change line that triggered it.")
    out.append("")

    if report.contract_changes or report.contract_errors:
        out.append("## Contract changes")
        out.append("")
        for cc in report.contract_changes:
            out.append(f"- **{cc.change_type}** `{cc.field_name}` in "
                       f"`{cc.spec_file}` ({cc.contract_type}): {cc.description}")
            if not cc.consumers:
                out.append("  - no references found in this repository "
                           "(consumers in OTHER repositories are not searched "
                           "by this command)")
            for c in cc.consumers[:20]:
                out.append(f"  - `{c['path']}:{c['line']}` "
                           f"({c['match_type']}, {c['confidence']}) "
                           f"`{c['snippet']}`")
            if len(cc.consumers) > 20:
                out.append(f"  - ... {len(cc.consumers) - 20} more in "
                           f"impact.json")
        for spec, err in report.contract_errors:
            out.append(f"- could not diff `{spec}`: {err}")
        out.append("")

    if report.findings:
        out.append("## Code outside the diff")
        out.append("")
        for sev in ("high", "medium", "low"):
            items = [f for f in report.findings if f.severity == sev]
            if not items:
                continue
            out.append(f"### {sev.upper()}")
            out.append("")
            cap = MD_LIST_CAP[sev]
            for f in items[:cap]:
                out.append(f"- `{f.path}:{f.line}` -- {f.reason}. "
                           f"Triggered by `{f.because_of}`. [{f.kind}]")
                out.append(f"  `{f.snippet}`")
            if len(items) > cap:
                out.append(f"- ... {len(items) - cap} more {sev} in "
                           f"impact.json")
            out.append("")

    if report.ambiguous_keys:
        out.append("## Too ambiguous to list")
        out.append("")
        for key, n in sorted(report.ambiguous_keys.items()):
            out.append(f"- `{key}` matched {n} sites; only the strongest "
                       f"(if any) are listed above.")
        out.append("")

    out.append("## Not covered")
    out.append("")
    out.append("- Consumers in other repositories (use the Ripple GitHub App "
               "or `ripple scan --repos`).")
    out.append("- Consumers that reach the changed value under a different "
               "name, through reflection, or via generated code.")
    out.append("- Whether any candidate is actually broken: that needs the "
               "tests, a type-check, or a human.")
    out.append("")
    return "\n".join(out)


def write_reports(report: ImpactReport, out_dir: str = "") -> tuple:
    out_dir = out_dir or os.path.join(report.repo_root, ".ripple")
    os.makedirs(out_dir, exist_ok=True)
    # The report directory ignores itself, so the next `ripple impact` does not
    # see its own previous output as an untracked change, and an agent running
    # `git add -A` does not commit it.
    ignore = os.path.join(out_dir, ".gitignore")
    if not os.path.exists(ignore):
        with open(ignore, "w") as fh:
            fh.write("*\n")
    md = os.path.join(out_dir, "impact.md")
    js = os.path.join(out_dir, "impact.json")
    with open(md, "w") as fh:
        fh.write(to_markdown(report))
    with open(js, "w") as fh:
        fh.write(to_json(report))
    return md, js
