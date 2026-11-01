"""Every contract engine, through the same dispatch the CLI, MCP server and
/poll use: a removed field, a changed type and a removed operation must each be
caught, and an unchanged spec must report nothing.

This was a one-off script twice and found a real miss both times (GraphQL type
changes; OpenAPI response-field type changes). As a gate, the next engine
regression fails pre-commit instead of waiting for someone to re-run it.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.change_types import canonical_op  # noqa: E402
from app.local_impact import diff_contract  # noqa: E402

C = {}
C["openapi"] = ("api/openapi.yaml", """openapi: 3.0.0
info: {title: t, version: '1'}
paths:
  /users:
    get:
      responses:
        '200':
          description: ok
          content:
            application/json:
              schema: {$ref: '#/components/schemas/User'}
  /orders:
    get:
      responses:
        '200': {description: ok}
    post:
      requestBody:
        content:
          application/json:
            schema:
              type: object
              properties:
                qty: {type: integer}
      responses:
        '201': {description: ok}
components:
  schemas:
    User:
      type: object
      properties:
        id: {type: integer}
        email: {type: string}
        age: {type: integer}
""", {
 "field_removed": ("        email: {type: string}\n", ""),
 "type_changed": ("        age: {type: integer}", "        age: {type: string}"),
 "op_removed": ("    get:\n      responses:\n        '200': {description: ok}\n    post:", "    post:"),
 "req_type_changed": ("qty: {type: integer}", "qty: {type: string}"),
})
C["protobuf"] = ("api/user.proto", """syntax = "proto3";
message User {
  int64 id = 1;
  string email = 2;
  int32 age = 3;
}
service Users {
  rpc GetUser(User) returns (User);
  rpc DeleteUser(User) returns (User);
}
""", {
 "field_removed": ("  string email = 2;\n", ""),
 "type_changed": ("  int32 age = 3;", "  string age = 3;"),
 "op_removed": ("  rpc DeleteUser(User) returns (User);\n", ""),
})
C["graphql"] = ("schema.graphql", """type User {
  id: ID!
  email: String
  age: Int
}
type Query {
  user(id: ID!): User
  users(limit: Int, offset: Int): [User!]!
  orders: [String]
}
""", {
 "field_removed": ("  email: String\n", ""),
 "type_changed": ("  age: Int\n", "  age: String\n"),
 "op_removed": ("  orders: [String]\n", ""),
 "argument_removed": ("users(limit: Int, offset: Int)", "users(offset: Int)"),
})
C["avro"] = ("user.avsc", json.dumps({"type": "record", "name": "User", "fields": [
    {"name": "id", "type": "long"}, {"name": "email", "type": "string"},
    {"name": "age", "type": "int"}]}, indent=1), {
 "field_removed": (',\n  {\n   "name": "email",\n   "type": "string"\n  }', ''),
 "type_changed": ('"name": "age",\n   "type": "int"', '"name": "age",\n   "type": "string"'),
})
C["thrift"] = ("user.thrift", """struct User {
  1: i64 id,
  2: string email,
  3: i32 age,
}
service Users {
  User getUser(1: i64 id),
  void deleteUser(1: i64 id),
}
""", {
 "field_removed": ("  2: string email,\n", ""),
 "type_changed": ("  3: i32 age,", "  3: string age,"),
 "op_removed": ("  void deleteUser(1: i64 id),\n", ""),
})
C["smithy"] = ("model/user.smithy", """$version: "2"
namespace example
service Users {
    operations: [GetUser, DeleteUser]
}
operation GetUser {
    input: User
}
operation DeleteUser {
    input: User
}
structure User {
    id: Long
    email: String
    age: Integer
}
""", {
 "field_removed": ("    email: String\n", ""),
 "type_changed": ("    age: Integer", "    age: String"),
 "op_removed": ("operation DeleteUser {\n    input: User\n}\n", ""),
})
C["asyncapi"] = ("asyncapi.yaml", """asyncapi: 2.6.0
info: {title: t, version: '1'}
channels:
  user/created:
    subscribe:
      message:
        payload:
          type: object
          properties:
            id: {type: integer}
            email: {type: string}
            age: {type: integer}
  user/deleted:
    subscribe:
      message:
        payload: {type: object}
""", {
 "field_removed": ("            email: {type: string}\n", ""),
 "type_changed": ("            age: {type: integer}", "            age: {type: string}"),
 "op_removed": ("  user/deleted:\n    subscribe:\n      message:\n        payload: {type: object}\n", ""),
})
C["jsonschema"] = ("user.schema.json", json.dumps({"type": "object", "properties": {
    "id": {"type": "integer"}, "email": {"type": "string"}, "age": {"type": "integer"}}}, indent=1), {
 "field_removed": (',\n  "email": {\n   "type": "string"\n  }', ''),
 "type_changed": ('"age": {\n   "type": "integer"', '"age": {\n   "type": "string"'),
})
C["sql"] = ("db/schema.sql", """CREATE TABLE users (
  id BIGINT PRIMARY KEY,
  email TEXT,
  age INTEGER
);
CREATE TABLE orders (
  id BIGINT PRIMARY KEY
);
""", {
 "field_removed": ("  email TEXT,\n", ""),
 "type_changed": ("  age INTEGER", "  age TEXT"),
 "op_removed": ("CREATE TABLE orders (\n  id BIGINT PRIMARY KEY\n);\n", ""),
})


#: (engine, case) -> change_type the engine must emit. Exact, so an engine that
#: starts reporting a removed field as something else is caught too.
EXPECT = {
    ("openapi", "field_removed"): "response_field_removed",
    ("openapi", "type_changed"): "field_type_changed",
    ("openapi", "req_type_changed"): "field_type_changed",
    ("openapi", "op_removed"): "endpoint_removed",
    ("protobuf", "field_removed"): "field_removed",
    ("protobuf", "type_changed"): "field_type_changed",
    ("protobuf", "op_removed"): "rpc_removed",
    ("graphql", "field_removed"): "field_removed",
    ("graphql", "type_changed"): "field_type_changed",
    ("graphql", "op_removed"): "operation_removed",
    ("graphql", "argument_removed"): "argument_removed",
    ("avro", "field_removed"): "field_removed",
    ("avro", "type_changed"): "field_type_changed",
    ("thrift", "field_removed"): "field_removed",
    ("thrift", "type_changed"): "field_type_changed",
    ("thrift", "op_removed"): "method_removed",
    ("smithy", "field_removed"): "member_removed",
    ("smithy", "type_changed"): "member_type_changed",
    ("smithy", "op_removed"): "operation_removed",
    ("asyncapi", "field_removed"): "message_field_removed",
    ("asyncapi", "type_changed"): "field_type_changed",
    ("asyncapi", "op_removed"): "channel_removed",
    ("jsonschema", "field_removed"): "property_removed",
    ("jsonschema", "type_changed"): "property_type_changed",
    ("sql", "field_removed"): "column_removed",
    ("sql", "type_changed"): "column_type_changed",
    ("sql", "op_removed"): "table_removed",
}


def _new(ctype, case):
    path, old, edits = C[ctype]
    a, b = edits[case]
    assert old.count(a) == 1, f"fixture anchor for {ctype}/{case} is not unique"
    return path, old, old.replace(a, b)


def test_every_engine_catches_every_case():
    missed = []
    for (ctype, case), want in EXPECT.items():
        path, old, new = _new(ctype, case)
        got = [c.change_type for c in diff_contract(ctype, old, new, path)]
        if want not in got:
            missed.append(f"{ctype}/{case}: want {want}, got {got}")
    assert not missed, "\n".join(missed)


def test_matrix_covers_every_fixture_case():
    declared = {(t, k) for t, (_p, _o, e) in C.items() for k in e}
    assert declared == set(EXPECT), declared ^ set(EXPECT)
    assert len(C) == 9


def test_unchanged_spec_reports_nothing():
    noisy = {t: [c.change_type for c in diff_contract(t, old, old, path)]
             for t, (path, old, _e) in C.items()}
    assert not any(noisy.values()), noisy


def test_every_emitted_type_is_classified():
    # An unmapped dialect falls to canonical_op's suffix guesses; every type
    # this matrix sees must route somewhere, never to "".
    for (ctype, case), want in EXPECT.items():
        assert canonical_op(want), f"{ctype}/{case}: {want} unclassified"


_TS = ("export interface User {\n  id: number;\n  email: string;\n}\n"
       "export const show = (u: User) => u.email;\n")


def _consumer(path="src/c.ts", line=3):
    from app.consumer_finder import ConsumerMatch
    return ConsumerMatch(file_path=path, line_number=line, code_snippet="",
                         confidence="high", match_reason="field",
                         language="typescript")


def test_template_fix_routes_every_dialect_by_operation():
    from app.diff_engine import BreakingChange
    from app.fix_generator import _generate_with_template
    # Each was "Unsupported change type" when this matched literal strings.
    for ct in ("response_field_removed", "column_removed", "member_removed",
               "property_removed", "message_field_removed"):
        bc = BreakingChange(change_type=ct, path="/u", method="get",
                            field_name="email", field_type="string",
                            location="", severity="breaking", description="")
        fixed, why = _generate_with_template(_TS, _consumer(), bc)
        # Routed to the remove-field handler. The template refuses
        # `u.email` in an arrow body, so the answer is markers on both
        # references -- never the old line fallback, which deleted the whole
        # `export const show` line to make "email" disappear.
        assert "Unsupported" not in why, (ct, why)
        assert fixed.count("RIPPLE-ACTION-REQUIRED") == 2, (ct, why, fixed)
        assert "export const show = (u: User) => u.email;" in fixed, fixed


def test_one_fix_per_file():
    import tempfile
    from app.diff_engine import BreakingChange
    from app.fix_generator import generate_fixes
    with tempfile.NamedTemporaryFile("w", suffix=".ts", delete=False) as fh:
        fh.write(_TS)
    try:
        bc = BreakingChange(change_type="field_removed", path="/u", method="get",
                            field_name="email", field_type="string",
                            location="", severity="breaking", description="")
        fixes = generate_fixes([_consumer(fh.name, 3), _consumer(fh.name, 5)],
                               bc, use_llm=False)
    finally:
        os.unlink(fh.name)
    assert len(fixes) == 1, [f.consumer.line_number for f in fixes]



def test_cli_diff_routes_by_spec_type():
    # `ripple diff/scan/run` sent every file to the OpenAPI engine: a .proto
    # or .graphql crashed with a YAML traceback.
    import tempfile
    from app.cli import diff_any
    for ctype in ("protobuf", "graphql", "sql", "openapi"):
        path, old, edits = C[ctype]
        a, b = edits["field_removed"]
        with tempfile.TemporaryDirectory() as d:
            name = os.path.basename(path)
            po, pn = os.path.join(d, "old-" + name), os.path.join(d, "new-" + name)
            open(po, "w").write(old)
            open(pn, "w").write(old.replace(a, b))
            got = [c.change_type for c in diff_any(po, pn).breaking_changes]
        assert EXPECT[(ctype, "field_removed")] in got, (ctype, got)


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _runner import run
    sys.exit(run(globals()))
