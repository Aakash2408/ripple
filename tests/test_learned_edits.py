"""Learning from reviewer edits (app/learned_edits.py + the webhook wiring).

Run: python tests/test_learned_edits.py

Every test here would pass against a learning layer that does nothing EXCEPT
the ones that assert a learned outcome differs from what the template alone
produces -- those are the tests that prove learning changes the code Ripple
writes, which is the whole point. Each such test checks the template-alone
result first, so a test cannot pass because the template happened to agree.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# Isolate every store this suite touches BEFORE any app module is imported:
# pr_ledger and learned_edits resolve their data directory at import time.
_DATA = tempfile.mkdtemp(prefix="ripple-learned-")
os.environ["RIPPLE_DATA_DIR"] = _DATA

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import learned_edits  # noqa: E402
from app.learned_edits import LearnedEditStore, apply, extract_rules  # noqa: E402
from app.fix_templates import apply_fix_template  # noqa: E402


def _store() -> LearnedEditStore:
    return LearnedEditStore(Path(tempfile.mkdtemp(dir=_DATA)) / "le.json")


def _ts_template(field):
    return lambda code: apply_fix_template(code, "typescript", "field_removed", field)


# The shape billing-api PR #5 got wrong in August: removing the parameter
# breaks every caller, and the shorthand cannot be removed without deciding
# what callers should do. A reviewer decides; Ripple should then follow.
CLIENT = """import { User } from "./types";

export interface CreateUserRequest {
  name: string;
  email: string;
  phoneNumber: string;
}

export class UserClient {
  async createUser(
    name: string,
    email: string,
    phoneNumber: string,
  ): Promise<User> {
    const request: CreateUserRequest = { name, email, phoneNumber };
    return post(request);
  }
}
"""

CLIENT_REVIEWED = CLIENT.replace("  phoneNumber: string;\n}", "}").replace(
    "{ name, email, phoneNumber }", "{ name, email }")


def _learn_client(store, scope="acme", source="https://github.com/acme/billing/pull/5"):
    return store.learn(scope=scope, change_type="removed_field", language="typescript",
                       field_name="phone_number", base=CLIENT, final=CLIENT_REVIEWED,
                       source=source)


def test_reviewer_choice_applies_to_a_new_field_in_the_same_shape():
    store = _store()
    summary = _learn_client(store)
    assert summary["rules_learned"] >= 2, summary

    other = CLIENT.replace("phoneNumber", "faxNumber")
    alone, _ = _ts_template("fax_number")(other)
    assert "{ name, email, faxNumber }" in alone, (
        "precondition: the template alone must NOT already make the reviewer's "
        "edit, or this test proves nothing about learning")

    r = apply(other, store=store, scope="acme", change_type="field_removed",
              language="typescript", field_name="fax_number",
              template_fn=_ts_template("fax_number"))
    assert r.applied, r.reason
    assert "{ name, email }" in r.code, r.code
    assert "faxNumber: string;\n}" not in r.code, "interface field not removed"
    assert "    faxNumber: string,\n" in r.code, (
        "the reviewer KEPT the parameter -- removing it breaks every caller")
    assert r.explanation.startswith("[learned]")
    assert "https://github.com/acme/billing/pull/5" in r.explanation


def test_a_kept_line_blocks_the_template_from_editing_it():
    base = ("interface Req {\n  name: string;\n  phoneNumber?: string;\n}\n"
            "const body = {\n  name: data.name,\n  phoneNumber: data.phoneNumber,\n};\n")
    # The reviewer accepted dropping the type field and REVERTED the payload edit.
    final = base.replace("  phoneNumber?: string;\n", "")
    store = _store()
    store.learn(scope="acme", change_type="field_removed", language="typescript",
                field_name="phone_number", base=base, final=final, source="pr-1")

    other = base.replace("phoneNumber", "faxNumber")
    alone, _ = _ts_template("fax_number")(other)
    assert "faxNumber: data.faxNumber" not in alone, (
        "precondition: the template alone removes the payload line")

    r = apply(other, store=store, scope="acme", change_type="field_removed",
              language="typescript", field_name="fax_number",
              template_fn=_ts_template("fax_number"))
    assert r.applied, r.reason
    assert "  faxNumber: data.faxNumber,\n" in r.code, r.code
    assert "faxNumber?: string" not in r.code, r.code


def test_rules_do_not_cross_orgs():
    store = _store()
    _learn_client(store, scope="acme")
    other = CLIENT.replace("phoneNumber", "faxNumber")
    r = apply(other, store=store, scope="globex", change_type="field_removed",
              language="typescript", field_name="fax_number",
              template_fn=_ts_template("fax_number"))
    assert not r.applied, "a rule containing acme's code was applied to globex"


def test_rules_do_not_cross_languages():
    store = _store()
    _learn_client(store)
    r = apply(CLIENT, store=store, scope="acme", change_type="field_removed",
              language="javascript", field_name="phone_number",
              template_fn=lambda c: (c, ""))
    assert not r.applied


def test_redelivered_webhook_votes_once():
    store = _store()
    first = _learn_client(store, source="pr-7")
    again = _learn_client(store, source="pr-7")
    assert again["rules_learned"] == 0 and again["rules_reinforced"] == 0, again
    assert again["already_counted"] == first["rules_learned"], (first, again)


def test_majority_outcome_wins_and_ties_go_to_the_latest():
    base = "const r = { name, phoneNumber };\n"
    drop = "const r = { name };\n"
    keep = base
    kw = dict(scope="acme", change_type="field_removed", language="typescript",
              field_name="phone_number", base=base)
    ident = lambda c: (c, "")                     # noqa: E731

    def winner(store):
        return apply(base, store=store, scope="acme", change_type="field_removed",
                     language="typescript", field_name="phone_number",
                     template_fn=ident).code

    tie = _store()
    tie.learn(final=drop, source="a", **kw)
    tie.learn(final=keep, source="b", **kw)       # 1-1, keep is newer
    assert winner(tie) == keep, "a tie should go to the most recent outcome"

    majority = _store()
    majority.learn(final=drop, source="a", **kw)
    majority.learn(final=drop, source="c", **kw)
    majority.learn(final=keep, source="b", **kw)  # newer, but outvoted 2-1
    assert winner(majority) == drop, "a newer minority beat the majority"


def test_a_dropped_marker_discards_the_learned_result():
    store = _store()
    _learn_client(store)

    def strips_comments(code):
        return "\n".join(l for l in code.split("\n") if not l.strip().startswith("//")), ""

    r = apply(CLIENT, store=store, scope="acme", change_type="field_removed",
              language="typescript", field_name="phone_number",
              template_fn=strips_comments)
    assert not r.applied
    assert "marker" in r.reason, r.reason


def test_indentation_follows_the_new_site():
    # Learned at depth 2, with a multi-line outcome nested one level deeper.
    base = "  const r = { a, phoneNumber };\n"
    final = "  const r = {\n    a,\n  };\n"
    store = _store()
    store.learn(scope="acme", change_type="field_removed", language="typescript",
                field_name="phone_number", base=base, final=final, source="p")
    code = "function f() {\n        const r = { a, faxNumber };\n}\n"
    r = apply(code, store=store, scope="acme", change_type="field_removed",
              language="typescript", field_name="fax_number",
              template_fn=lambda c: (c, ""))
    assert r.applied, r.reason
    assert r.code == ("function f() {\n        const r = {\n          a,\n"
                      "        };\n}\n"), repr(r.code)


def test_non_removal_rules_stay_field_specific():
    base = "const body = { name };\n"
    final = "const body = { name, country: user.country ?? 'US' };\n"
    # The site must reference the field to be learnable; for a newly required
    # field the reviewer's line is an insertion into a line WITHOUT it, so this
    # pins that such an edit is reported, not silently turned into a rule.
    rules, skipped = extract_rules(base, final, "country")
    assert rules == [], rules

    base = "const body = { name, country: undefined };\n"
    final = "const body = { name, country: user.country ?? 'US' };\n"
    store = _store()
    store.learn(scope="acme", change_type="added_required_field", language="typescript",
                field_name="country", base=base, final=final, source="p")
    ident = lambda c: (c, "")                     # noqa: E731
    other = base.replace("country", "region")
    r = apply(other, store=store, scope="acme", change_type="added_required_field",
              language="typescript", field_name="region", template_fn=ident)
    assert not r.applied, "a value chosen for `country` was applied to `region`"
    r = apply(base, store=store, scope="acme", change_type="added_required_field",
              language="typescript", field_name="country", template_fn=ident)
    assert r.applied and r.code == final, r.code


def test_rules_survive_a_restart():
    path = Path(tempfile.mkdtemp(dir=_DATA)) / "le.json"
    _learn_client(LearnedEditStore(path))
    fresh = LearnedEditStore(path)
    assert fresh.stats()["rules"] > 0
    r = apply(CLIENT, store=fresh, scope="acme", change_type="field_removed",
              language="typescript", field_name="phone_number",
              template_fn=_ts_template("phone_number"))
    assert r.applied and "{ name, email }" in r.code


def test_a_corrupt_store_raises_instead_of_forgetting():
    path = Path(tempfile.mkdtemp(dir=_DATA)) / "le.json"
    path.write_text("{not json")
    try:
        LearnedEditStore(path).stats()
    except ValueError:
        return
    raise AssertionError("a corrupt store loaded as empty -- every rule forgotten")


def test_unrelated_reviewer_edits_are_not_learned():
    base = "const a = 1;\nconst p = u.phoneNumber;\n"
    final = "const a = 2;\nconst p = u.phoneNumber;\n"
    rules, _ = extract_rules(base, final, "phone_number")
    sites = [r["site"] for r in rules]
    assert sites == [["const p = u.\u27e6camel\u27e7;"]], sites
    assert all(r["outcome"][0][2] == r["site"][0] for r in rules), "only a keep rule"


def test_an_unchanged_learned_result_is_final_not_a_fallthrough():
    from app import webhook as w
    from app.consumer_finder import ConsumerMatch
    from app.diff_engine import BreakingChange

    base = "const body = {\n  name: d.name,\n  phoneNumber: d.phoneNumber,\n};\n"
    store = _store()
    store.learn(scope="acme", change_type="field_removed", language="typescript",
                field_name="phone_number", base=base, final=base, source="keep-all")
    saved = learned_edits._default_store
    learned_edits._default_store = store
    try:
        consumer = ConsumerMatch(file_path="src/x.ts", line_number=0, code_snippet="",
                                 confidence="high", match_reason="t",
                                 language="typescript")
        change = BreakingChange(change_type="field_removed", field_name="phone_number",
                                field_type="string", path="/u", method="post",
                                location="request_body", severity="breaking",
                                description="")
        alone, _ = w._generate_with_template(base, consumer, change)
        assert alone != base, "precondition: the template alone edits this file"
        fixed, expl = w._generate_fix_with_rag_fallback(base, consumer, change, "acme")
        assert fixed == base, "fell through to the template the reviewer overruled"
        assert expl.startswith("[learned]")
    finally:
        learned_edits._default_store = saved


def test_a_learned_fix_is_recorded_as_learned_in_the_ledger():
    from app import webhook as w, pr_ledger
    from app.diff_engine import BreakingChange
    url = "https://github.com/acme/svc/pull/41"
    change = BreakingChange(change_type="field_removed", field_name="phone_number",
                            field_type="string", path="/u", method="post",
                            location="request_body", severity="breaking", description="")
    w._record_pr_provenance(url, platform="github", repo="acme/svc", change=change,
                            consumer_file="src/x.ts", language="typescript",
                            explanation="[learned] 1 site(s) ...", validated=None,
                            level="review")
    assert pr_ledger.lookup(url)["source"] == "learned"


class _PatchedGitHub:
    """Replaces the webhook's GitHub reads with a dict of (path, sha) -> text."""

    def __init__(self, files, authors=("reviewer-human",)):
        self.files, self.authors = files, list(authors)

    def __enter__(self):
        from app import webhook as w
        self.w = w
        self.saved = {k: getattr(w, k) for k in
                      ("_get_token", "_fetch_pr_commit_authors", "_github_api",
                       "_fetch_file_at_sha")}
        w._get_token = lambda *_a, **_k: "t"
        w._fetch_pr_commit_authors = lambda *_a, **_k: list(self.authors)
        w._github_api = lambda m, path, tok, *a, **k: (
            {"merge_base_commit": {"sha": "MB"}} if "/compare/" in path else {})
        w._fetch_file_at_sha = lambda repo, path, sha, tok: self.files.get((path, sha))
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(self.w, k, v)


def _merged_pr(url, commits=2):
    return {"number": int(url.rsplit("/", 1)[1]), "merged": True, "html_url": url,
            "commits": commits, "changed_files": 1,
            "base": {"sha": "BASE_TIP"}, "head": {"sha": "HEAD"},
            "user": {"login": "ripple-api[bot]"}}


def test_a_merged_with_edits_pr_teaches_the_next_fix_end_to_end():
    from app import webhook as w, pr_ledger
    from app.consumer_finder import ConsumerMatch
    from app.diff_engine import BreakingChange

    store = _store()
    saved = learned_edits._default_store
    learned_edits._default_store = store
    url = "https://github.com/acme/billing/pull/77"
    pr_ledger.record_open(url, pattern_id="", source="template",
                          change_type="field_removed", language="typescript",
                          field_name="phone_number", consumer_file="src/client.ts",
                          repo="acme/billing", validated=None, level="review")
    try:
        files = {("src/client.ts", "MB"): CLIENT, ("src/client.ts", "HEAD"): CLIENT_REVIEWED,
                 # Upstream moved after the PR opened; diffing against its tip
                 # instead of the merge base would mis-credit the reviewer.
                 ("src/client.ts", "BASE_TIP"): "// upstream rewrote this\n"}
        with _PatchedGitHub(files):
            result = w._handle_pr_merged({"repository": {"full_name": "acme/billing"},
                                          "installation": {"id": 1}}, _merged_pr(url))
        assert result["outcome"] == "merged_with_edits", result
        assert result["learned_edits"]["rules_learned"] >= 2, result

        other = CLIENT.replace("phoneNumber", "faxNumber")
        consumer = ConsumerMatch(file_path="src/other.ts", line_number=0,
                                 code_snippet="", confidence="high", match_reason="t",
                                 language="typescript")
        change = BreakingChange(change_type="field_removed", field_name="fax_number",
                                field_type="string", path="/u", method="post",
                                location="request_body", severity="breaking",
                                description="")
        fixed, expl = w._generate_fix_with_rag_fallback(other, consumer, change, "acme")
        assert "{ name, email }" in fixed and "    faxNumber: string,\n" in fixed, fixed
        assert expl.startswith("[learned]") and url in expl, expl

        # Another org gets the plain template.
        fixed_b, expl_b = w._generate_fix_with_rag_fallback(other, consumer, change, "globex")
        assert not expl_b.startswith("[learned]"), expl_b
    finally:
        learned_edits._default_store = saved


def test_an_unreadable_file_is_skipped_not_learned_as_empty():
    from app import webhook as w, pr_ledger
    store = _store()
    saved = learned_edits._default_store
    learned_edits._default_store = store
    url = "https://github.com/acme/billing/pull/78"
    pr_ledger.record_open(url, pattern_id="", source="template",
                          change_type="field_removed", language="typescript",
                          field_name="phone_number", consumer_file="src/client.ts",
                          repo="acme/billing", validated=None, level="review")
    try:
        with _PatchedGitHub({("src/client.ts", "MB"): CLIENT}):   # HEAD unreadable
            result = w._handle_pr_merged({"repository": {"full_name": "acme/billing"},
                                          "installation": {"id": 1}}, _merged_pr(url))
        le = result["learned_edits"]
        assert le["files"] == 0 and le["rules_learned"] == 0, le
        assert "could not read the merged version" in le["skipped_files"][0], le
        assert store.stats()["rules"] == 0
    finally:
        learned_edits._default_store = saved


def test_a_clean_merge_of_a_learned_fix_reinforces_its_rules():
    from app import webhook as w, pr_ledger
    store = _store()
    _learn_client(store, source="pr-first")
    saved = learned_edits._default_store
    learned_edits._default_store = store
    url = "https://github.com/acme/billing/pull/79"
    pr_ledger.record_open(url, pattern_id="", source="learned",
                          change_type="field_removed", language="typescript",
                          field_name="phone_number", consumer_file="src/client.ts",
                          repo="acme/billing", validated=None, level="review")
    try:
        files = {("src/client.ts", "MB"): CLIENT, ("src/client.ts", "HEAD"): CLIENT_REVIEWED}
        with _PatchedGitHub(files, authors=("ripple-api[bot]",)):
            result = w._handle_pr_merged({"repository": {"full_name": "acme/billing"},
                                          "installation": {"id": 1}},
                                         _merged_pr(url, commits=1))
        assert result["outcome"] == "merged_clean", result
        assert result["counters_updated"], result
        assert result["learned_edits"]["rules_reinforced"] >= 2, result
        assert not result["derived_pattern"], (
            "a learned fix was credited to the template as a derived pattern")
    finally:
        learned_edits._default_store = saved


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _runner import run
    sys.exit(run(globals()))
