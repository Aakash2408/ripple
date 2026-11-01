"""Removed operations (endpoint, channel, table, GraphQL root field, RPC) and
.graphql operation documents as consumers.

Before: a removed endpoint, channel or table found 0 consumers (each was
searched for as an identifier), a removed GraphQL root field matched every
line naming it -- and the fixer commented out `client.get("/orders")` for the
GraphQL `orders` field -- and .graphql query files were never scanned.
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.diff_engine import BreakingChange  # noqa: E402
from app.fix_templates import MARKER, apply_fix_template  # noqa: E402
from app.languages import detect, is_scannable  # noqa: E402
from app.operation_sites import (  # noqa: E402
    graphql_root_selections, is_operation_document, multiline_string_opens,
    operation_site_kinds, operation_sites,
)
from app.smart_consumer_finder import (  # noqa: E402
    consumer_query, find_field_consumers, find_matches_in_file, search_terms_for,
)


def _bc(change_type, field, method="", path="spec", location=""):
    return BreakingChange(change_type=change_type, path=path, method=method,
                          field_name=field, field_type="", location=location,
                          severity="breaking", description="")


def _fix(code, lang, bc):
    from app.fix_generator import _generate_with_template
    from app.consumer_finder import ConsumerMatch
    c = ConsumerMatch(file_path="f", line_number=1, code_snippet="",
                      confidence="high", match_reason="", language=lang)
    return _generate_with_template(code, c, bc)


# ── what each removed operation is searched for ─────────────────────────────

def test_query_per_operation_kind():
    assert consumer_query(_bc("endpoint_removed", "GET /orders", "get",
                              "/orders", "path")) == ("GET /orders", "endpoint")
    assert consumer_query(_bc("channel_removed", "user/deleted",
                              "user/deleted")) == ("user/deleted", "channel")
    assert consumer_query(_bc("table_removed", "orders", "orders")) == \
        ("orders", "table")
    assert consumer_query(_bc("operation_removed", "orders", "Query",
                              location="graphql")) == ("Query.orders",
                                                       "graphql_operation")
    # An RPC keeps the symbol search: consumers call client.DeleteUser(...).
    assert consumer_query(_bc("rpc_removed", "DeleteUser", "Users")) == \
        ("DeleteUser", "symbol")
    assert search_terms_for("user/{id}/deleted", "channel") == ["user"]
    assert search_terms_for("Query.orders", "graphql_operation") == ["orders"]


def test_table_removed_is_judgment():
    from app.change_types import canonical_op, is_judgment
    assert canonical_op("table_removed") == "remove_operation"
    assert is_judgment("table_removed")


# ── endpoint ────────────────────────────────────────────────────────────────

_API = ('await client.get("/orders");\n'
        'await client.post("/orders", b);\n'
        'const u = `${base}/orders`;\n'
        'await client.get("/orders/archive");\n')


def test_endpoint_calls_and_references():
    calls, refs = operation_site_kinds(_API, "GET /orders", "endpoint",
                                       "typescript")
    # POST /orders still exists; /orders/archive is another endpoint.
    assert (calls, refs) == ([0], [2]), (calls, refs)


def test_endpoint_fix_comments_out_only_the_call():
    out, why = _fix(_API, "typescript", _bc("endpoint_removed", "GET /orders",
                                            "get", "/orders", "path"))
    lines = out.split("\n")
    assert '// await client.get("/orders");' in lines, out
    assert 'await client.post("/orders", b);' in lines
    assert 'const u = `${base}/orders`;' in lines           # marked, kept
    assert out.count(MARKER) == 2 and "Commented out 1" in why, (out, why)


# ── channel ─────────────────────────────────────────────────────────────────

def test_channel_literal_matching():
    code = ('producer.send("user/deleted", msg)\n'
            'subscribe(["user/deleted/v2"])\n'
            'TOPIC = "user/deleted"\n'
            'log("user deleted")\n')
    assert operation_site_kinds(code, "user/deleted", "channel") == ([0], [2])
    assert operation_sites('x.on("user/42/deleted")', "user/{id}/deleted",
                           "channel") == [0]
    assert operation_sites('x.on("user/42/43/deleted")', "user/{id}/deleted",
                           "channel") == []


# ── table ───────────────────────────────────────────────────────────────────

_SQL = ('rows = db.execute("""\n'
        '  SELECT id\n'
        '  FROM orders o\n'
        '""")\n'
        'n = db.scalar("SELECT count(*) FROM public.orders")\n'
        'class O(Base):\n'
        '    __tablename__ = "orders"\n'
        'reorders = db.execute("SELECT 1 FROM orders_archive")\n')


def test_table_sites():
    assert operation_site_kinds(_SQL, "orders", "table", "python") == \
        ([2, 4], [6])


def test_table_fix_never_edits_inside_the_sql_string():
    out, why = _fix(_SQL, "python", _bc("table_removed", "orders", "orders"))
    lines = out.split("\n")
    # The triple-quoted query is untouched; its marker sits above the call.
    assert lines[0].startswith("# " + MARKER) and lines[1] == _SQL.split("\n")[0]
    assert "  FROM orders o" in lines
    assert '# n = db.scalar("SELECT count(*) FROM public.orders")' in lines
    assert '    __tablename__ = "orders"' in lines                # marked, kept
    assert 'reorders = db.execute("SELECT 1 FROM orders_archive")' in lines
    assert "Commented out 1" in why and "marked 2" in why, why


# ── GraphQL root field ──────────────────────────────────────────────────────

_GQL_TS = ('const Q = gql`\n'
           '  query Feed {\n'
           '    feed: orders(first: 2) { id }\n'
           '    me { orders { id } }\n'
           '  }\n'
           '`;\n'
           'const M = gql`mutation { orders }`;\n'
           'await client.get("/orders");\n')


def test_graphql_root_selection_only_in_the_right_operation():
    # Line 2 selects Query.orders (aliased); line 3's is User.orders; line 6
    # is a MUTATION field of the same name; line 7 is a REST call.
    assert operation_sites(_GQL_TS, "Query.orders", "graphql_operation",
                           "typescript") == [2]
    assert operation_sites(_GQL_TS, "Mutation.orders", "graphql_operation",
                           "typescript") == [6]


def test_graphql_operation_fix_marks_outside_the_document():
    out, why = _fix(_GQL_TS, "typescript", _bc("operation_removed", "orders",
                                               "Query", location="graphql"))
    lines = out.split("\n")
    # Marker above `const Q = gql\``, nothing inserted into the query.
    assert lines[0].startswith("// " + MARKER) and lines[1] == "const Q = gql`"
    assert out.count(MARKER) == 1, out
    assert 'await client.get("/orders");' in lines           # REST untouched
    assert "Commented out 0" in why
    # A one-line document is not inside a multi-line string, and it is still
    # a selection: marked, never commented out.
    one = 'const M = gql`{ orders { id } }`;\n'
    out, why = _fix(one, "typescript", _bc("operation_removed", "orders",
                                           "Query", location="graphql"))
    assert out.split("\n")[1] == one.rstrip("\n") and "Commented out 0" in why, out


def test_root_selection_parser():
    doc = ('fragment F on Query { orders }\n'
           'query($n: Int = 1) { a: orders(f: {x: "}"}) @skip(if: false) '
           '{ id } ...F b }\n')
    got = [(k, n) for _o, k, n in graphql_root_selections(doc)]
    assert got == [("Query", "orders"), ("query", "orders"), ("query", "b")], got
    # A "(" inside a string argument must not leave the parser in an
    # argument list for the rest of the document.
    tricky = 'query { a(f: "(") orders }'
    assert [n for _o, _k, n in graphql_root_selections(tricky)] == ["a", "orders"]


def test_multiline_string_opens():
    code = 'a = """\nx\n"""\nb = `\ny ${z}\n`\nc = "`"\n'
    assert multiline_string_opens(code) == {1: 0, 2: 0, 4: 3, 5: 3}


# ── RPC (symbol path) ───────────────────────────────────────────────────────

def test_rpc_fix_cuts_calls_marks_references_ignores_strings():
    code = ('await client.deleteUser({ id });\n'
            'log("deleteUser failed");\n'
            'const h = { deleteUser: handler };\n')
    out, why = _fix(code, "typescript", _bc("rpc_removed", "DeleteUser", "Users"))
    lines = out.split("\n")
    assert "// await client.deleteUser({ id });" in lines
    assert 'log("deleteUser failed");' in lines and \
        lines[lines.index('log("deleteUser failed");') - 1].strip() \
        .startswith("//") is True    # the line above is the commented call
    assert "const h = { deleteUser: handler };" in lines
    assert out.count(MARKER) == 2 and "Commented out 1" in why, (out, why)


def test_unaffected_file_gets_no_change():
    code = 'await client.post("/orders", b);\n'
    out, why = _fix(code, "typescript", _bc("endpoint_removed", "GET /orders",
                                            "get", "/orders", "path"))
    assert out == code and why.startswith("No change") and "never calls" in why, why


# ── .graphql operation documents ────────────────────────────────────────────

_DOC = ('# show email here\n'
        'query Me($email: String) {\n'
        '  me {\n'
        '    id\n'
        '    email\n'
        '    nick: email @include(if: true)\n'
        '    posts(filter: {email: $email}) { id }\n'
        '    profile { email avatar }\n'
        '  }\n'
        '}\n')
_SCHEMA = 'type User {\n  id: ID\n  email: String\n}\n'


def test_graphql_files_are_scanned_but_schemas_never_match():
    assert detect("src/queries/me.graphql") == "graphql"
    assert is_scannable("src/queries/me.gql")
    assert is_operation_document(_DOC) and not is_operation_document(_SCHEMA)
    assert not is_operation_document("query { a }\ntype A { a: Int }")
    hits = find_field_consumers(_DOC, "me.graphql", "email", "graphql")
    # Selections only (lines 5, 6, 8): not the comment, the $email variable
    # or the `email:` argument inside posts(...). High, so the CLI fixes it.
    assert [(m.line_number, m.confidence) for m in hits] == \
        [(5, 0.9), (6, 0.9), (8, 0.9)], hits
    assert find_field_consumers(_SCHEMA, "s.graphql", "email", "graphql") == []
    assert find_matches_in_file(_SCHEMA, "s.graphql", "email", "graphql") == []
    # Every vector is gated, not just the field matcher: a schema naming a
    # deleted package directory is still not a consumer of it.
    assert find_matches_in_file(_DOC, "api/users/me.graphql", "api/users",
                                "graphql", vector="package")
    assert find_matches_in_file(_SCHEMA, "api/users/s.graphql", "api/users",
                                "graphql", vector="package") == []


def test_graphql_remove_field_removes_selections_not_lines():
    out, _ = apply_fix_template(_DOC, "graphql", "field_removed", "email")
    assert out == ('# show email here\n'
                   'query Me($email: String) {\n'
                   '  me {\n'
                   '    id\n'
                   '    posts(filter: {email: $email}) { id }\n'
                   '    profile { avatar }\n'
                   '  }\n'
                   '}\n'), out


def test_cli_walk_finds_graphql_query_files():
    from app.consumer_finder import find_consumers
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "q"))
        open(os.path.join(d, "q", "me.graphql"), "w").write(_DOC)
        open(os.path.join(d, "schema.graphql"), "w").write(_SCHEMA)
        found = find_consumers([d], _bc("field_removed", "email", "User",
                                        location="graphql"))
    files = {(os.path.basename(m.file_path), m.confidence) for m in found}
    assert files == {("me.graphql", "high")}, files


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _runner import run
    sys.exit(run(globals()))
