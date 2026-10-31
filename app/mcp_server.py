"""Ripple as an MCP server: `ripple mcp` (stdio, JSON-RPC 2.0).

The same deterministic checks as `ripple impact`, exposed as tools an agent can
call mid-task instead of shelling out and parsing markdown:

  ripple_impact          what does the uncommitted change in a repo break
                         OUTSIDE the diff (the `ripple impact` report)
  ripple_diff_contract   breaking changes between two versions of one API
                         contract (OpenAPI, proto, GraphQL, Avro, Thrift,
                         Smithy, AsyncAPI, JSON Schema, SQL) -- no repo needed
  ripple_find_references every reference to a field/symbol in a repo, across
                         naming conventions (phone_number / phoneNumber /
                         PhoneNumber), with comments and strings filtered out

SCOPE OF WHAT THIS SERVER CAN DO
--------------------------------
Read only. Every tool reads files and git metadata; none writes to the
repository unless `ripple_impact` is called with `write_report: true`, and then
only `.ripple/impact.{md,json}` (a directory that git-ignores itself). It
speaks over stdin/stdout only: no socket is opened, so nothing is reachable
from the network. A `repo_path` must be a git working tree, which is what
bounds the files the server will read.

No dependency on the `mcp` package: the protocol surface used here --
initialize, tools/list, tools/call, ping -- is small, and the rest of
`ripple impact` runs on the standard library, so the server does too.

STDOUT IS THE PROTOCOL
----------------------
Anything else printed to stdout corrupts the stream. Tool execution runs with
sys.stdout pointed at stderr, so a stray print in an engine becomes a log line
rather than a broken session.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys

from . import local_impact

SERVER_NAME = "ripple"
SERVER_VERSION = "0.1.0"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

# A finding list is truncated in the TEXT the model reads -- the full list is
# always in structuredContent. Same reasoning as the markdown report's caps.
DEFAULT_MAX_FINDINGS = 40

TOOLS = [
    {
        "name": "ripple_impact",
        "title": "What does this change break outside the diff?",
        "description": (
            "Run BEFORE committing or opening a PR. Compares the working tree "
            "(uncommitted + untracked) of a git repository against the merge "
            "base of a base ref, and returns UNCHANGED code that may now be "
            "wrong: lines that branch on the same value the change branches "
            "on, callers of functions whose body changed, user-facing text "
            "near changed logic, and local consumers of changed API "
            "contracts. Deterministic, no model call. Every finding is a "
            "candidate to check, with the rule that produced it and the "
            "change line that triggered it. Resolve each HIGH and MEDIUM: fix "
            "it, or state why it is unaffected. An empty result means no "
            "candidates in the files searched, NOT that nothing breaks."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string",
                              "description": "Absolute path inside a git working tree."},
                "base": {"type": "string",
                         "description": "Ref to compare against (merge base with HEAD). "
                                        "Default: origin/main, origin/master, main, master."},
                "max_findings": {"type": "integer", "minimum": 1, "maximum": 500,
                                 "description": f"Findings listed in the text summary "
                                                f"(default {DEFAULT_MAX_FINDINGS}); all are "
                                                f"in the structured result."},
                "write_report": {"type": "boolean",
                                 "description": "Also write .ripple/impact.md and .json "
                                                "in the repository (default false)."},
            },
            "required": ["repo_path"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "idempotentHint": True,
                        "openWorldHint": False},
    },
    {
        "name": "ripple_diff_contract",
        "title": "Breaking changes between two versions of an API contract",
        "description": (
            "Diff two versions of one API contract file and list the BREAKING "
            "changes (removed/renamed fields, type changes, newly required "
            "fields, removed operations...). Supports OpenAPI (yaml/json), "
            "Protocol Buffers, GraphQL, Avro, Thrift, Smithy, AsyncAPI, JSON "
            "Schema and SQL schemas. The type is inferred from `path` unless "
            "`contract_type` is given. Deterministic, no repository needed."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "The contract's file name or path, e.g. "
                                        "api/user.proto -- used to infer the type."},
                "old_content": {"type": "string"},
                "new_content": {"type": "string"},
                "contract_type": {"type": "string",
                                  "enum": ["openapi", "protobuf", "graphql", "avro",
                                           "thrift", "smithy", "asyncapi",
                                           "jsonschema", "sql"]},
            },
            "required": ["path", "old_content", "new_content"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "idempotentHint": True,
                        "openWorldHint": False},
    },
    {
        "name": "ripple_find_references",
        "title": "Every reference to a field or symbol, across naming conventions",
        "description": (
            "Search a git repository's source files for references to a field "
            "or symbol name in every naming convention (snake_case, camelCase, "
            "PascalCase, kebab-case, getter forms). Comments and string "
            "literals are filtered out and each hit carries a confidence and "
            "match type. Use it to see who depends on a field before changing "
            "or removing it."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string",
                              "description": "Absolute path inside a git working tree."},
                "name": {"type": "string", "minLength": 2,
                         "description": "Field or symbol name, any convention."},
                "min_confidence": {"type": "number", "minimum": 0, "maximum": 1,
                                   "description": "Default 0.5."},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 1000,
                                "description": "Default 200."},
            },
            "required": ["repo_path", "name"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "idempotentHint": True,
                        "openWorldHint": False},
    },
]


class ToolError(Exception):
    """A failure the CALLER can act on -- returned as isError, not raised."""


# ─── tools ──────────────────────────────────────────────────────────────────

def _repo_path(args: dict) -> str:
    path = args.get("repo_path")
    if not isinstance(path, str) or not path.strip():
        raise ToolError("repo_path is required")
    path = os.path.expanduser(path)
    if not os.path.isdir(path):
        raise ToolError(f"repo_path {path!r} is not a directory")
    return path


def tool_impact(args: dict) -> tuple:
    repo = _repo_path(args)
    limit = int(args.get("max_findings") or DEFAULT_MAX_FINDINGS)
    try:
        report = local_impact.analyse(repo, args.get("base") or "")
    except ValueError as e:
        raise ToolError(str(e)) from None
    if args.get("write_report"):
        local_impact.write_reports(report)

    data = json.loads(local_impact.to_json(report))
    h, m, l = (report.count(s) for s in ("high", "medium", "low"))
    lines = [f"Compared the working tree of {report.repo_root} against "
             f"{report.base_ref} ({report.base_sha[:10]}): "
             f"{len(report.changed_files)} changed file(s), "
             f"{report.files_scanned} source file(s) searched"
             + (f", {report.files_unreadable} unreadable" if report.files_unreadable
                else "") + "."]
    if not (h or m or l):
        lines.append("No candidates found. That is the result of this search, "
                     "not a guarantee that nothing breaks.")
    else:
        lines.append(f"{h} high, {m} medium, {l} low candidate(s). Resolve every "
                     f"HIGH and MEDIUM: change the code, or say why it is "
                     f"unaffected.")
    for cc in report.contract_changes:
        lines.append(f"CONTRACT {cc.change_type} `{cc.field_name}` in "
                     f"{cc.spec_file}: {len(cc.consumers)} local reference(s)"
                     + "".join(f"\n  - {c['path']}:{c['line']} {c['snippet']}"
                               for c in cc.consumers[:10]))
    for spec, err in report.contract_errors:
        lines.append(f"COULD NOT DIFF {spec}: {err}")
    for f in report.findings[:limit]:
        more = f" (+{len(f.also)} more: {', '.join(f.also[:5])})" if f.also else ""
        lines.append(f"{f.severity.upper()} {f.path}:{f.line} [{f.kind}] "
                     f"{f.reason}{more}. Triggered by {f.because_of}.\n"
                     f"  {f.snippet}")
    if len(report.findings) > limit:
        lines.append(f"... {len(report.findings) - limit} more in the "
                     f"structured result.")
    if report.ambiguous_keys:
        lines.append("Too ambiguous to list: " + ", ".join(
            f"{k} ({n} sites)" for k, n in sorted(report.ambiguous_keys.items())))
    lines.append("Not covered: other repositories; values reached under a "
                 "different name or via generated code; whether a candidate "
                 "is actually broken (needs tests or a human).")
    return "\n".join(lines), data


def tool_diff_contract(args: dict) -> tuple:
    path = args.get("path") or ""
    old, new = args.get("old_content"), args.get("new_content")
    if not isinstance(old, str) or not isinstance(new, str):
        raise ToolError("old_content and new_content must both be strings")
    ctype = args.get("contract_type") or local_impact.contract_type_of(path)
    if not ctype:
        raise ToolError(
            f"cannot infer the contract type from {path!r}; pass contract_type "
            f"(openapi, protobuf, graphql, avro, thrift, smithy, asyncapi, "
            f"jsonschema, sql)")
    try:
        changes = local_impact.diff_contract(ctype, old, new, path)
    except ValueError as e:
        raise ToolError(str(e)) from None
    except Exception as e:
        raise ToolError(f"the {ctype} engine could not read the input: "
                        f"{type(e).__name__}: {str(e)[:300]}") from None
    items = [{"change_type": c.change_type, "field_name": c.field_name,
              "field_type": c.field_type, "path": c.path, "method": c.method,
              "location": c.location, "severity": c.severity,
              "description": c.description, "new_name": c.new_name,
              "old_type": c.old_type, "new_type": c.new_type} for c in changes]
    breaking = [i for i in items if i["severity"] == "breaking"]
    if not breaking:
        text = (f"No breaking changes detected in {path} ({ctype})."
                + (f" {len(items)} non-breaking change(s) in the structured "
                   f"result." if items else ""))
    else:
        text = f"{len(breaking)} breaking change(s) in {path} ({ctype}):\n" + \
            "\n".join(f"- {b['change_type']} `{b['field_name']}`: {b['description']}"
                      for b in breaking)
    return text, {"contract_type": ctype, "changes": items,
                  "breaking_count": len(breaking)}


def tool_find_references(args: dict) -> tuple:
    from . import languages, local_diff
    from .smart_consumer_finder import find_field_consumers
    repo = _repo_path(args)
    name = (args.get("name") or "").strip()
    if len(name) < 2:
        raise ToolError("name must be at least 2 characters")
    min_conf = float(args.get("min_confidence", 0.5))
    cap = int(args.get("max_results") or 200)
    try:
        root = local_diff.repo_root(repo)
        paths = local_diff.tracked_and_untracked_files(root)
    except ValueError as e:
        raise ToolError(str(e)) from None
    hits, scanned, unreadable = [], 0, 0
    for path in paths:
        if not languages.is_scannable(path):
            continue
        text = local_diff.read_worktree(root, path)
        if text is None:
            unreadable += 1
            continue
        scanned += 1
        for m in find_field_consumers(text, path, name, languages.detect(path),
                                      min_confidence=min_conf):
            hits.append({"path": path, "line": m.line_number,
                         "snippet": m.line_content[:200],
                         "match_type": m.match_type,
                         "confidence": round(m.confidence, 2),
                         "variant": m.variant_matched})
    hits.sort(key=lambda h: (-h["confidence"], h["path"], h["line"]))
    shown = hits[:cap]
    text = (f"{len(hits)} reference(s) to `{name}` in {scanned} source file(s)"
            + (f" ({unreadable} unreadable)" if unreadable else "") + ".")
    text += "".join(f"\n- {h['path']}:{h['line']} ({h['match_type']}, "
                    f"{h['confidence']}) {h['snippet']}" for h in shown[:60])
    if len(hits) > 60:
        text += f"\n... {len(hits) - 60} more in the structured result."
    return text, {"name": name, "files_scanned": scanned,
                  "files_unreadable": unreadable, "total": len(hits),
                  "references": shown, "truncated": len(hits) > cap}


HANDLERS = {
    "ripple_impact": tool_impact,
    "ripple_diff_contract": tool_diff_contract,
    "ripple_find_references": tool_find_references,
}


# ─── protocol ───────────────────────────────────────────────────────────────

def _result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message}}


def handle(msg: dict):
    """One JSON-RPC message in, a response dict (or None for a notification)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _error(msg.get("id") if isinstance(msg, dict) else None,
                      -32600, "invalid request")
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}
    is_notification = "id" not in msg

    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        return _result(req_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "Call ripple_impact on the repository before committing or "
                "opening a pull request, and resolve every HIGH and MEDIUM "
                "candidate it returns."),
        })
    if method == "ping":
        return _result(req_id, {})
    if method == "tools/list":
        return _result(req_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        handler = HANDLERS.get(name)
        if handler is None:
            return _error(req_id, -32602, f"unknown tool {name!r}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _error(req_id, -32602, "arguments must be an object")
        try:
            with contextlib.redirect_stdout(sys.stderr):
                text, data = handler(args)
            return _result(req_id, {
                "content": [{"type": "text", "text": text}],
                "structuredContent": data,
                "isError": False,
            })
        except ToolError as e:
            return _result(req_id, {"content": [{"type": "text", "text": str(e)}],
                                    "isError": True})
        except Exception as e:   # a bug in a tool must not kill the session
            print(f"ripple mcp: {name} failed: {type(e).__name__}: {e}",
                  file=sys.stderr)
            return _result(req_id, {
                "content": [{"type": "text",
                             "text": f"internal error in {name}: "
                                     f"{type(e).__name__}: {str(e)[:300]}"}],
                "isError": True})
    if is_notification:
        return None          # notifications/initialized, cancelled, ...
    return _error(req_id, -32601, f"method not found: {method}")


def serve(stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply = _error(None, -32700, "parse error")
        else:
            if isinstance(msg, list):     # batch
                replies = [r for r in map(handle, msg) if r is not None]
                reply = replies or None
            else:
                reply = handle(msg)
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()
    return 0


def main() -> int:
    return serve()
