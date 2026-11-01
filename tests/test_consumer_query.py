"""Consumer finding for NEWLY REQUIRED fields, headers and arguments.

A newly required field is absent from consumer code by definition, so every
consumer search keyed on the field name found the consumers that were already
fine and none that break: 0 of 3 for `country` on POST /users in
tests/consumers. These tests pin the replacement -- search for what the
consumer CALLS -- at every entry point that searches.
"""
from __future__ import annotations

import base64
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.diff_engine import BreakingChange  # noqa: E402
from app.smart_consumer_finder import (  # noqa: E402
    consumer_query, find_endpoint_consumers, find_matches_in_file,
    search_terms_for, _endpoint_path_regex,
)

CONSUMERS = os.path.join(ROOT, "tests", "consumers")


def _bc(change_type, path="/users", method="post", field="country",
        location="request_body"):
    return BreakingChange(change_type=change_type, path=path, method=method,
                          field_name=field, field_type="string",
                          location=location, severity="breaking",
                          description="")


# ── what to search for ──────────────────────────────────────────────────────

def test_query_http_add_required_is_the_endpoint():
    assert consumer_query(_bc("added_required_field")) == ("POST /users", "endpoint")
    assert consumer_query(_bc("required_header_added", field="X-Tenant",
                              location="parameter")) == ("POST /users", "endpoint")


def test_query_non_http_add_required_is_the_containing_type():
    # Non-HTTP engines put the spec FILE in `path` and the type in `method`.
    gql = _bc("required_field_added", path="api/schema.graphql",
              method="CreateUserInput", location="graphql")
    assert consumer_query(gql) == ("CreateUserInput", "symbol")
    sql = _bc("not_null_column_added", path="db/001.sql", method="users",
              field="country", location="database")
    assert consumer_query(sql) == ("users", "symbol")
    # GraphQL argument: the consumer names the operation field, not `Query`.
    arg = _bc("required_argument_added", path="schema.graphql", method="Query",
              field="users(offset)", location="graphql")
    assert consumer_query(arg) == ("users", "symbol")


def test_query_falls_back_when_change_names_no_anchor():
    # A JSON Schema root names nothing a consumer writes: keep the old search.
    root = _bc("required_property_added", path="schema.json", method="root",
               location="jsonschema")
    assert consumer_query(root) == ("country", "symbol")
    # An HTTP verb in `method` with a non-path `path` is not a type name.
    odd = _bc("added_required_field", path="User", method="post")
    assert consumer_query(odd) == ("country", "symbol")


def test_query_unchanged_for_non_add_required():
    assert consumer_query(_bc("removed_field", field="phone_number")) == (
        "phone_number", "symbol")
    assert consumer_query(_bc("field_type_changed", field="age")) == ("age", "symbol")


def test_search_terms_use_the_static_path_prefix():
    assert search_terms_for("POST /users", "endpoint") == ["/users"]
    assert search_terms_for("GET /users/{id}/posts", "endpoint") == ["/users"]
    assert search_terms_for("phone_number", "symbol") == ["phone_number"]


# ── matching the endpoint ───────────────────────────────────────────────────

def test_path_regex_boundaries():
    rx = _endpoint_path_regex("/users")
    assert rx.search("post('/users', body)")
    assert rx.search('requests.post(f"{BASE}/users?x=1")')
    assert rx.search("fetch('/api/v1/users')")           # base prefix allowed
    assert not rx.search("get(`/users/${id}`)")           # different operation
    assert not rx.search("get('/usersettings')")
    assert not rx.search("get('/users/me')")


def test_path_regex_parameters():
    rx = _endpoint_path_regex("/users/{id}")
    assert rx.search("get(`/users/${id}`)")
    assert rx.search('get(f"{BASE}/users/{user_id}")')
    assert rx.search('get(BASE + "/users/" + id)')
    assert rx.search("get('/users/42')")
    assert not rx.search("get('/users')")
    assert not rx.search("get('/users/42/posts')")


def test_verb_decides_whether_a_call_is_affected():
    code = "\n".join([
        "// Consumer of POST /users API",                 # comment: ignored
        "const a = await http.post('/users', body);",      # target verb
        "const b = await http.get('/users');",             # other verb
        "", "", "", "",                                    # out of the window
        "const url = '/users';",                           # no verb nearby
    ])
    got = {m.line_number: m.match_type
           for m in find_endpoint_consumers(code, "x.ts", "POST /users",
                                            "typescript", min_confidence=0.0)}
    assert got == {2: "endpoint_call", 3: "endpoint_other_method",
                   8: "endpoint_ref"}, got
    # At the default floor the GET is not a consumer of the POST.
    kept = {m.line_number for m in
            find_endpoint_consumers(code, "x.ts", "POST /users", "typescript")}
    assert kept == {2, 8}


def test_verb_on_a_nearby_line_counts():
    # Java HttpRequest builder: .uri(...) and .POST(...) on separate lines.
    code = "\n".join([
        "HttpRequest r = HttpRequest.newBuilder()",
        "    .uri(URI.create(BASE + \"/users\"))",
        "    .header(\"Content-Type\", \"application/json\")",
        "    .POST(body)",
        "    .build();",
    ])
    ms = find_endpoint_consumers(code, "C.java", "POST /users", "java")
    assert [(m.line_number, m.match_type) for m in ms] == [(2, "endpoint_call")]


def test_dispatch_routes_endpoint_vector():
    code = "requests.post(f'{BASE}/users', json=p)\n"
    ms = find_matches_in_file(code, "s.py", "POST /users", "python",
                              vector="endpoint")
    assert [m.match_type for m in ms] == ["endpoint_call"]


# ── end to end on the real test consumers ───────────────────────────────────

def test_cli_finder_finds_all_three_post_users_consumers():
    from app.consumer_finder import find_consumers
    matches = find_consumers([CONSUMERS], _bc("added_required_field"))
    files = sorted(os.path.basename(m.file_path) for m in matches)
    assert files == ["UserClient.java", "user_service.py", "users.ts"], files
    assert all(m.match_reason == "endpoint_call" for m in matches)
    # None of the GET calls on /users or /users/{id} is reported.
    assert not any(".get(" in m.code_snippet for m in matches)


def test_fix_anchors_only_the_post_call():
    from app.fix_generator import _call_site_hints
    path = os.path.join(CONSUMERS, "frontend", "src", "api", "users.ts")
    code = open(path).read()
    hints = _call_site_hints(_bc("added_required_field"), code, "typescript")
    assert hints == ("const response = await httpClient.post('/users', {",), hints
    # Without code, the old path-based anchors are kept.
    assert _call_site_hints(_bc("added_required_field"))[0] == "/users"


def test_fix_anchors_on_type_not_spec_file_for_non_http():
    from app.fix_generator import _call_site_hints
    gql = _bc("required_field_added", path="api/schema.graphql",
              method="CreateUserInput", location="graphql")
    hints = _call_site_hints(gql, "const i: CreateUserInput = {name}", "typescript")
    assert hints[0] == "CreateUserInput"
    assert not any("schema.graphql" in h for h in hints)


# ── production paths ────────────────────────────────────────────────────────

def _fake_repo(files: dict):
    """A _github_api stand-in serving a tree and contents for `files`."""
    calls = []

    def api(method, path, token, *a, **k):
        calls.append(path)
        if path == "/repos/o/r":
            return {"default_branch": "main"}
        if path.startswith("/repos/o/r/git/trees/"):
            return {"tree": [{"type": "blob", "path": p, "size": len(c)}
                             for p, c in files.items()]}
        if path.startswith("/repos/o/r/contents/"):
            p = path[len("/repos/o/r/contents/"):].split("?", 1)[0]
            if p in files:
                return {"content": base64.b64encode(files[p].encode()).decode()}
            return {"error": 404}
        if path.startswith("/search/code"):
            return {"items": [{"path": p} for p in files]}
        return {"error": 404}
    return api, calls


_FILES = {
    "src/create.ts": "await http.post('/users', {name});\n",
    "src/list.ts": "await http.get('/users');\n",
}


def test_webhook_tree_scan_finds_add_required_consumer():
    import app.webhook as W
    orig = W._github_api
    W._github_api, _ = _fake_repo(_FILES)
    try:
        found = W._scan_repo_tree_for_consumers(
            "o/r", _bc("added_required_field"), "tok")
    finally:
        W._github_api = orig
    assert [p for p, _c, _s in found] == ["src/create.ts"], found


def test_webhook_search_queries_the_path_not_the_field():
    import app.webhook as W
    from urllib.parse import unquote
    orig = W._github_api
    W._github_api, calls = _fake_repo(_FILES)
    try:
        found = W._search_repo_for_consumers(
            "o/r", _bc("added_required_field"), "tok")
    finally:
        W._github_api = orig
    q = unquote(next(c for c in calls if c.startswith("/search/code")))
    assert '"/users"' in q and "country" not in q, q
    assert [p for p, _c, _s in found] == ["src/create.ts"], found


def test_gitlab_bitbucket_admission_uses_endpoint_vector():
    import app.webhook as W
    admitted = W._admit_consumers(
        list(_FILES), lambda p: _FILES[p], field_name="POST /users",
        vector="endpoint", language_of=lambda p: "typescript",
        max_consumers=5, budget={"remaining": 10})
    assert [p for p, _c, _s in admitted] == ["src/create.ts"], admitted


# ── GraphQL argument removed ────────────────────────────────────────────────

def _arg_removed():
    return BreakingChange(change_type="argument_removed", path="schema.graphql",
                          method="Query", field_name="users(limit)",
                          field_type="Int", location="graphql",
                          severity="breaking", description="")


def test_query_graphql_argument_removed_is_the_argument_vector():
    assert consumer_query(_arg_removed()) == ("users(limit)", "argument")
    assert search_terms_for("users(limit)", "argument") == ["users"]
    # Other restrict_schema dialects keep searching their own name.
    other = _bc("input_schema_changed", path="CreateUser", method="",
                field="CreateUserInput", location="")
    assert consumer_query(other) == ("CreateUserInput", "symbol")


_GQL_PASSES = '''const Q = gql`
  query List($limit: Int) {
    users(limit: $limit) { id }
  }
`;
'''
_GQL_MULTI = '''Q = """
query {
  users(
    first: 2
    limit: 10
  ) { id }
}
"""
'''
_GQL_NO_ARG = 'const Q = gql`{ users { id } }`;\nconst opts = { limit: 5 };\n'


def test_finder_admits_only_queries_that_pass_it():
    hits = find_matches_in_file(_GQL_PASSES, "a.ts", "users(limit)",
                                "typescript", vector="argument")
    assert [(m.line_number, m.confidence) for m in hits] == [(3, 0.90)], hits
    multi = find_matches_in_file(_GQL_MULTI, "a.py", "users(limit)", "python",
                                 vector="argument")
    assert [m.line_number for m in multi] == [5], multi
    # Calls the field without the argument; `limit:` elsewhere is not a call.
    assert find_matches_in_file(_GQL_NO_ARG, "b.ts", "users(limit)",
                                "typescript", vector="argument") == []
    # Longer names and other fields' arguments do not count.
    assert find_matches_in_file("q = `{ users(ratelimit: 1) { id } "
                                "posts(limit: 2) }`", "c.ts", "users(limit)",
                                "typescript", vector="argument") == []
    assert find_matches_in_file("q = `{ allusers(limit: 1) { id } }`", "e.ts",
                                "users(limit)", "typescript",
                                vector="argument") == []
    assert find_matches_in_file("// users(limit: 1)\n", "d.ts", "users(limit)",
                                "typescript", vector="argument") == []


def test_fix_marks_the_passed_argument_with_a_graphql_comment():
    from app.fix_templates import apply_fix_template, MARKER
    out, why = apply_fix_template(_GQL_PASSES, "typescript", "argument_removed",
                                  "users(limit)")
    assert out.count(MARKER) == 1, out
    marker = [ln for ln in out.split("\n") if MARKER in ln][0]
    # Inside the gql`` string: `#`, the only comment GraphQL accepts.
    assert marker.strip().startswith("# "), marker
    assert "PARTIAL" in why and "did NOT delete" in why
    # The query is annotated, never edited.
    assert [ln for ln in out.split("\n") if MARKER not in ln] == \
        _GQL_PASSES.split("\n")
    # Re-running on already-marked code adds no second marker.
    again, _ = apply_fix_template(out, "typescript", "argument_removed",
                                  "users(limit)")
    assert again == out, again
    m2, _ = apply_fix_template(_GQL_MULTI, "python", "argument_removed",
                               "users(limit)")
    assert m2.split("\n")[4].strip().startswith("# " + MARKER), m2
    # A standalone .graphql operation file: no string around it, `#` comments.
    plain, _ = apply_fix_template("query { users(limit: 3) { id } }\n",
                                  "graphql", "argument_removed", "users(limit)")
    assert plain.startswith("# " + MARKER), plain


def test_fix_leaves_a_query_that_never_passes_it():
    from app.fix_templates import apply_fix_template
    out, why = apply_fix_template(_GQL_NO_ARG, "typescript", "argument_removed",
                                  "users(limit)")
    assert out == _GQL_NO_ARG, out
    assert why.startswith("No change") and "never passes" in why, why


def test_webhook_search_and_admission_use_the_argument_vector():
    import app.webhook as W
    from urllib.parse import unquote
    files = {"src/q.ts": _GQL_PASSES, "src/all.ts": _GQL_NO_ARG}
    orig = W._github_api
    W._github_api, calls = _fake_repo(files)
    try:
        found = W._search_repo_for_consumers("o/r", _arg_removed(), "tok")
    finally:
        W._github_api = orig
    q = unquote(next(c for c in calls if c.startswith("/search/code")))
    assert '"users"' in q and "limit" not in q, q
    assert [p for p, _c, _s in found] == ["src/q.ts"], found
    admitted = W._admit_consumers(
        list(files), lambda p: files[p], field_name="users(limit)",
        vector="argument", language_of=lambda p: "typescript",
        max_consumers=5, budget={"remaining": 10})
    assert [p for p, _c, _s in admitted] == ["src/q.ts"], admitted

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _runner import run
    sys.exit(run(globals()))
