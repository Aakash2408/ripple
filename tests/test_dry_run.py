"""Tests for the public /dry-run engine routing (app.dry_run._detect_changes).

Run:  python3.12 tests/test_dry_run.py

/dry-run is the page people try first. It routed OpenAPI (and SQL) to a
generic fallback comparison instead of the real engines, so the most common
breaking change -- a removed response field in a components schema -- came
back as "no breaking changes". These tests pin dry-run to the same engine
table `ripple impact`, `/poll` and the MCP server use.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.dry_run import _detect_changes  # noqa: E402


def _openapi(user_props: dict) -> str:
    return json.dumps({
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {"/users/{id}": {"get": {
            "parameters": [{"name": "id", "in": "path", "required": True,
                            "schema": {"type": "string"}}],
            "responses": {"200": {"description": "OK", "content": {
                "application/json": {"schema": {
                    "$ref": "#/components/schemas/User"}}}}}}}},
        "components": {"schemas": {"User": {
            "type": "object", "properties": user_props}}},
    })


BEFORE = _openapi({"id": {"type": "string"}, "email": {"type": "string"},
                   "phone": {"type": "string"}})
AFTER_REMOVED = _openapi({"id": {"type": "string"}, "email": {"type": "string"}})


def test_openapi_removed_ref_response_field_is_detected():
    changes, meta = _detect_changes(BEFORE, AFTER_REMOVED, "openapi")
    names = {c.field_name for c in changes if c.severity == "breaking"}
    assert "phone" in names, (
        f"removed response field not reported; got {names}, engine={meta}")
    assert "basic" not in meta["engine"], meta


def test_openapi_unchanged_spec_reports_nothing():
    changes, _ = _detect_changes(BEFORE, BEFORE, "openapi")
    assert [c for c in changes if c.severity == "breaking"] == []


def test_openapi_removed_endpoint_still_detected():
    before = json.loads(BEFORE)
    after = json.loads(BEFORE)
    del after["paths"]["/users/{id}"]
    changes, _ = _detect_changes(json.dumps(before), json.dumps(after), "openapi")
    assert any(c.change_type == "endpoint_removed" for c in changes), changes


def test_openapi_yaml_input_is_accepted():
    import yaml
    changes, meta = _detect_changes(
        yaml.safe_dump(json.loads(BEFORE)),
        yaml.safe_dump(json.loads(AFTER_REMOVED)), "openapi")
    assert "phone" in {c.field_name for c in changes}, (changes, meta)


def test_database_dropped_column_is_detected():
    before = "CREATE TABLE users (id INT PRIMARY KEY, email TEXT, phone TEXT);"
    after = "CREATE TABLE users (id INT PRIMARY KEY, email TEXT);"
    changes, meta = _detect_changes(before, after, "database")
    assert "phone" in {c.field_name for c in changes}, (changes, meta)
    assert "basic" not in meta["engine"], meta


def test_engine_crash_is_reported_as_degraded_not_hidden():
    changes, meta = _detect_changes("{not json", "{also not", "openapi")
    assert "engine_degraded" in meta, meta


if __name__ == "__main__":
    from _runner import run
    sys.exit(run(globals()))
