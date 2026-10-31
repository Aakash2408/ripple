"""Tests for `ripple impact`: app.local_diff, app.outside_diff, app.local_impact.

Run:  python3.12 tests/test_local_impact.py

Every test builds a real throwaway git repository. Mocking git would test the
mock; the parsing this command depends on is git's actual output.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import local_diff, local_impact, outside_diff  # noqa: E402

DEMO = os.path.join(ROOT, "demo", "gstack-2141")
GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *GIT_ID, *args], check=True,
                   capture_output=True)


def _repo(files: dict) -> str:
    d = tempfile.mkdtemp(prefix="ripple-impact-test-")
    _write(d, files)
    _git(d, "init", "-q", "-b", "main")
    _git(d, "add", "-A")
    _git(d, "commit", "-q", "-m", "base")
    return d


def _write(root, files: dict):
    for rel, text in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            fh.write(text)


def _demo_repo() -> str:
    d = tempfile.mkdtemp(prefix="ripple-2141-test-")
    shutil.copytree(os.path.join(DEMO, "base"), d, dirs_exist_ok=True)
    _git(d, "init", "-q", "-b", "main")
    _git(d, "add", "-A")
    _git(d, "commit", "-q", "-m", "base")
    shutil.copytree(os.path.join(DEMO, "after"), d, dirs_exist_ok=True)
    return d


def _at(report, path, line, kind=None):
    return [f for f in report.findings if f.path == path and f.line == line
            and (kind is None or f.kind == kind)]


# ─── the demo case ──────────────────────────────────────────────────────────

def test_gstack_2141_miss_one_is_found_high():
    """The submit helper that forces typeless files to text/plain is unchanged
    and outside the diff. It must be the top finding, at HIGH."""
    d = _demo_repo()
    try:
        r = local_impact.analyse(d, "main")
        hits = _at(r, "src/upload/submit.ts", 5, "same_predicate")
        assert hits, ("the unchanged submit helper was not flagged; findings: "
                      + repr([(f.path, f.line, f.kind) for f in r.findings]))
        assert hits[0].severity == "high", hits[0]
        assert r.findings[0].path == "src/upload/submit.ts", (
            "the real miss is not ranked first")
        assert hits[0].because_of == "src/upload/validate.ts:9", (
            "cites the wrong change line: " + hits[0].because_of)
    finally:
        shutil.rmtree(d)


def test_gstack_2141_miss_two_stale_message_is_found():
    """The 'Total file size' message went stale when totalSize() stopped
    counting PDFs. It is unchanged; it must still be flagged."""
    d = _demo_repo()
    try:
        r = local_impact.analyse(d, "main")
        assert _at(r, "src/upload/limits.ts", 12, "stale_message"), (
            "the stale size message was not flagged")
    finally:
        shutil.rmtree(d)


def test_gstack_2141_control_name_only_match_stays_low():
    """`user.type === 'admin'` shares only a property NAME with the change:
    different receiver, different literal. It may be listed; it must not be
    ranked as if it were the real miss."""
    d = _demo_repo()
    try:
        r = local_impact.analyse(d, "main")
        hits = _at(r, "src/user/profile.ts", 4)
        assert all(f.severity == "low" for f in hits), hits
    finally:
        shutil.rmtree(d)


def test_gstack_2141_callers_of_edited_functions():
    d = _demo_repo()
    try:
        r = local_impact.analyse(d, "main")
        assert _at(r, "src/upload/UploadForm.tsx", 6, "caller")
        assert _at(r, "src/upload/limits.ts", 11, "caller")
    finally:
        shutil.rmtree(d)


# ─── precision rules, each pinned by the defect that motivated it ───────────

def test_lines_the_change_added_are_never_findings():
    d = _demo_repo()
    try:
        r = local_impact.analyse(d, "main")
        cs = local_diff.collect_changes(d, "main")
        added = {(f.path, n) for f in cs.files for n in f.added_lines}
        bad = [(f.path, f.line) for f in r.findings if (f.path, f.line) in added]
        assert not bad, f"findings on lines the change itself added: {bad}"
    finally:
        shutil.rmtree(d)


def test_brand_new_file_contributes_no_keys():
    """Nothing outside the diff can depend on a file that did not exist."""
    d = _repo({"a.py": "def keep(r):\n    if r.returncode == 0:\n        return 1\n"})
    try:
        _write(d, {"new.py": "def f(proc):\n    if proc.returncode != 0:\n"
                             "        raise SystemExit(1)\n"})
        r = local_impact.analyse(d, "main")
        assert not r.findings, r.findings
    finally:
        shutil.rmtree(d)


def test_docstring_contents_are_not_code():
    lines = outside_diff.analyse_lines(
        'x = 1\n"""\nif user.name == "x": file(\n"""\ny = 2\n', "python")
    assert lines[2].props == () and not lines[2].is_condition, lines[2]
    assert lines[4].code.strip() == "y = 2"


def test_template_literal_spanning_lines_is_not_code():
    lines = outside_diff.analyse_lines(
        "const a = `\n${user.type === 'x'}\n`;\nconst b = c.d;\n", "typescript")
    assert lines[1].props == (), lines[1]
    assert ("c", "d") in lines[3].props


def test_method_call_is_not_a_property_read():
    """`x.is_scannable(` once matched as property `.is_scannabl` because the
    regex backtracked one character to dodge its own `(` lookahead."""
    [ln] = outside_diff.analyse_lines("if languages.is_scannable(p):", "python")
    assert ln.props == (), ln.props


def test_same_named_function_in_another_module_is_not_a_caller():
    """fork_pr._git is not local_diff._git.

    b.py imports the defining module (so the import rule alone would admit it)
    but defines its own `_git`, and d.py imports an unrelated module whose name
    merely CONTAINS the defining module's name.
    """
    d = _repo({
        "a.py": "def _git(x):\n    return x\n\n\ndef other():\n    pass\n",
        "b.py": "from a import other\n\n\ndef _git(y):\n    return y\n\n\n"
                "print(_git(1))\n",
        "c.py": "from a import _git\n\n\nprint(_git(2))\n",
        "d.py": "import data_tools\n\n\nprint(_git(3))\n",
    })
    try:
        _write(d, {"a.py": "def _git(x):\n    return x + 1\n\n\ndef other():\n"
                           "    pass\n"})
        r = local_impact.analyse(d, "main")
        callers = {(f.path, f.line) for f in r.findings if f.kind == "caller"}
        assert ("c.py", 4) in callers, callers
        assert not any(p in ("b.py", "d.py") for p, _ in callers), callers
    finally:
        shutil.rmtree(d)


def test_generic_receivers_do_not_count_as_agreement():
    d = _repo({
        "a.py": "def f(result):\n    if result.status == 1:\n        pass\n",
        "b.py": "\n\n\n\n\ndef g(result):\n    if result.status == 2:\n"
                "        pass\n",
    })
    try:
        _write(d, {"a.py": "def f(result):\n    if result.status == 3:\n"
                           "        pass\n"})
        r = local_impact.analyse(d, "main")
        hits = _at(r, "b.py", 7)
        assert hits and all(f.severity == "low" for f in hits), hits
    finally:
        shutil.rmtree(d)


# ─── contract layer ─────────────────────────────────────────────────────────

PROTO_V1 = """syntax = "proto3";
message User {
  string id = 1;
  string phone_number = 2;
}
"""
PROTO_V2 = """syntax = "proto3";
message User {
  string id = 1;
}
"""


def test_contract_removed_field_finds_local_consumer():
    d = _repo({"api/user.proto": PROTO_V1,
               "web/profile.ts": "export const show = (u: any) => u.phoneNumber;\n"})
    try:
        _write(d, {"api/user.proto": PROTO_V2})
        r = local_impact.analyse(d, "main")
        removed = [c for c in r.contract_changes
                   if c.field_name == "phone_number"]
        assert removed, (r.contract_changes, r.contract_errors)
        assert any(c["path"] == "web/profile.ts" for c in removed[0].consumers), (
            removed[0].consumers)
        assert r.worst() == "high"
    finally:
        shutil.rmtree(d)


def test_contract_engine_failure_is_reported_not_swallowed():
    d = _repo({"openapi.yaml": "openapi: 3.0.0\npaths: {}\n"})
    try:
        _write(d, {"openapi.yaml": "openapi: [unclosed\n"})
        r = local_impact.analyse(d, "main")
        assert r.contract_errors and r.contract_errors[0][0] == "openapi.yaml", (
            "a spec the engine could not read vanished from the report")
        assert "could not diff" in local_impact.to_markdown(r)
    finally:
        shutil.rmtree(d)


def test_unreadable_spec_is_an_error_not_a_mass_removal():
    """A spec that cannot be read must not be diffed as "", which would report
    every one of its fields as removed."""
    d = _repo({"api/user.proto": PROTO_V1})
    try:
        big = PROTO_V1 + "// " + "x" * (local_diff.MAX_SCAN_BYTES + 1) + "\n"
        _write(d, {"api/user.proto": big})
        r = local_impact.analyse(d, "main")
        assert not r.contract_changes, r.contract_changes
        assert r.contract_errors and "could not read" in r.contract_errors[0][1]
    finally:
        shutil.rmtree(d)


# ─── noise rules from the 15-commit OSS replay (zod, httpx, cobra) ──────────

def test_comment_below_an_ended_function_is_not_an_edit_of_it():
    """zod: a changed comment above record() was attributed to isRef(), which
    had already closed, and every isRef() caller was reported."""
    base = ("export function isRef(x: any) {\n  return !!x.ref;\n}\n\n"
            "// keeps a record\nexport function record(a: any) {\n  return a;\n}\n")
    d = _repo({"m.ts": base,
               "use.ts": "import { isRef } from './m';\n\n\n\nisRef(1);\n"})
    try:
        _write(d, {"m.ts": base.replace("// keeps a record", "// keeps a record by ref")})
        r = local_impact.analyse(d, "main")
        assert not [f for f in r.findings if f.kind == "caller"], r.findings
    finally:
        shutil.rmtree(d)


def test_code_line_below_an_ended_function_is_not_an_edit_of_it():
    """zod 3a20805: a field added to an interface declared after a function was
    attributed to that function. Here: a top-level const after isRef()."""
    base = ("export function isRef(x: any) {\n  return !!x.ref;\n}\n\n"
            "export const LIMIT = 1;\n")
    d = _repo({"m.ts": base,
               "use.ts": "import { isRef } from './m';\n\n\n\nisRef(1);\n"})
    try:
        _write(d, {"m.ts": base.replace("LIMIT = 1", "LIMIT = 2")})
        r = local_impact.analyse(d, "main")
        assert not [f for f in r.findings if f.kind == "caller"], r.findings
    finally:
        shutil.rmtree(d)


def test_python_wrapped_signature_still_encloses_its_body():
    """httpx: `def create_ssl_context(` wrapped, with `) -> ssl.SSLContext:` at
    column 0. The first enclosure check took that line as the function's end
    and lost the two RELEVANT callers."""
    base = ("def create_ssl_context(\n    verify=True,\n) -> object:\n"
            "    if verify:\n        return 1\n    return 2\n")
    d = _repo({"cfg.py": base,
               "t.py": "from cfg import create_ssl_context\n\n\n\n"
                       "ctx = create_ssl_context(verify=False)\n"})
    try:
        _write(d, {"cfg.py": base.replace("return 2", "return 3")})
        r = local_impact.analyse(d, "main")
        assert _at(r, "t.py", 5, "caller"), r.findings
    finally:
        shutil.rmtree(d)


def test_literal_the_change_kept_is_not_agreement():
    """zod 7989d07: a condition rewritten with "backward" on both sides made
    every `=== "backward"` in the codebase MEDIUM."""
    d = _repo({
        "a.ts": "export function f(ctx: any, n: number) {\n"
                "  if (ctx.direction === 'backward' && n > 1) return 1;\n"
                "  return 0;\n}\n",
        "b.ts": "\n\n\n\nexport function g(c: any) {\n"
                "  if (c.direction === 'backward') return 2;\n}\n",
    })
    try:
        _write(d, {"a.ts": "export function f(ctx: any, n: number) {\n"
                           "  if (ctx.direction === 'backward' && n > 2) return 1;\n"
                           "  return 0;\n}\n"})
        r = local_impact.analyse(d, "main")
        hits = _at(r, "b.ts", 6, "same_predicate")
        assert all(f.severity == "low" for f in hits), hits
    finally:
        shutil.rmtree(d)


def test_module_attribute_is_not_a_predicate_key():
    """httpx 2ea2286: an added `if typing.TYPE_CHECKING:` made every module's
    own TYPE_CHECKING guard a MEDIUM. Uses a non-builtin module name, so only
    the imported-module rule can pass it."""
    d = _repo({"a.py": "import flags_lib\n\nX = 1\n",
               "b.py": "import flags_lib\n\n\n\n\nif flags_lib.beta_mode:\n"
                       "    import os\n"})
    try:
        _write(d, {"a.py": "import flags_lib\n\nif flags_lib.beta_mode:\n"
                           "    import os\nX = 1\n"})
        r = local_impact.analyse(d, "main")
        assert not r.findings, r.findings
    finally:
        shutil.rmtree(d)


def test_constant_on_an_object_is_still_a_predicate_key():
    """`settings.DEBUG` in two files IS related logic. An ALL_CAPS skip list,
    proposed alongside the module rule, would have hidden it."""
    d = _repo({"a.py": "def f(settings):\n    if settings.DEBUG == 1:\n"
                       "        return 1\n",
               "b.py": "\n\n\n\n\ndef g(settings):\n    if settings.DEBUG == 1:\n"
                       "        return 2\n"})
    try:
        _write(d, {"a.py": "def f(settings):\n    if settings.DEBUG == 2:\n"
                           "        return 1\n"})
        r = local_impact.analyse(d, "main")
        assert _at(r, "b.py", 7, "same_predicate"), r.findings
    finally:
        shutil.rmtree(d)


def test_test_files_neither_source_keys_nor_rank_above_low():
    d = _repo({
        "tests/test_a.py": "def check(job):\n    if job.phase == 'x':\n"
                           "        pass\n",
        "src.py": "\n\n\n\n\ndef run(job):\n    if job.phase == 'x':\n"
                  "        return 1\n",
        "tests/test_b.py": "\n\n\n\n\ndef t(job):\n    if job.phase == 'x':\n"
                           "        pass\n",
    })
    try:
        _write(d, {"tests/test_a.py": "def check(job):\n    if job.phase == 'y':\n"
                                      "        pass\n"})
        r = local_impact.analyse(d, "main")
        assert not r.findings, ("a changed test sourced keys: "
                                + repr([(f.path, f.line) for f in r.findings]))
        _git(d, "checkout", "-q", "--", ".")
        _write(d, {"src.py": "\n\n\n\n\ndef run(job):\n    if job.phase == 'y':\n"
                             "        return 1\n"})
        r = local_impact.analyse(d, "main")
        in_tests = [f for f in r.findings if f.path.startswith("tests/")]
        assert in_tests and all(f.severity == "low" for f in in_tests), in_tests
    finally:
        shutil.rmtree(d)


def test_many_callers_of_one_function_collapse_to_one_finding():
    calls = "".join(f"edit(1)\n" for _ in range(5))
    d = _repo({"e.py": "def edit(x):\n    return x\n",
               "u.py": "from e import edit\n\n\n\n" + calls})
    try:
        _write(d, {"e.py": "def edit(x):\n    return x + 1\n"})
        r = local_impact.analyse(d, "main")
        callers = [f for f in r.findings if f.kind == "caller"]
        assert len(callers) == 1 and len(callers[0].also) == 4, callers
    finally:
        shutil.rmtree(d)


# ─── command surface ────────────────────────────────────────────────────────

def _cli(argv):
    from app import cli
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.cmd_impact(argv)
    return code, out.getvalue(), err.getvalue()


def test_cli_exit_codes():
    d = _demo_repo()
    try:
        code, _, _ = _cli(["--repo", d, "--base", "main", "--no-write",
                           "--fail-on", "high"])
        assert code == 1, code
        code, _, _ = _cli(["--repo", d, "--base", "main", "--no-write"])
        assert code == 0, code
        code, _, err = _cli(["--repo", d, "--base", "no-such-ref", "--no-write"])
        assert code == 2 and "does not resolve" in err, (code, err)
    finally:
        shutil.rmtree(d)
    nd = tempfile.mkdtemp()
    try:
        code, _, _ = _cli(["--repo", nd, "--no-write"])
        assert code == 2, "a directory that is not a repo must not report clean"
    finally:
        shutil.rmtree(nd)


def test_json_report_is_machine_readable():
    d = _demo_repo()
    try:
        code, out, _ = _cli(["--repo", d, "--base", "main", "--no-write",
                             "--format", "json"])
        data = json.loads(out)
        assert data["worst"] == "high" and data["summary"]["high"] >= 1, data
    finally:
        shutil.rmtree(d)


def test_clean_tree_says_no_candidates_not_no_breakage():
    d = _repo({"a.py": "x = 1\n"})
    try:
        r = local_impact.analyse(d, "main")
        md = local_impact.to_markdown(r)
        assert "No candidates found" in md and "not a guarantee" in md, md
    finally:
        shutil.rmtree(d)


def test_report_directory_ignores_itself():
    """A second run must not see the first run's report as a change."""
    d = _repo({"a.py": "x = 1\n"})
    try:
        local_impact.write_reports(local_impact.analyse(d, "main"))
        cs = local_diff.collect_changes(d, "main")
        assert not cs.files, [f.path for f in cs.files]
    finally:
        shutil.rmtree(d)


def test_parse_unified_diff_handles_sql_comment_removal():
    """A removed SQL line `-- note` arrives as `--- note`: it is a removed line,
    not a new file header."""
    text = ("diff --git a/s.sql b/s.sql\n--- a/s.sql\n+++ b/s.sql\n"
            "@@ -2 +1,0 @@\n--- note\n")
    [fc] = local_diff.parse_unified_diff(text)
    assert fc.path == "s.sql" and fc.removed_lines == {2: "-- note"}, fc


def _main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed.append(name)
            print(f"  FAIL  {name}\n          {e}")
        except Exception as e:
            failed.append(name)
            print(f"  ERROR {name}\n          {type(e).__name__}: {e}")
    print(f"\n  {len(tests) - len(failed)}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
