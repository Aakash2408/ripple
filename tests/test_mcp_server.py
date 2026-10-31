"""Tests for `ripple mcp` (app.mcp_server).

Run:  python3.12 tests/test_mcp_server.py

The end-to-end tests start the REAL server as a subprocess through bin/ripple
and speak JSON-RPC over its stdin/stdout, because that byte stream is the whole
contract: one stray print on stdout breaks every client, and an in-process call
of handle() cannot see that.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import mcp_server  # noqa: E402
from test_local_impact import _demo_repo  # noqa: E402

BIN = os.path.join(ROOT, "bin", "ripple")


def _session(messages: list) -> list:
    """Run one server process, send `messages`, return every stdout line parsed.

    Fails if ANY stdout line is not JSON: that is the protocol-corruption case.
    """
    stdin = "".join(json.dumps(m) + "\n" for m in messages)
    proc = subprocess.run([BIN, "mcp"], input=stdin, capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = []
    for line in proc.stdout.splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            raise AssertionError(f"non-JSON on stdout corrupts the protocol: {line!r}")
    return out


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "0"}}}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def _call(i, name, args):
    return {"jsonrpc": "2.0", "id": i, "method": "tools/call",
            "params": {"name": name, "arguments": args}}


def test_handshake_and_tool_list():
    replies = _session([INIT, INITIALIZED,
                        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
    assert len(replies) == 2, "a notification must not get a reply"
    assert replies[0]["result"]["protocolVersion"] == "2025-06-18"
    names = {t["name"] for t in replies[1]["result"]["tools"]}
    assert names == {"ripple_impact", "ripple_diff_contract",
                     "ripple_find_references"}, names
    for t in replies[1]["result"]["tools"]:
        assert t["annotations"]["readOnlyHint"] is True


def test_unknown_protocol_version_gets_a_supported_one():
    r = mcp_server.handle(dict(INIT, params={"protocolVersion": "1999-01-01"}))
    assert r["result"]["protocolVersion"] in mcp_server.SUPPORTED_PROTOCOLS


def test_impact_over_the_wire_finds_the_gstack_2141_miss():
    d = _demo_repo()
    try:
        replies = _session([INIT, INITIALIZED,
                            _call(2, "ripple_impact",
                                  {"repo_path": d, "base": "main"})])
        res = replies[1]["result"]
        assert res["isError"] is False, res
        text = res["content"][0]["text"]
        assert "HIGH src/upload/submit.ts:5" in text, text
        top = res["structuredContent"]["findings"][0]
        assert (top["path"], top["line"], top["severity"]) == \
            ("src/upload/submit.ts", 5, "high"), top
        assert not os.path.exists(os.path.join(d, ".ripple")), (
            "a read-only tool wrote into the repository")
    finally:
        shutil.rmtree(d)


def test_impact_write_report_is_opt_in():
    d = _demo_repo()
    try:
        _session([INIT, _call(2, "ripple_impact", {"repo_path": d, "base": "main",
                                                   "write_report": True})])
        assert os.path.exists(os.path.join(d, ".ripple", "impact.md"))
    finally:
        shutil.rmtree(d)


def test_impact_on_a_non_repo_is_a_tool_error_not_a_clean_result():
    nd = tempfile.mkdtemp()
    try:
        res = _session([INIT, _call(2, "ripple_impact", {"repo_path": nd})])[1]["result"]
        assert res["isError"] is True, res
        assert "No candidates" not in res["content"][0]["text"]
    finally:
        shutil.rmtree(nd)


def test_diff_contract_proto():
    old = 'syntax = "proto3";\nmessage User {\n  string id = 1;\n  string email = 2;\n}\n'
    new = 'syntax = "proto3";\nmessage User {\n  string id = 1;\n}\n'
    res = mcp_server.handle(_call(3, "ripple_diff_contract",
                                  {"path": "api/user.proto", "old_content": old,
                                   "new_content": new}))["result"]
    assert res["isError"] is False, res
    assert res["structuredContent"]["contract_type"] == "protobuf"
    assert res["structuredContent"]["breaking_count"] >= 1
    assert "email" in res["content"][0]["text"]


def test_diff_contract_uninferable_type_says_what_to_pass():
    res = mcp_server.handle(_call(3, "ripple_diff_contract",
                                  {"path": "notes.txt", "old_content": "a",
                                   "new_content": "b"}))["result"]
    assert res["isError"] is True and "contract_type" in res["content"][0]["text"]


def test_find_references_across_conventions():
    d = tempfile.mkdtemp()
    try:
        for rel, text in {"a.py": "user.phone_number = 1\n",
                          "b.ts": "const p = user.phoneNumber;\n",
                          "c.py": "# phone_number in a comment only\n"}.items():
            with open(os.path.join(d, rel), "w") as fh:
                fh.write(text)
        subprocess.run(["git", "-C", d, "init", "-q"], check=True)
        res = mcp_server.handle(_call(4, "ripple_find_references",
                                      {"repo_path": d, "name": "phone_number"}))["result"]
        assert res["isError"] is False, res
        paths = {r["path"] for r in res["structuredContent"]["references"]}
        assert paths == {"a.py", "b.ts"}, paths
    finally:
        shutil.rmtree(d)


def test_unknown_tool_and_unknown_method():
    assert mcp_server.handle(_call(5, "nope", {}))["error"]["code"] == -32602
    assert mcp_server.handle({"jsonrpc": "2.0", "id": 6,
                              "method": "resources/list"})["error"]["code"] == -32601


def test_a_tool_that_prints_does_not_corrupt_stdout():
    """Engines are allowed to print; the protocol stream must stay JSON."""
    import io
    saved = mcp_server.HANDLERS["ripple_diff_contract"]

    def noisy(args):
        print("engine chatter on stdout")
        return "ok", {}
    mcp_server.HANDLERS["ripple_diff_contract"] = noisy
    out = io.StringIO()
    real = sys.stdout
    try:
        sys.stdout = out
        mcp_server.serve(stdin=io.StringIO(json.dumps(
            _call(7, "ripple_diff_contract", {})) + "\n"), stdout=out)
    finally:
        sys.stdout = real
        mcp_server.HANDLERS["ripple_diff_contract"] = saved
    lines = out.getvalue().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["id"] == 7, lines


def test_garbage_line_is_a_parse_error_and_the_session_survives():
    import io
    out = io.StringIO()
    mcp_server.serve(stdin=io.StringIO("not json\n" + json.dumps(
        {"jsonrpc": "2.0", "id": 8, "method": "ping"}) + "\n"), stdout=out)
    a, b = map(json.loads, out.getvalue().splitlines())
    assert a["error"]["code"] == -32700 and b["id"] == 8 and b["result"] == {}


if __name__ == "__main__":
    from _runner import run
    sys.exit(run(globals()))
