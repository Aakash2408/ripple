from __future__ import annotations
"""
Regression suite for Ripple's detection and fix pipeline.

Every test here corresponds to a bug that actually shipped and silently
broke the product. They are grouped by failure class so a regression tells
you immediately WHICH invariant broke.

Two classes matter most:

  FALSE NEGATIVE  -- Ripple says "no breaking changes" when the schema DID
                     break. The user trusts the silence. Worst case.
  BROKEN FIX      -- Ripple opens a PR whose code does not compile or run.
                     Destroys trust on first contact.

The seam tests exist because all 13 bugs found on 2026-08-15/16 were at
module boundaries, not inside modules. Each module passed in isolation.

Run:  python3.12 -m pytest tests/test_regression.py -q
  or: python3.12 tests/test_regression.py     (no pytest needed)
"""

import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.proto_diff import diff_proto, parse_proto_schema
from app.schema_parse import strip_comments, extract_blocks
from app.fix_templates import apply_fix_template, _clean_trailing_commas, _remove_empty_blocks
from app.smart_consumer_finder import generate_variants, file_is_consumer, find_residual_references


# ===================================================================
# CLASS 1: FALSE NEGATIVES -- silent "no breaking changes"
# ===================================================================

def test_nested_message_does_not_hide_later_fields():
    """r'\\{([^}]*)\\}' truncated the body at the nested message's '}',
    making every field after it invisible."""
    old = """message U {
  message Inner { string x = 1; }
  string keep = 1;
  string gone = 2;
}"""
    new = """message U {
  message Inner { string x = 1; }
  string keep = 1;
}"""
    changes = diff_proto(old, new)
    assert any(c.change_type == "field_removed" and c.field_name == "gone"
               for c in changes), "field after a nested message was not detected"


def test_oneof_does_not_hide_later_fields():
    old = """message U {
  oneof choice { string s = 1; int32 i = 2; }
  string keep = 3;
  string gone = 4;
}"""
    new = """message U {
  oneof choice { string s = 1; int32 i = 2; }
  string keep = 3;
}"""
    changes = diff_proto(old, new)
    assert any(c.field_name == "gone" for c in changes), \
        "field after a oneof was not detected"


def test_inner_enum_does_not_hide_later_fields():
    old = """message U {
  enum E { A = 0; B = 1; }
  string keep = 1;
  string gone = 2;
}"""
    new = """message U {
  enum E { A = 0; B = 1; }
  string keep = 1;
}"""
    assert any(c.field_name == "gone" for c in diff_proto(old, new))


def test_rpc_removal_is_detected():
    """`service` blocks were never parsed at all. Removing an rpc breaks
    every caller -- the most severe gRPC change possible."""
    old = """service S {
  rpc GetUser(Req) returns (Res);
  rpc DeleteUser(Req) returns (Res);
}"""
    new = """service S {
  rpc GetUser(Req) returns (Res);
}"""
    changes = diff_proto(old, new)
    assert any(c.change_type == "rpc_removed" and c.field_name == "DeleteUser"
               for c in changes), "rpc removal not detected"


def test_rpc_signature_change_is_detected():
    old = "service S {\n  rpc Get(ReqA) returns (Res);\n}"
    new = "service S {\n  rpc Get(ReqB) returns (Res);\n}"
    assert any(c.change_type == "rpc_signature_changed" for c in diff_proto(old, new))


def test_service_removal_is_detected():
    old = "service S {\n  rpc Get(R) returns (P);\n}"
    new = ""
    assert any(c.change_type == "service_removed" for c in diff_proto(old, new))


def test_enum_value_removal_is_detected():
    old = "enum Role { ADMIN = 0; USER = 1; GUEST = 2; }"
    new = "enum Role { ADMIN = 0; USER = 1; }"
    assert any(c.change_type == "enum_value_removed" and c.field_name == "GUEST"
               for c in diff_proto(old, new))


def test_field_number_change_is_detected():
    old = "message U { string a = 1; string b = 2; }"
    new = "message U { string a = 1; string b = 5; }"
    assert any(c.change_type == "field_number_changed" for c in diff_proto(old, new))


def test_field_type_change_is_detected():
    old = "message U { string a = 1; }"
    new = "message U { int32 a = 1; }"
    assert any(c.change_type == "field_type_changed" for c in diff_proto(old, new))


def test_map_field_removal_is_detected():
    old = "message U {\n  map<string, string> meta = 1;\n  string gone = 2;\n}"
    new = "message U {\n  map<string, string> meta = 1;\n}"
    assert any(c.field_name == "gone" for c in diff_proto(old, new))


# ===================================================================
# CLASS 2: FALSE POSITIVES -- PRs for changes that never happened
# ===================================================================

def test_comment_only_change_is_not_breaking():
    """Comments were never stripped, so `// string phone = 2;` parsed as a
    live field and deleting the comment fabricated a breaking change."""
    old = """message U {
  string keep = 1;
  // string gone = 2;
}"""
    new = """message U {
  string keep = 1;
}"""
    assert diff_proto(old, new) == [], \
        "comment-only edit reported as a breaking change"


def test_block_comment_field_is_not_a_field():
    old = """message U {
  string keep = 1;
  /* string gone = 2; */
}"""
    new = "message U {\n  string keep = 1;\n}"
    assert diff_proto(old, new) == []


def test_adding_a_field_is_not_breaking():
    old = "message U { string a = 1; }"
    new = "message U { string a = 1; string b = 2; }"
    assert diff_proto(old, new) == [], "adding an optional field is not breaking"


def test_url_in_string_is_not_a_comment():
    """strip_comments must not treat '//' inside a string literal as a
    comment, or it would corrupt the remainder of the schema."""
    assert strip_comments('option x = "http://example.com";') == \
        'option x = "http://example.com";'


# ===================================================================
# CLASS 3: BROKEN FIXES -- PRs containing code that will not build
# ===================================================================

def test_go_composite_literal_keeps_trailing_comma():
    """Go REQUIRES the trailing comma when '}' is on the next line. The
    cleanup regex stripped it, so Ripple shipped PRs that did not compile."""
    code = """req := &pb.CreateUserRequest{
\tName:        name,
\tEmail:       email,
\tPhoneNumber: phone,
}"""
    fixed, _explanation = apply_fix_template(code, "go", "field_removed", "phone_number")
    literal_lines = [l for l in fixed.splitlines()
                     if ":" in l and not l.strip().startswith("//")
                     and "{" not in l]
    assert literal_lines, "expected literal body lines to survive"
    assert literal_lines[-1].rstrip().endswith(","), \
        f"trailing comma stripped -- Go will not compile: {literal_lines[-1]!r}"


def test_multiline_trailing_comma_is_preserved():
    code = 'send(\n    to=x,\n    body=y,\n)'
    assert _clean_trailing_commas(code) == code
    assert _remove_empty_blocks(code) == code


def test_doubled_comma_is_collapsed():
    assert _clean_trailing_commas("foo(a,, c)") == "foo(a, c)"


def test_comma_after_open_paren_is_removed():
    assert _clean_trailing_commas("foo(, b)") == "foo(b)"


def test_python_fix_output_is_parseable():
    import ast
    code = """from dataclasses import dataclass

@dataclass
class User:
    name: str
    email: str
    phone_number: str
"""
    fixed, _explanation = apply_fix_template(code, "python", "field_removed", "phone_number")
    ast.parse(fixed)  # raises SyntaxError on regression
    assert "phone_number" not in fixed, "declaration was not removed"


# ===================================================================
# CLASS 4: SEAM BUGS -- modules correct alone, wiring wrong
# ===================================================================

def test_generate_variants_covers_all_three_casings():
    """generate_variants() was computed then DISCARDED; the search only
    ever used snake_case, so Go (PhoneNumber) and TS (phoneNumber)
    consumers were invisible."""
    variants = generate_variants("phone_number")
    for required in ("phone_number", "phoneNumber", "PhoneNumber"):
        assert required in variants, f"{required} missing from variants"


def test_file_is_consumer_matches_each_language_casing():
    cases = [
        ("handler.go", "go", "\t\tPhoneNumber: phone,"),
        ("client.ts", "typescript", "  phoneNumber: string;"),
        ("svc.py", "python", "    phone_number: str"),
    ]
    for fname, lang, line in cases:
        is_consumer, conf, _ = file_is_consumer(
            line, fname, "phone_number", lang, min_confidence=0.5
        )
        assert is_consumer, f"{lang} consumer not detected in {fname}"


def test_proto_change_type_string_matches_template_dispatch():
    """proto_diff emitted 'field_removed' while fix_generator checked for
    'removed_field' -- an exact-string mismatch that produced zero fixes."""
    changes = diff_proto(
        "message U { string a = 1; string phone_number = 2; }",
        "message U { string a = 1; }",
    )
    assert changes, "expected a breaking change"
    change_type = changes[0].change_type
    fixed, _explanation = apply_fix_template(
        "type U struct {\n\tPhoneNumber string\n}", "go", change_type, "phone_number"
    )
    assert isinstance(fixed, str), "apply_fix_template must return (code, explanation)"
    assert "PhoneNumber" not in fixed, \
        f"template did not handle change_type {change_type!r} emitted by the diff engine"


def test_residual_references_are_reported():
    """Removing a declaration but leaving `user.phone_number` reads
    produces code that AttributeErrors. Those must be surfaced, never
    silently shipped as a complete fix."""
    fixed = "def f(user):\n    return send(to=user.phone_number)\n"
    residual = find_residual_references(fixed, "phone_number", "python")
    assert residual, "surviving reference not reported"
    assert residual[0][0] == 2, "wrong line number reported"


def test_residual_references_ignore_comments():
    fixed = "def f(user):\n    # phone_number was removed\n    return 1\n"
    assert find_residual_references(fixed, "phone_number", "python") == [], \
        "comment-only mention should not be flagged as a runtime break"


def test_parser_handles_unbalanced_braces_without_crashing():
    """Malformed input must not raise -- a webhook crash is invisible to
    the user and looks identical to 'no changes'."""
    schema = parse_proto_schema("message U { string a = 1;")
    assert isinstance(schema.messages, dict)


def test_extract_blocks_ignores_braces_in_strings():
    blocks = extract_blocks('message U { string s = "}"; int32 a = 1; }', "message")
    assert len(blocks) == 1
    assert "int32 a = 1" in blocks[0][1]


# ===================================================================
# CLASS 5: AUTH / SCOPE -- the band-aids must stay deleted
# ===================================================================

def test_app_jwt_is_valid_rs256_within_github_limits():
    """A malformed App JWT means every installation token exchange fails,
    which surfaces later as 'no consumers found'."""
    import base64
    import json as _json
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.asymmetric import rsa, padding
    from app import github_app_auth as gaa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()

    token = gaa.build_app_jwt(app_id="42", private_key_pem=pem)
    header_b64, payload_b64, sig_b64 = token.split(".")

    def _d(part):
        return _json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))

    assert _d(header_b64)["alg"] == "RS256"
    payload = _d(payload_b64)
    assert payload["iss"] == "42"
    assert payload["exp"] - payload["iat"] <= 600, "GitHub caps App JWT at 10 min"

    sig = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
    key.public_key().verify(
        sig, f"{header_b64}.{payload_b64}".encode(),
        padding.PKCS1v15(), hashes.SHA256(),
    )  # raises on invalid signature


def test_private_key_accepts_escaped_newline_pem():
    """Railway and most env-var stores carry PEMs with literal \\n."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from app import github_app_auth as gaa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()

    old = os.environ.get("GITHUB_APP_PRIVATE_KEY")
    try:
        os.environ["GITHUB_APP_PRIVATE_KEY"] = pem.replace("\n", "\\n")
        loaded = gaa.get_private_key()
        assert "-----BEGIN" in loaded and "\n" in loaded
        gaa.build_app_jwt(app_id="1", private_key_pem=loaded)
    finally:
        if old is None:
            os.environ.pop("GITHUB_APP_PRIVATE_KEY", None)
        else:
            os.environ["GITHUB_APP_PRIVATE_KEY"] = old


def test_missing_app_config_raises_not_returns_empty():
    """Silent '' would look identical to a healthy no-op downstream."""
    from app import github_app_auth as gaa
    saved = {k: os.environ.pop(k, None)
             for k in ("GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY",
                       "GITHUB_APP_PRIVATE_KEY_PATH")}
    try:
        assert gaa.is_app_configured() is False
        raised = False
        try:
            gaa.build_app_jwt()
        except gaa.AppAuthError:
            raised = True
        assert raised, "misconfigured App auth must raise, not return ''"
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_repo_cap_bandaid_is_gone():
    """RIPPLE_MAX_CONSUMER_REPOS silently dropped consumers for anyone with
    more repos than the cap -- the same silent-false-negative class we
    removed from the parser. It must not come back."""
    source = open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "app", "webhook.py")).read()
    assert "RIPPLE_MAX_CONSUMER_REPOS" not in source, \
        "repo cap heuristic reintroduced"


def test_self_repo_blocklist_bandaid_is_gone():
    """The hardcoded '{owner}/ripple' exclusion suppressed a symptom of
    unscoped discovery. Authoritative installation scope replaces it."""
    source = open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "app", "webhook.py")).read()
    assert 'f"{owner}/ripple"' not in source, "self-repo blocklist reintroduced"
    assert "RIPPLE_EXCLUDE_REPOS" not in source, "exclude-repos heuristic reintroduced"


def test_presentation_dir_heuristic_is_gone_but_vendored_still_excluded():
    """Deleting the marketing/docs guesses must NOT also delete the
    objectively-correct vendored/generated exclusions."""
    from app.webhook import _is_code_file
    # judgment-call dirs are no longer blanket-excluded
    assert _is_code_file("examples/client.go") is True
    assert _is_code_file("website/src/api/userClient.ts") is True
    # vendored + generated remain excluded
    assert _is_code_file("node_modules/foo/index.js") is False
    assert _is_code_file("vendor/pkg/thing.go") is False
    assert _is_code_file("gen/user/v1/user.pb.go") is False
    assert _is_code_file("api/user_pb2.py") is False
    assert _is_code_file("dist/bundle.min.js") is False
    # ordinary source still qualifies
    assert _is_code_file("internal/handler/user.go") is True


# ===================================================================
# CLASS 6: RESILIENCE -- transient faults must not kill a run
# ===================================================================

def test_transient_connection_error_is_retried():
    """RemoteDisconnected is NOT an HTTPError, so it previously escaped
    _github_api and killed the whole spec run with
    'Remote end closed connection without response'. The tree fallback
    issues hundreds of calls, so a mid-run blip must not discard the work."""
    import http.client
    from app import webhook as wh

    calls = {"n": 0}
    real = wh.urlopen

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise http.client.RemoteDisconnected("closed")
        return real(*a, **k)

    wh.urlopen = flaky
    try:
        wh._github_api("GET", "/zen", "invalid-token")
        assert calls["n"] == 3, f"expected 2 retries then success, got {calls['n']} attempts"
    finally:
        wh.urlopen = real


def test_exhausted_retries_return_error_not_exception():
    """After the retry budget, return an error dict. Raising here would
    abort the run; returning '' would look like 'no consumers found'."""
    import http.client
    from app import webhook as wh

    real = wh.urlopen

    def always_fail(*a, **k):
        raise http.client.RemoteDisconnected("boom")

    wh.urlopen = always_fail
    try:
        result = wh._github_api("GET", "/zen", "t")
        assert result.get("error") == "transient", f"unexpected: {result}"
        assert "RemoteDisconnected" in result.get("message", "")
    finally:
        wh.urlopen = real


def test_permanent_http_errors_are_not_retried():
    """404/422 cannot be fixed by retrying -- burning the budget on them
    would slow every run for nothing."""
    from urllib.error import HTTPError
    from app import webhook as wh

    calls = {"n": 0}
    real = wh.urlopen

    def not_found(*a, **k):
        calls["n"] += 1
        raise HTTPError("u", 404, "Not Found", {}, None)

    wh.urlopen = not_found
    try:
        result = wh._github_api("GET", "/nope", "t")
        assert result.get("error") == 404
        assert calls["n"] == 1, f"404 should not be retried, made {calls['n']} calls"
    finally:
        wh.urlopen = real


def test_tree_scan_respects_call_budget():
    """An unbounded scan across a wide installation scope is what triggered
    the connection drops. Budget exhaustion must stop the scan and be
    logged, never silently truncate."""
    from app import webhook as wh
    budget = {"remaining": 0}
    before = len(wh._activity_log)

    class _Change:
        field_name = "phone_number"
        # The real BreakingChange always carries change_type, and the tree scan
        # now derives the propagation vector from it. A stub missing it was
        # silently diverging from the real shape.
        change_type = "field_removed"

    result = wh._scan_repo_tree_for_consumers(
        "Aakash2408/user-proto", _Change(), "invalid", "", budget
    )
    assert result == [], "no results expected with an invalid token"
    assert budget["remaining"] == 0
    assert len(wh._activity_log) >= before


def test_variant_order_is_deterministic():
    """generate_variants() returned a set(), and Python randomises string
    hashes per process -- so order changed every run. The search query takes
    only the first few variants, so which ones got searched was random and a
    consumer could be found on one run and silently missed on the next."""
    order = generate_variants("phone_number")
    for _ in range(20):
        assert generate_variants("phone_number") == order, "variant order unstable"
    # canonical conventions must come before accessor forms
    assert order.index("phone_number") < order.index("getPhoneNumber")
    assert order.index("phoneNumber") < order.index("getPhoneNumber")
    assert order.index("PhoneNumber") < order.index("getPhoneNumber")


def test_confidence_is_stable_and_uses_strongest_match():
    """The membership test is case-INSENSITIVE but classify_match is
    case-SENSITIVE, and the loop used to break on the first hit. For
    `PhoneNumber: phone,` both phoneNumber and PhoneNumber pass membership
    but only PhoneNumber scores 0.95, so the result depended on random
    variant order. All matching variants are now scored, strongest wins."""
    go_line = "\t\tPhoneNumber: phone,"
    results = {
        file_is_consumer(go_line, "handler.go", "phone_number", "go", 0.5)[1]
        for _ in range(20)
    }
    assert len(results) == 1, f"confidence unstable across runs: {results}"
    assert results.pop() >= 0.9, "field assignment should score high"


def test_case_mismatched_variant_does_not_lower_score():
    """A Go struct-field assignment must score as a field assignment even
    though a camelCase variant also passes the case-insensitive membership
    test."""
    _, conf, matches = file_is_consumer(
        "\t\tPhoneNumber: phone,", "handler.go", "phone_number", "go", 0.5
    )
    assert conf >= 0.9, f"expected >=0.9 for struct field assignment, got {conf}"
    assert matches[0].variant_matched == "PhoneNumber", \
        f"strongest variant should win, got {matches[0].variant_matched}"


# ===================================================================
# CLASS 7: CROSS-ENGINE -- graphql / smithy / thrift on schema_parse
# ===================================================================

def test_graphql_brace_default_does_not_hide_later_fields():
    """r'\\{([^}]*)\\}' truncated a GraphQL type body at the first nested
    brace, so a field default like `= {x: 1}` hid every field after it."""
    from app.graphql_diff import diff_graphql
    old = """type Q {
  a(f: I = {x: 1}): String
  keep: String
  gone: String
}"""
    new = """type Q {
  a(f: I = {x: 1}): String
  keep: String
}"""
    assert any(c.field_name == "gone" for c in diff_graphql(old, new)), \
        "field after a brace default was not detected"


def test_thrift_container_default_does_not_hide_later_fields():
    from app.thrift_diff import diff_thrift
    old = """struct U {
  1: map<string,string> m = {},
  2: string keep,
  3: string gone,
}"""
    new = """struct U {
  1: map<string,string> m = {},
  2: string keep,
}"""
    assert any(c.field_name == "gone" for c in diff_thrift(old, new)), \
        "field after a container default was not detected"


def test_graphql_comment_only_change_is_not_breaking():
    from app.graphql_diff import diff_graphql
    old = "type U {\n  keep: String\n  # gone: String\n}"
    new = "type U {\n  keep: String\n}"
    assert diff_graphql(old, new) == [], "comment-only edit reported as breaking"


def test_thrift_comment_only_change_is_not_breaking():
    from app.thrift_diff import diff_thrift
    old = "struct U {\n  1: string keep,\n  // 2: string gone,\n}"
    new = "struct U {\n  1: string keep,\n}"
    assert diff_thrift(old, new) == [], "comment-only edit reported as breaking"


def test_smithy_comment_only_change_is_not_breaking():
    from app.smithy_diff import diff_smithy
    old = "structure U {\n  keep: String\n  // gone: String\n}"
    new = "structure U {\n  keep: String\n}"
    assert diff_smithy(old, new) == [], "comment-only edit reported as breaking"


def test_ported_engines_still_detect_real_removals():
    """Comment stripping must not suppress genuine breaking changes."""
    from app.graphql_diff import diff_graphql
    from app.thrift_diff import diff_thrift
    from app.smithy_diff import diff_smithy

    assert diff_graphql("type U {\n keep: String\n gone: String\n}",
                        "type U {\n keep: String\n}"), "graphql regression"
    assert diff_thrift("struct U {\n 1: string keep,\n 2: string gone,\n}",
                       "struct U {\n 1: string keep,\n}"), "thrift regression"
    assert diff_smithy("structure U {\n keep: String\n gone: String\n}",
                       "structure U {\n keep: String\n}"), "smithy regression"


def test_no_engine_uses_the_non_nesting_regex():
    """The [^}]* pattern must not come back in any diff engine.

    Uses AST rather than line matching so the docstrings that *explain* the
    old pattern are not mistaken for live code -- a line-based check flagged
    three explanatory comments as violations.

    Any standalone string EXPRESSION is treated as documentation. That is
    broader than "docstring" on purpose: proto_diff.py opens with
    `from __future__ import annotations`, so its module docstring is not
    body[0] and Python does not classify it as a docstring at all.
    """
    import ast
    import glob
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    offenders = []

    for path in glob.glob(os.path.join(root, "app", "*diff*.py")):
        tree = ast.parse(open(path).read())

        # Any bare string statement is prose, not a regex
        documentation = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Expr)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)):
                documentation.add(id(node.value))

        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in documentation
                    and "[^}]" in node.value):
                offenders.append(f"{os.path.basename(path)}:{node.lineno}")

    assert not offenders, f"non-nesting block regex in live code: {offenders}"


# ===================================================================
# CLASS 8: SHARED STATE -- dashboard must reflect real pipeline work
# ===================================================================

def test_dashboard_counters_reflect_pipeline_events():
    """dashboard.py kept its OWN _activity_log plus log_activity() and
    register_repo() that NOTHING ever called, so it could only render zeros
    while the pipeline opened real PRs. It also counted action names
    ('pr_created', 'breaking_change') the pipeline never emits."""
    import tempfile
    old_dir = os.environ.get("RIPPLE_DATA_DIR")
    os.environ["RIPPLE_DATA_DIR"] = tempfile.mkdtemp()
    try:
        import importlib
        from app import activity
        importlib.reload(activity)
        activity.reset()

        activity.record("breaking_changes_detected",
                        {"spec": "user.proto", "count": 1})
        activity.record("pr_result", {"repo": "o/auth-service",
                                      "url": "https://github.com/o/auth-service/pull/3"})
        activity.record("pr_result", {"repo": "o/billing-api",
                                      "url": "https://github.com/o/billing-api/pull/3"})
        activity.record("residual_refs_flagged", {"repo": "o/notifications", "count": 2})

        c = activity.counters()
        assert c["breaks_detected"] == 1, c
        assert c["prs_created"] == 2, c
        assert c["partial_fixes"] == 1, c
        assert c["repos_monitored"] >= 3, c
    finally:
        if old_dir is None:
            os.environ.pop("RIPPLE_DATA_DIR", None)
        else:
            os.environ["RIPPLE_DATA_DIR"] = old_dir


def test_failed_prs_are_not_counted_as_created():
    import tempfile
    old_dir = os.environ.get("RIPPLE_DATA_DIR")
    os.environ["RIPPLE_DATA_DIR"] = tempfile.mkdtemp()
    try:
        import importlib
        from app import activity
        importlib.reload(activity)
        activity.reset()
        activity.record("pr_result", {"repo": "o/x", "url": "FAILED"})
        activity.record("pr_result", {"repo": "o/y", "url": ""})
        assert activity.counters()["prs_created"] == 0, "FAILED counted as created"
    finally:
        if old_dir is None:
            os.environ.pop("RIPPLE_DATA_DIR", None)
        else:
            os.environ["RIPPLE_DATA_DIR"] = old_dir


def test_activity_survives_process_restart():
    """An in-memory-only log resets on every Railway redeploy -- which is what
    erased the successful 08:49 run before it could be inspected.

    Uses a REAL SUBPROCESS, not importlib.reload(). A reload re-executes module
    top-level code inside a process that already has the data loaded, so
    module-level state can survive in ways a genuine restart would not -- the
    previous version of this test could have passed on an in-memory store.
    Writing in one interpreter and reading in another is the only shape that
    actually proves persistence.

    This proves the CODE persists. It does NOT prove the deployed volume
    survives a redeploy -- that needs the live service, see
    tools/verify_durability.py."""
    import json
    import subprocess
    import sys
    import tempfile

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with tempfile.TemporaryDirectory() as data_dir:
        env = {**os.environ, "RIPPLE_DATA_DIR": data_dir, "PYTHONPATH": root}

        writer = (
            "from app import activity\n"
            "activity.reset()\n"
            "activity.record('pr_result', {'repo': 'o/x',\n"
            "    'url': 'https://github.com/o/x/pull/1'})\n"
        )
        r = subprocess.run([sys.executable, "-c", writer], env=env,
                           capture_output=True, text=True, cwd=root)
        assert r.returncode == 0, f"writer failed: {r.stderr[-400:]}"

        # A DIFFERENT interpreter reads it back. No shared memory of any kind.
        reader = (
            "import json\n"
            "from app import activity\n"
            "print(json.dumps({'prs': activity.counters()['prs_created'],\n"
            "                  'events': len(activity.all_events())}))\n"
        )
        r = subprocess.run([sys.executable, "-c", reader], env=env,
                           capture_output=True, text=True, cwd=root)
        assert r.returncode == 0, f"reader failed: {r.stderr[-400:]}"
        got = json.loads(r.stdout.strip().splitlines()[-1])
        assert got["prs"] == 1, (
            f"activity did not survive a real process restart: {got}")
        assert got["events"] >= 1, got


def test_dashboard_has_no_duplicate_activity_store():
    """Two independent _activity_log lists were the root cause. Guard
    against a second one reappearing."""
    import os as _os
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    src = open(_os.path.join(root, "app", "dashboard.py")).read()
    assert "_activity_log: list" not in src, \
        "dashboard.py declared its own activity store again"
    assert "_installed_repos: list" not in src, \
        "dashboard.py declared its own repo list again"


def test_fix_generated_is_logged_exactly_once():
    """fix_generated was emitted both in the fix loop AND inside
    _generate_fix_with_rag_fallback, so every fix logged twice
    (handler.go x2, UserClient.ts x2, ...) and the dashboard's
    fixes_generated counter read double the real number."""
    import os as _os
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    src = open(_os.path.join(root, "app", "webhook.py")).read()
    count = src.count('_log_activity("fix_generated"')
    assert count == 1, f"fix_generated logged from {count} sites, expected 1"


# ===================================================================
# CLASS 9: RAG -- the subsystem that had never once executed
# ===================================================================

def test_rag_retriever_imports():
    """rag_retriever imported `from app.rag_store import ...` and that module
    was NEVER WRITTEN, so ~1000 lines of retrieval could not load at all.
    Every fix silently fell through to templates, hidden by
    `except Exception: pass` around the RAG call."""
    from app.rag_retriever import generate_fix_rag, retrieve_fix_pattern
    from app.rag_store import rag_store, FixPattern, StructuredPattern
    assert callable(generate_fix_rag)
    assert hasattr(rag_store, "patterns")
    assert hasattr(rag_store, "structured_patterns")
    assert hasattr(rag_store, "save")


def test_generate_fix_rag_accepts_the_webhook_call_shape():
    """webhook.py calls generate_fix_rag(code=, file_path=, change_type=,
    change_description=, store=) but the signature was
    (change_type, language, field_name, consumer_code, repo) -- a TypeError
    on the first real invocation even once the module existed."""
    from app.rag_retriever import generate_fix_rag
    go = "type R struct {\n\tName string\n\tPhoneNumber string\n}"
    result = generate_fix_rag(
        code=go, file_path="handler.go", field_name="phone_number",
        change_type="field_removed", change_description="removed phone_number",
    )
    assert result.fixed_code != go, "no fix produced"
    assert "PhoneNumber" not in result.fixed_code


def test_rag_exact_match_used_when_a_pattern_exists():
    import tempfile
    import time as _t
    old = os.environ.get("RIPPLE_DATA_DIR")
    os.environ["RIPPLE_DATA_DIR"] = tempfile.mkdtemp()
    try:
        import importlib
        from app import rag_store as rs
        importlib.reload(rs)
        from app import rag_retriever as rr
        importlib.reload(rr)

        pid = rs.PatternStore.make_pattern_id("field_removed", "go", "phone_number")
        rs.rag_store.add_pattern(rs.FixPattern(
            pattern_id=pid, change_type="field_removed", language="go",
            field_name="phone_number", strategy="drop struct field",
            source_file="handler.go", merge_count=7, reject_count=1,
            last_used=_t.time(),
        ))
        rs.rag_store._rebuild_clusters()

        go = "type R struct {\n\tName string\n\tPhoneNumber string\n}"
        result = rr.generate_fix_rag(
            code=go, file_path="handler.go",
            field_name="phone_number", change_type="field_removed",
        )
        assert result.source_type == "rag_exact", \
            f"expected rag_exact, got {result.source_type}"
        assert result.pattern_id == pid
    finally:
        if old is None:
            os.environ.pop("RIPPLE_DATA_DIR", None)
        else:
            os.environ["RIPPLE_DATA_DIR"] = old


def test_apply_fix_template_called_with_the_real_signature():
    """rag_retriever passed source_code=/new_field_name= (actual params are
    code=/new_name=) and treated the (code, explanation) tuple as a string.

    Inspects the AST rather than the raw text: a substring check flagged the
    comment that documents the old kwargs as a violation.
    """
    import ast
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tree = ast.parse(open(os.path.join(root, "app", "rag_retriever.py")).read())

    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
        if name != "apply_fix_template":
            continue
        for kw in node.keywords:
            if kw.arg in ("source_code", "new_field_name"):
                bad.append(f"line {node.lineno}: {kw.arg}=")
    assert not bad, f"stale kwargs in apply_fix_template call(s): {bad}"


def test_package_vector_is_reachable_from_a_real_push():
    """The package vector must be REACHABLE, not merely implemented.

    It was previously built, unit-tested and CI-gated while being unreachable
    from production: no change_type emitted a package deletion, and the webhook
    never called the dispatcher. A capability that cannot fire is worse than an
    absent one, because the tests say it works.

    This test walks the real path -- push payload -> _find_removed_specs ->
    _process_spec_deletion -> package vector -> template -> PR -- stubbing only
    the two IO boundaries (token, network). If anyone unwires the routing, the
    unit tests will still pass and THIS one will fail.
    """
    import asyncio
    import app.webhook as w
    from app.change_types import vector_for

    assert vector_for("package_removed") == "package"
    assert vector_for("field_removed") == "symbol", \
        "symbol routing must be unaffected"

    orig_token, orig_repos = w._get_token, w._find_consumer_repos
    orig_scan, orig_pr = w._scan_repo_tree_for_consumers, w._create_fix_pr
    consumer_src = 'import "api/v1/user.proto"\n\nfunc main(){ c := userpb.New() }\n'
    seen = []
    try:
        w._get_token = lambda iid=None: "stub"
        w._find_consumer_repos = lambda src, tok, iid=None: ["acme/consumer"]
        w._scan_repo_tree_for_consumers = (
            lambda repo, change, token, exclude_path="", budget=None:
            [("svc/handler.go", consumer_src, 0.9)])
        w._create_fix_pr = lambda repo, fp, fixed, change, src, tok, **kw: (
            seen.append((repo, fp, fixed, kw.get("sources"))) or "https://x/pull/1")

        payload = {
            "repository": {"full_name": "acme/contracts"},
            "installation": {"id": 1},
            "commits": [{"removed": ["api/v1/user.proto", "api/v1/order.proto"],
                         "modified": [], "added": []}],
        }
        dels = w._find_removed_specs(payload)
        assert len(dels) == 1 and dels[0]["change_type"] == "package_removed"

        res = asyncio.new_event_loop().run_until_complete(
            w._process_spec_deletion("acme/contracts", dels[0], installation_id=1))
    finally:
        w._get_token, w._find_consumer_repos = orig_token, orig_repos
        w._scan_repo_tree_for_consumers, w._create_fix_pr = orig_scan, orig_pr

    assert res["vector"] == "package", res
    assert res["prs"], "no PR opened -- the vector is not reachable"
    assert len(seen) == 1, seen
    _, _, fixed, sources = seen[0]
    assert "RIPPLE-ACTION-REQUIRED" in fixed, "fix was not marked"
    assert 'import "api/v1/user.proto"' in fixed, \
        "the import must survive -- removing it leaves every usage undefined"
    assert any("package" in str(x) for x in (sources or [])), sources


def test_deleted_specs_are_detected_at_the_event_layer():
    """`git rm api/user.proto` used to produce NOTHING.

    _find_changed_specs reads only `modified` and `added` from the push payload.
    Nothing read `removed`, so deleting a contract outright -- the most severe
    change a producer can make, since every symbol it declared disappears at
    once -- was not detected at all. Not detected-but-unfixable: invisible.

    No diff engine can supply this. Each has the shape
    diff_x(old_content, new_content, file_path) and never sees more than one
    file, so a file that ceased to exist is outside what any of them observe.
    """
    from app.webhook import _find_removed_specs
    from app.change_types import canonical_op, category

    # A single deleted contract.
    one = _find_removed_specs(
        {"commits": [{"removed": ["api/user.proto"], "modified": [], "added": []}]})
    assert len(one) == 1, one
    assert one[0]["change_type"] == "spec_removed"
    assert canonical_op("spec_removed") == "remove_package"
    assert category("spec_removed") == "judgment", \
        "a deleted contract cannot be fixed mechanically -- the replacement is " \
        "a product decision"

    # Several from one directory collapse into ONE package deletion, because
    # consumers reference the package path rather than each file.
    many = _find_removed_specs({"commits": [{"removed": [
        "api/v1/user.proto", "api/v1/order.proto", "api/v1/payment.proto",
        "README.md",
    ], "modified": [], "added": []}]})
    assert len(many) == 1, f"expected one package deletion, got {many}"
    assert many[0]["change_type"] == "package_removed"
    assert many[0]["path"] == "api/v1/"
    assert many[0]["count"] == 3, "non-spec files must not be counted"

    # Non-spec deletions are not breaking changes.
    assert _find_removed_specs(
        {"commits": [{"removed": ["README.md", ".gitignore"]}]}) == []

    # Separate directories stay separate.
    mixed = _find_removed_specs({"commits": [{"removed": [
        "api/a.proto", "api/b.proto", "other/c.proto"]}]})
    kinds = sorted(m["change_type"] for m in mixed)
    assert kinds == ["package_removed", "spec_removed"], mixed


def test_package_deletion_opens_a_marked_pr_not_silence():
    """A judgment change must produce a NON-EMPTY marked diff.

    fixed_code == content opens no PR, so returning the file unchanged would
    turn a detected deletion straight back into silence. Nothing may be edited
    or removed either: dropping the import leaves every usage undefined, and
    dropping the usages silently deletes behaviour.
    """
    from app.fix_templates import apply_fix_template

    importer = 'import "api/v1/user.proto"\n\nfunc main(){ c := userpb.New() }\n'
    out, expl = apply_fix_template(importer, "go", "package_removed", "api/v1")
    assert out != importer, "unchanged code opens no PR"
    assert "RIPPLE-ACTION-REQUIRED" in out
    assert "PARTIAL" in expl
    # The import must survive -- removing it is what breaks the build.
    assert 'import "api/v1/user.proto"' in out

    # A member of the deleted directory names nothing, but must still be marked.
    member = "package group\n\nfunc mustRunAs() error { return nil }\n"
    out2, _ = apply_fix_template(member, "go", "package_removed",
                                 "pkg/security/podsecuritypolicy")
    assert out2 != member and "RIPPLE-ACTION-REQUIRED" in out2


def test_llm_backend_is_overridable_and_labelled_honestly():
    """One place decides which LLM answers, and the PR says which one did.

    Three call sites each had their own idea of how to reach a model:
    fix_generator and validated_fix pinned the model, and natural_language pinned
    the URL too -- so an ANTHROPIC_BASE_URL override reached two of three sites
    and the third silently kept calling api.anthropic.com.

    The labelling half matters as much. ANTHROPIC_BASE_URL can front Gemini via a
    LiteLLM proxy, or Ollama. "LLM-generated (semantic)" would then be true but
    imply Claude, which is the same defect as the "Learning: enabled" footer that
    shipped on every live PR.
    """
    import importlib
    import os
    import app.llm_config as L

    saved = {k: os.environ.get(k) for k in
             ("ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
              "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")}
    try:
        for k in saved:
            os.environ.pop(k, None)

        # Default: FREE and SELF-HOSTED. This asserted api.anthropic.com and
        # is_anthropic() is True until 2026-09-13, pinning a default that FAILED OPEN
        # to a metered third-party API. Reversed deliberately -- see llm_config's
        # "FREE BY DEFAULT, PAID BY OPT-IN".
        importlib.reload(L)
        assert L.base_url() == "http://localhost:11434", (
            f"unconfigured install resolves to {L.base_url()} -- the default must be a "
            f"free self-hosted endpoint, never a paid third-party API")
        assert L.is_anthropic() is False
        assert L.is_paid_backend() is False, "the default backend bills per token"
        assert "Anthropic" not in L.backend_label()
        # And it must not CLAIM a backend when none is reachable.
        assert L.is_configured() is False, "default-off broke: something is configured"
        assert L.backend_label() == "no model (deterministic only)", (
            f"unconfigured install labels its provenance {L.backend_label()!r}, which "
            f"would render into a PR body as a model that never answered")

        # Overridden: a proxy fronting something else.
        os.environ["ANTHROPIC_BASE_URL"] = "http://localhost:4000"
        os.environ["ANTHROPIC_MODEL"] = "gemini-2.5-flash"
        os.environ["ANTHROPIC_AUTH_TOKEN"] = "DUMMY"
        importlib.reload(L)
        assert L.base_url() == "http://localhost:4000"
        assert L.model() == "gemini-2.5-flash"
        assert L.is_anthropic() is False, "must not claim Anthropic when proxied"
        assert L.messages_url() == "http://localhost:4000/v1/messages"
        assert L.api_key() == "DUMMY", "AUTH_TOKEN must be honoured"

        label = L.backend_label()
        assert "gemini-2.5-flash" in label and "localhost:4000" in label, label
        assert "Anthropic" not in label, \
            f"label claims Anthropic while proxied to something else: {label}"

        # And the PR body must carry it, not a generic '(semantic)'.
        import app.confidence as C
        importlib.reload(C)
        body = C.format_pr_body(
            change_description="removed field x", source_repo="acme/spec",
            confidence=0.8, sources=["llm"], reasons=[], consumer_file="a.go")
        assert "gemini-2.5-flash" in body, "PR body hides the real backend"
        assert "LLM-generated (semantic)" not in body
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(L)
        import app.confidence as C
        importlib.reload(C)


def test_config_languages_are_matched_not_skipped():
    """YAML and shell files were skipped entirely for having no matcher.

    Measured on the PropBench replay: 137 files that a real merged PR had to
    change were excluded on language alone -- 24 of 36 on kubernetes#109798,
    i.e. most of that change. Adding these matchers moved 41 files from
    "excluded" to "scored" and flagged 26 of them.

    Two rules that differ from the code matchers, both load-bearing:
      1. A quoted value IS a reference. `name: "podsecuritypolicy"` means what
         the unquoted form means, so the string-literal demotion to 0.3 (below
         min_confidence) must not apply.
      2. Matching is case-insensitive. generate_variants('podsecuritypolicy')
         cannot produce 'PodSecurityPolicy' -- splitting an unseparated
         lowercase compound needs a dictionary -- so `kind: PodSecurityPolicy`
         fell to the 0.70 fallback until the classifiers ignored case.
    """
    from app.smart_consumer_finder import find_field_consumers
    from app.rag_engine import _detect_language

    assert _detect_language("a/b.yaml") == "yaml"
    assert _detect_language("a/b.yml") == "yaml"
    assert _detect_language("hack/local-up-cluster.sh") == "shell"

    # Case differs from the symbol, and the value is what carries the reference.
    ms = find_field_consumers("kind: PodSecurityPolicy\n", "psp.yaml",
                              "podsecuritypolicy", "yaml")
    assert ms, "case-insensitive config match regressed"
    assert ms[0].confidence >= 0.90, \
        f"expected a specific classification, got {ms[0].match_type} " \
        f"{ms[0].confidence} -- the 0.70 fallback means no pattern fired"

    # A quoted value must not be demoted as a string literal.
    q = find_field_consumers('  name: "podsecuritypolicy"\n', "psp.yaml",
                             "podsecuritypolicy", "yaml")
    assert q and q[0].confidence >= 0.5, \
        "quoted YAML value demoted below min_confidence"

    # Shell variables and path arguments.
    sh = find_field_consumers('kubectl create -f podsecuritypolicy/psp.yaml\n',
                              "up.sh", "podsecuritypolicy", "shell")
    assert sh and sh[0].match_type.startswith("shell_")

    # Comments are still excluded, as for every other language.
    assert find_field_consumers("# see podsecuritypolicy for history\n",
                                "x.sh", "podsecuritypolicy", "shell") == []


def test_package_vector_finds_what_symbol_search_cannot():
    """A deleted PACKAGE propagates by path, not by name.

    Measured on kubernetes#109798 ("Remove PodSecurityPolicy admission plugin"):
    31 of the 32 files Ripple failed to flag lived under
    pkg/security/podsecuritypolicy/ and named no shared identifier. They had to
    change because their PACKAGE was deleted. A symbol matcher is structurally
    blind to that, however good its variant generation is.
    """
    from app.smart_consumer_finder import (
        find_package_consumers, find_field_consumers, find_matches_in_file,
    )
    PKG = "pkg/security/podsecuritypolicy/"

    # A package member naming nothing in common with the package.
    member_path = "pkg/security/podsecuritypolicy/group/mustrunas.go"
    member_src = "package group\n\nfunc mustRunAs(x int) error { return nil }\n"

    assert find_field_consumers(member_src, member_path, "podsecuritypolicy", "go") == [], \
        "symbol search should find nothing here -- that is the gap being closed"

    ms = find_package_consumers(member_src, member_path, PKG, "go")
    assert len(ms) == 1 and ms[0].match_type == "package_member", \
        f"membership not detected: {ms}"
    assert ms[0].confidence >= 0.95

    # An importer outside the package. Go puts the path on its own line with no
    # keyword, which an import regex anchored on 'import' would miss.
    imp_src = 'import (\n\t"k8s.io/kubernetes/pkg/security/podsecuritypolicy"\n)\n'
    ims = find_package_consumers(imp_src, "plugin/pkg/admission/a.go", PKG, "go")
    assert any(m.match_type == "package_import" for m in ims), \
        f"bare quoted import path not classified as an import: " \
        f"{[(m.match_type, m.confidence) for m in ims]}"

    # A file with no relationship must return nothing, so callers can tell
    # "not a consumer" from "consumer by path".
    assert find_package_consumers(
        "package other\nfunc Validate() {}\n", "pkg/other/t.go", PKG, "go") == []

    # Comments must not count -- same rule the symbol matcher follows.
    assert find_package_consumers(
        "// see pkg/security/podsecuritypolicy for history\n",
        "x.go", PKG, "go") == []

    # Signals stay distinguishable: package matches are prefixed, symbol ones
    # are not, so mixed output remains attributable.
    assert all(m.match_type.startswith("package_") for m in ms + ims)


def test_symbol_matcher_behaviour_is_unchanged_by_path_signal():
    """The path signal is additive. The symbol path feeds the live webhook and
    must stay bit-identical, so it is a separate function, not a new branch
    inside find_field_consumers."""
    from app.smart_consumer_finder import find_field_consumers, find_matches_in_file

    src = (
        "func send(u User) error {\n"
        "\tto := u.PhoneNumber\n"
        "\treturn notify(to)\n"
        "}\n"
    )
    direct = find_field_consumers(src, "h.go", "phone_number", "go")
    assert direct, "symbol matcher regressed -- PhoneNumber no longer found"
    assert all(not m.match_type.startswith("package_") for m in direct)

    # The dispatcher must delegate identically, not re-implement.
    viad = find_matches_in_file(src, "h.go", "phone_number", "go", vector="symbol")
    assert [(m.line_number, m.match_type, m.confidence) for m in viad] == \
           [(m.line_number, m.match_type, m.confidence) for m in direct], \
        "find_matches_in_file(vector='symbol') diverged from find_field_consumers"


def test_propbench_indexer_reads_the_real_schema():
    """PropBench entries use `files:` (a LIST); the indexer read `file` (a str).

    That key appears ZERO times across 881 real entries, so every record
    produced an empty path and language detection resolved to 'unknown'
    throughout -- the indexer had never actually read the corpus.

    Also pins the honest half: PropBench carries no diffs, so entries must
    still be reported via entries_without_diff and rejected downstream. A
    retrievable pattern that cannot produce a fix is worse than no pattern,
    because it can win retrieval and then return nothing.
    """
    import tempfile
    from pathlib import Path as _Path
    from app.rag_engine import index_from_propbench, RagStore as _EngineStore
    from app.rag_store import PatternStore

    entry = """
id: fixture-001
source_repo: acme/widgets
trigger:
  package: widgets
  files:
  - api/user.proto
  intent: Removed phone_number from User
  diff_summary: 'Primary change: api/user.proto (+0/-1)'
consequences:
- package: consumer
  files:
  - src/handler.go
  - src/client.ts
  description: 'Co-changed'
  mechanical: true
  relationship: co-change
"""
    with tempfile.TemporaryDirectory() as td:
        (_Path(td) / "e.yaml").write_text(entry)
        store = _EngineStore(collection_name="test_pb_schema")
        stats = index_from_propbench(td, store)

        assert stats["entries_loaded"] == 1, f"entry not read: {stats}"
        # Both consequence files must yield an example -- reading a scalar key
        # dropped them entirely.
        assert stats["examples_stored"] == 2, \
            f"expected one example per consequence file, got {stats}"
        assert stats["entries_without_diff"] == 1, \
            "an entry with no diff must be counted, not silently passed through"
        assert stats["parse_errors"] == 0

        ex = store.all_examples()
        assert all(e.fix_file for e in ex), "fix_file empty -- 'files' list not read"
        assert all(e.trigger_file == "api/user.proto" for e in ex), \
            "trigger_file empty -- trigger 'files' list not read"
        assert {e.language for e in ex} == {"go", "typescript"}, \
            f"language detection needs a real path, got {[e.language for e in ex]}"
        assert all(e.trigger_description == "Removed phone_number from User" for e in ex), \
            "trigger description should come from 'intent'"
        # diff_summary is a summary, NOT a diff -- passing it off as one would
        # make an unusable example look complete.
        assert all(not e.trigger_diff for e in ex), \
            "diff_summary must not be presented as trigger_diff"

        # And the honest outcome: no diffs means no usable fix patterns.
        ps = PatternStore(collection_name="test_pb_schema_patterns")
        res = ps.ingest_examples(ex)
        assert res["added"] == 0 and ps.count() == 0, \
            f"diff-less entries must not become fix patterns: {res}"


def test_pr_body_makes_no_unearned_learning_claims():
    """The PR body must not claim learning that has not happened.

    Every PR previously carried "Learning: enabled" and "PropBench v1 (882
    entries)" in its footer, plus "Similar fixes merged without revert" and
    "Co-change pattern detected in git history" in the confidence table. None
    was true: propbench_data/ is not vendored into this repo, no learning
    channel runs in the hosted deployment, and no prior merge is tracked.

    A PR that overstates what it did costs more trust than one admitting a
    partial fix, so these strings are pinned absent.
    """
    from app.confidence import format_pr_body

    body = format_pr_body(
        change_description="removed field phone_number",
        source_repo="acme/user-proto",
        confidence=0.95,
        sources=["template"],
        reasons=[],
        consumer_file="handler.go",
    )

    forbidden = [
        "Learning: enabled",
        "PropBench v1 (882 entries)",
        "Similar fixes merged without revert",
        "Co-change pattern detected in git history",
    ]
    for claim in forbidden:
        assert claim not in body, f"unearned claim back in PR body: {claim!r}"

    # And the honest replacements must actually be present, so this test fails
    # if the rows are deleted rather than corrected.
    assert "not a measurement of past merges" in body, \
        "historical-accuracy row must state it is a prior, not a measurement"
    assert "Static reference match in consumer source" in body, \
        "observation row must describe what actually matched"


def test_rag_fallback_to_template_is_not_labelled_as_rag():
    """RAG's own chain returns '[RAG/template]' when it finds no learned
    pattern. Checking for '[RAG' before 'template' would claim
    learned-pattern provenance for a purely deterministic transform -- the
    same false-provenance bug as the earlier 'LLM-generated' mislabel."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "app", "webhook.py")).read()
    idx_tmpl = src.find('if "template" in explanation.lower()')
    idx_rag = src.find('elif "[RAG" in explanation')
    assert idx_tmpl != -1 and idx_rag != -1, "provenance detection not found"
    assert idx_tmpl < idx_rag, "template must be checked before [RAG"


# ===================================================================
# CLASS 10: CHANGE TYPE COVERAGE -- detection must not outrun fixing
# ===================================================================

def test_every_emitted_change_type_is_classified():
    """An unclassified change_type reaches fix_templates as 'Unknown
    change_type', leaves the code unchanged, and therefore opens NO PR --
    Ripple detects a breaking change and silently does nothing."""
    from app.change_types import canonical_op
    import ast as _ast
    import glob as _glob

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    emitted = set()
    paths = sorted(_glob.glob(os.path.join(root, "app", "*diff*.py")))
    paths.append(os.path.join(root, "app", "diff_engine.py"))
    for path in paths:
        if not os.path.exists(path):
            continue
        for node in _ast.walk(_ast.parse(open(path).read())):
            if not isinstance(node, _ast.Call):
                continue
            for kw in node.keywords:
                if (kw.arg == "change_type"
                        and isinstance(kw.value, _ast.Constant)
                        and isinstance(kw.value.value, str)):
                    emitted.add(kw.value.value)
            if getattr(node.func, "id", "") == "_bc" and node.args:
                a = node.args[0]
                if isinstance(a, _ast.Constant) and isinstance(a.value, str):
                    emitted.add(a.value)

    unclassified = [ct for ct in sorted(emitted) if not canonical_op(ct)]
    assert not unclassified, f"unclassified change types: {unclassified}"
    assert len(emitted) >= 40, f"expected ~47 emitted types, found {len(emitted)}"


def test_no_change_type_returns_unknown_from_templates():
    """Every emitted type must reach a handler in fix_templates."""
    from app.change_types import CHANGE_TYPE_MAP
    from app.fix_templates import apply_fix_template

    escapes = []
    for ct in sorted(CHANGE_TYPE_MAP):
        for lang in ("go", "typescript", "python", "java"):
            _, expl = apply_fix_template(
                "x = 1", lang, ct, "Thing",
                new_name="Other", old_type="A", new_type="B",
            )
            if "Unknown change_type" in expl or "Unclassified" in expl:
                escapes.append(f"{ct}/{lang}")
    assert not escapes, f"unknown-type escapes: {escapes[:8]}"


def test_judgment_types_produce_a_non_empty_marked_diff():
    """A judgment type that returns the code unchanged opens no PR, which is
    the silence this work exists to remove. Each must produce a marked diff."""
    from app.change_types import CHANGE_TYPE_MAP, category
    from app.fix_templates import apply_fix_template, MARKER

    code = "func f(c *Client) error {\n\tr, err := c.svc.DeleteUser(ctx)\n\treturn err\n}"
    empty = []
    for ct in sorted(CHANGE_TYPE_MAP):
        if category(ct) != "judgment":
            continue
        fixed, _ = apply_fix_template(code, "go", ct, "DeleteUser")
        if fixed == code or MARKER not in fixed:
            empty.append(ct)
    assert not empty, f"judgment types producing no marked diff: {empty}"


def test_add_required_never_invents_a_value():
    """Guessing a required field's value is a silent behaviour change and the
    most likely way to ship a confidently wrong fix."""
    from app.fix_templates import apply_fix_template, MARKER

    py = "def create(name, email):\n    return User(name=name, email=email)"
    fixed, expl = apply_fix_template(py, "python", "required_field_added", "country")
    body = "\n".join(l for l in fixed.splitlines() if MARKER not in l)
    assert "country=" not in body, "invented a value for the new required field"
    assert "country" not in body, "leaked the field name into code"
    import ast as _ast
    _ast.parse(fixed)  # annotation must not break the file


def test_remove_operation_comments_out_rather_than_deletes():
    """Deleting the call would hide that functionality was dropped; the
    original line must stay visible in the diff."""
    from app.fix_templates import apply_fix_template, MARKER

    go = "func f(c *Client) error {\n\tr, err := c.svc.DeleteUser(ctx)\n\treturn err\n}"
    fixed, expl = apply_fix_template(go, "go", "rpc_removed", "DeleteUser")
    assert MARKER in fixed
    assert "// \tr, err := c.svc.DeleteUser(ctx)" in fixed or \
           "// r, err := c.svc.DeleteUser(ctx)" in fixed, \
           "original call line was deleted instead of commented"
    # Must NOT claim the file compiles -- commenting an assignment can leave
    # dependents referencing undefined variables.
    assert "so the file compiles" not in expl, "false compile claim in explanation"
    assert "may still not compile" in expl, "missing the honest caveat"


def test_case_arm_removal_does_not_orphan_the_body():
    """Removing only 'case X:' leaves its statements dangling in the switch,
    which does not compile."""
    from app.fix_templates import apply_fix_template

    go = ("switch s {\ncase userpb.Status_LEGACY:\n\treturn 1\n"
          "case userpb.Status_ACTIVE:\n\treturn 2\n}")
    fixed, _ = apply_fix_template(go, "go", "enum_value_removed", "LEGACY")
    assert "LEGACY" not in fixed
    assert "return 1" not in fixed, "arm body was orphaned"
    assert "Status_ACTIVE" in fixed and "return 2" in fixed, "wrong arm removed"


def test_enum_arm_matching_treats_underscore_as_a_boundary():
    """Go protobuf enums render as Status_LEGACY, and \\bLEGACY\\b does NOT
    match there because '_' is a word character -- the same underscore trap
    that made consumer confidence non-deterministic."""
    from app.fix_templates import apply_fix_template

    go = "switch s {\ncase userpb.Status_LEGACY:\n\treturn 1\n}"
    fixed, _ = apply_fix_template(go, "go", "enum_value_removed", "LEGACY")
    assert "Status_LEGACY" not in fixed, "underscore-prefixed enum not matched"


def test_wire_only_returns_code_unchanged_and_says_so_explicitly():
    """A changed proto field number / thrift field id breaks the WIRE
    contract, not the source contract -- consumer code never references field
    numbers, so leaving the code unchanged is CORRECT, not a failure.

    The distinction matters because unchanged code means no PR opens, which
    otherwise looks identical to 'we could not fix it'.
    """
    from app.fix_templates import apply_fix_template

    code = 'type U struct {\n\tPhone string `protobuf:"bytes,4,opt"`\n}'
    for ct in ("field_number_changed", "field_id_changed"):
        for lang in ("go", "typescript", "python", "java"):
            fixed, expl = apply_fix_template(code, lang, ct, "phone_number")
            assert fixed == code, f"{ct}/{lang} modified source for a wire break"
            assert "NO SOURCE CHANGE REQUIRED" in expl, f"{ct}/{lang}: {expl[:60]}"
            # must not read as a failure or an unsupported type
            for bad in ("Unknown change_type", "Unclassified",
                        "No mechanical template", "Error:"):
                assert bad not in expl, f"{ct}/{lang} reads as failure: {expl[:70]}"


def test_wire_only_predicate_separates_the_three_categories():
    from app.change_types import is_wire_only, is_judgment, category

    assert is_wire_only("field_number_changed")
    assert is_wire_only("field_id_changed")
    assert not is_wire_only("field_removed")
    assert not is_wire_only("rpc_removed")

    assert is_judgment("rpc_removed")
    assert not is_judgment("field_number_changed")

    assert category("field_removed") == "mechanical"


def test_webhook_short_circuits_wire_only_before_consumer_search():
    """Searching every repo for consumers of a wire-only break would spend
    hundreds of API calls to reach a guaranteed no-op, and a run containing
    only wire breaks must not look like a run that found nothing."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "app", "webhook.py")).read()

    assert "if is_wire_only(change.change_type):" in src, \
        "wire-only changes are not short-circuited"
    assert '"wire_only_change"' in src, "wire-only breaks are not logged"
    assert '"wire_only_changes": wire_only_changes' in src, \
        "wire-only breaks are not reported in the result"

    # the short-circuit must come BEFORE the consumer search
    idx_guard = src.find("if is_wire_only(change.change_type):")
    idx_search = src.find("consumer_files = _search_repo_for_consumers")
    assert idx_guard < idx_search, \
        "wire-only guard runs after the consumer search, wasting API calls"


def test_rag_path_handles_every_change_type_without_crashing():
    """Stages 2-4 verified the direct template path. RAG wraps it with its own
    argument shuffling, so it needs its own coverage check."""
    import tempfile
    old = os.environ.get("RIPPLE_DATA_DIR")
    os.environ["RIPPLE_DATA_DIR"] = tempfile.mkdtemp()
    try:
        import importlib
        from app import rag_store as rs
        importlib.reload(rs)
        from app import rag_retriever as rr
        importlib.reload(rr)
        from app.change_types import CHANGE_TYPE_MAP

        go = "func f(c *C) error {\n\tr, err := c.DeleteUser(ctx)\n\treturn err\n}"
        bad = []
        for ct in sorted(CHANGE_TYPE_MAP):
            for lang in ("go", "python"):
                try:
                    res = rr.generate_fix_rag(
                        code=go, file_path="h.go", field_name="DeleteUser",
                        change_type=ct, language=lang,
                    )
                    if ("Unknown change_type" in res.explanation
                            or "Unclassified" in res.explanation):
                        bad.append(f"{ct}/{lang}")
                except Exception as e:
                    bad.append(f"{ct}/{lang}: {type(e).__name__}")
        assert not bad, f"RAG path failures: {bad[:8]}"
    finally:
        if old is None:
            os.environ.pop("RIPPLE_DATA_DIR", None)
        else:
            os.environ["RIPPLE_DATA_DIR"] = old


def test_ingest_skips_types_that_can_never_produce_a_fix():
    """rag_engine's diff heuristic emits 'field_added' (an optional add is not
    breaking) and 'modified' (unclassifiable). Storing those as fix patterns
    lets them win a retrieval score against a real change and then produce
    nothing."""
    import tempfile
    from dataclasses import dataclass

    old = os.environ.get("RIPPLE_DATA_DIR")
    os.environ["RIPPLE_DATA_DIR"] = tempfile.mkdtemp()
    try:
        import importlib
        from app import rag_store as rs
        importlib.reload(rs)

        @dataclass
        class Ex:
            change_type: str
            language: str = "go"
            field_name: str = "phone_number"
            fix_file: str = "h.go"
            repo_name: str = "o/r"
            added_at: float = 0.0

        store = rs.PatternStore("t")
        store.load()
        stats = store.ingest_examples([
            Ex("field_removed"),               # fixable
            Ex("rpc_removed"),                 # fixable (judgment)
            Ex("field_added"),                 # non-breaking -> skip
            Ex("field_number_changed"),        # wire-only -> skip
            Ex("modified"),                    # unclassified -> skip
        ])
        assert stats["added"] == 2, stats
        assert stats["skipped_unfixable"] == 3, stats
        assert stats["skipped_reasons"].get("wire_only") == 1, stats
        assert stats["skipped_reasons"].get("non_breaking") == 1, stats
        assert stats["skipped_reasons"].get("unclassified") == 1, stats
    finally:
        if old is None:
            os.environ.pop("RIPPLE_DATA_DIR", None)
        else:
            os.environ["RIPPLE_DATA_DIR"] = old


def test_all_webhook_paths_guard_wire_only():
    """The guard was initially only on the GitHub path, so GitLab and
    Bitbucket would search every consumer for a break that has no source fix
    -- hundreds of API calls to reach a guaranteed no-op."""
    import re as _re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    lines = open(os.path.join(root, "app", "webhook.py")).read().split("\n")

    loops = [i for i, l in enumerate(lines)
             if "for change in breaking_changes:" in l]
    assert len(loops) >= 3, f"expected 3 per-change loops, found {len(loops)}"

    unguarded = []
    for i in loops:
        window = "\n".join(lines[i:i + 16])
        if "is_wire_only" not in window:
            fn = "?"
            for j in range(i, 0, -1):
                m = _re.match(r"(?:async )?def (\w+)", lines[j])
                if m:
                    fn = m.group(1)
                    break
            unguarded.append(fn)
    assert not unguarded, f"platform paths missing the wire-only guard: {unguarded}"


def test_is_fixable_separates_all_four_categories():
    from app.change_types import is_fixable, category

    assert is_fixable("field_removed")          # mechanical
    assert is_fixable("rpc_removed")            # judgment
    assert not is_fixable("field_number_changed")   # wire_only
    assert not is_fixable("field_added")            # non_breaking
    assert not is_fixable("modified")               # unclassified
    assert category("field_added") == "non_breaking"


# ===================================================================
# runner (works without pytest)
# ===================================================================

import os.path as _os_path  # for the run-evidence file


def _main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed, failed = 0, []
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed.append((name, str(e) or "assertion failed"))
            print(f"  FAIL  {name}\n          {e}")
        except Exception as e:
            failed.append((name, f"{type(e).__name__}: {e}"))
            print(f"  ERROR {name}\n          {type(e).__name__}: {e}")

    print(f"\n  {passed}/{len(tests)} passed")
    if failed:
        print("\n  failures:")
        for name, msg in failed:
            print(f"    - {name}: {msg}")

    # Machine-readable RUN EVIDENCE for tools/audit_capabilities.py.
    #
    # The capability registry lets a cell claim e2e_tested by naming a test.
    # Checking that the name EXISTS is weak -- a test can exist and never run, or
    # be renamed while the claim keeps pointing at a stale name. This records
    # which tests actually executed and passed, so the audit can verify the
    # evidence rather than the reference.
    #
    # Written on failure too: the audit must be able to tell "the fixture failed"
    # apart from "the suite never ran", which are different states.
    try:
        import json as _json
        import time as _t
        _here = _os_path.dirname(_os_path.abspath(__file__))

        # WHICH REVISION THIS RESULT IS ABOUT.
        #
        # "122/122 passed" is not evidence about a deployment unless it names the
        # code it ran. tools/verify_release.py refuses to pass unless the DEPLOYED
        # sha equals the sha recorded here, which is the whole point: Ripple modifies
        # other people's code, so which commit produced a PR cannot be a guess.
        #
        # `dirty` is recorded rather than hidden. A dirty tree means the tested code
        # is not any commit, so it can never legitimately match a deployed sha, and
        # the release gate treats that as a refusal rather than a mismatch.
        _sha, _dirty = "", None
        try:
            import subprocess as _sp
            _repo = _os_path.dirname(_here)
            _r = _sp.run(["git", "-C", _repo, "rev-parse", "HEAD"],
                         capture_output=True, text=True, timeout=5)
            if _r.returncode == 0:
                _sha = _r.stdout.strip()
            _d = _sp.run(["git", "-C", _repo, "status", "--porcelain"],
                         capture_output=True, text=True, timeout=10)
            if _d.returncode == 0:
                _dirty = bool(_d.stdout.strip())
        except Exception:
            # Bookkeeping must never fail the suite. sha stays "" and dirty stays
            # None, which the release gate reads as "unknown" and REFUSES -- it does
            # not read a missing sha as a match.
            pass

        with open(_os_path.join(_here, ".last_run.json"), "w") as fh:
            _json.dump({
                "ran_at": _t.time(),
                "tested_sha": _sha or None,
                "tested_tree_dirty": _dirty,
                "total": len(tests),
                "passed": sorted(n for n, _ in tests
                                 if n not in {f for f, _ in failed}),
                "failed": sorted(f for f, _ in failed),
            }, fh, indent=2)
    except OSError:
        pass    # never let bookkeeping fail the suite

    return 1 if failed else 0


def test_every_llm_client_is_wrapped_by_the_diff_contract():
    """No LLM generator may be reachable except through generate_fix().

    WHY AN AST CHECK AND NOT A MONKEYPATCH
    Two existing gates verify the LLM path is contract-checked by monkeypatching
    `fix_generator._generate_with_llm` and asserting the result is rejected. Both are
    keyed on that one NAME. Measured consequence of adding a second client: a
    `_generate_with_openai` would be unpatched by either test, so the diff contract
    could fail to wrap it and both gates would stay green -- a gate passing while
    asserting nothing about the thing that was added. That is the defect shape this
    repo has now found roughly forty times, and here it would arrive four at once.

    So this asserts the STRUCTURE instead: every function named in
    llm_providers.FORMAT_CLIENTS must exist, and must be called from inside
    `generate_fix`, which is where diff_contract.check() lives. A generator called
    from anywhere else bypasses verification no matter what a monkeypatch proves.
    """
    import ast

    from app.llm_providers import FORMAT_CLIENTS

    assert FORMAT_CLIENTS, (
        "no wire format claims a client, so this check would pass over an empty set")

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    for fmt, (module, func) in sorted(FORMAT_CLIENTS.items()):
        path = os.path.join(root, "app", f"{module}.py")
        assert os.path.exists(path), (
            f"wire format {fmt!r} names app/{module}.py, which does not exist")
        tree = ast.parse(open(path, encoding="utf-8").read())
        defined = {n.name for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert func in defined, (
            f"wire format {fmt!r} claims client {module}.{func}, which is not "
            f"defined. Declaring a format without writing its client makes "
            f"llm_providers.usable_today() report a provider as reachable when "
            f"nothing can speak to it.")

        # The generator must be called from within generate_fix, because the diff
        # contract lives there. Anything else reaches the model unverified.
        wrapper = next((n for n in ast.walk(tree)
                        if isinstance(n, ast.FunctionDef)
                        and n.name == "generate_fix"), None)
        assert wrapper is not None, (
            f"app/{module}.py defines no generate_fix, so there is nowhere for the "
            f"diff contract to sit")
        called = {n.func.id for n in ast.walk(wrapper)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert func in called, (
            f"{module}.{func} is NOT called from generate_fix. The diff contract "
            f"that verifies an LLM patch lives in generate_fix; a generator invoked "
            f"anywhere else returns unverified model output straight to the caller.")

        # And generate_fix must actually consult the contract, or wrapping is empty.
        wrapper_src = ast.dump(wrapper)
        assert "diff_contract" in wrapper_src or "_diff_check" in wrapper_src, (
            f"generate_fix does not reference the diff contract, so wrapping "
            f"{func} in it verifies nothing")


def _with_provider_credentials():
    """Give every registry provider a credential, and restore the environment after.

    The chains are CREDENTIAL-FILTERED -- a provider with no key is excluded, because
    including it makes the chain three names over one endpoint (measured: a local
    Ollama reply was reported as `answered by openrouter`). That is correct behaviour
    and it makes chain composition depend on ambient environment, so a test that reads
    the environment asserts whatever the developer's shell happens to hold. Both of
    the tests below failed on a bare environment for exactly that reason.
    """
    import contextlib
    import os as _os

    from app.llm_providers import PROVIDERS

    @contextlib.contextmanager
    def _ctx():
        names = {p.key_env for p in PROVIDERS.values() if p.key_env}
        saved = {n: _os.environ.get(n) for n in names}
        try:
            for n in names:
                _os.environ[n] = "test-credential"
            yield
        finally:
            for n, v in saved.items():
                if v is None:
                    _os.environ.pop(n, None)
                else:
                    _os.environ[n] = v
    return _ctx()


def test_model_prose_that_overclaims_is_rejected_not_sanitised():
    """A model asked not to overclaim will sometimes overclaim anyway.

    The prompt says "do not claim anything was verified". That is a REQUEST.
    FORBIDDEN_CLAIMS is the enforcement, and rejection rather than sanitisation is
    deliberate: stripping the offending phrase leaves prose built around a claim it no
    longer makes, and hides that the model overclaimed at all. An invisible failure is
    the shape of every defect in this codebase's history.
    """
    from app import pr_prose

    facts = {"change_type": "removed_field", "field_name": "phone_number",
             "language": "typescript", "consumer_file": "src/contact.ts",
             "edits": [{"shape": "object-literal property"}],
             "refusals": ["line 9: function parameter -- breaks every caller"]}

    class _R:
        def __init__(self, text):
            self.text, self.ok, self.attempts = text, True, ()
            self.answered_by = "test-model"

        def stated_outcome(self):
            return "answered by test-model"

    # Each forbidden phrase must actually cause a rejection.
    assert pr_prose.FORBIDDEN_CLAIMS, "no claims are forbidden -- vacuous"
    for phrase in sorted(pr_prose.FORBIDDEN_CLAIMS):
        res = pr_prose.render(
            facts, call_chain=lambda p, _t=phrase: _R(
                f"This change matters. I {_t} the result before opening this."))
        assert res.source == "template", (
            f"prose containing {phrase!r} was accepted as model-authored")
        assert phrase in res.reason, (
            f"the rejection does not name the offending claim: {res.reason}")
        assert phrase not in res.text.lower(), (
            f"the rejected claim survived into the body: {res.text}")

    # Clean prose IS used, or the check is just a way of never using the model.
    clean = ("The upstream contract dropped this field, so the call here now sends "
             "something the API will ignore. Start at the changed lines, then decide "
             "what to do about the parameter that was left alone.")
    ok = pr_prose.render(facts, call_chain=lambda p: _R(clean))
    assert ok.source == "model", (ok.source, ok.reason)
    assert ok.text == clean
    assert not ok.violations

    # And the checker is usable on its own, in both directions.
    assert pr_prose.check(clean) == []
    assert pr_prose.check("I verified every reference") != []


def test_the_narrative_never_writes_the_ledger():
    """The model gets the FACTS and is asked for prose. It must not supply counts.

    Two guarantees, and the second is the load-bearing one:

    1. The prompt contains the structured facts, so the model has something true to
       render and no reason to invent.
    2. When the model is not used, the body still carries a narrative -- the
       deterministic one -- so the PR's shape does not depend on whether a backend
       happened to be reachable. A body that silently loses a section when the LLM is
       down is how "the model was rate limited" becomes invisible.
    """
    from app import pr_prose

    facts = {"change_type": "removed_field", "field_name": "phone_number",
             "language": "python", "consumer_file": "src/checkout.py",
             "edits": [{"shape": "dict-literal entry"}],
             "refusals": ["line 4: function parameter"],
             "notes": ["line 12: mentioned in a comment"]}

    seen = {}

    class _R:
        text, ok, attempts, answered_by = "Fine prose about the change.", True, (), "m"

        def stated_outcome(self):
            return "answered by m"

    def _capture(prompt):
        seen["prompt"] = prompt
        return _R()

    pr_prose.render(facts, call_chain=_capture)
    prompt = seen["prompt"]
    for token in ("removed_field", "phone_number", "dict-literal entry",
                  "function parameter"):
        assert token in prompt, f"the model was not told {token!r}, so it cannot render it"
    assert "Do not add to them" in prompt, "the prompt does not forbid invention"

    # No model at all: still a narrative, and it says it is not model-written.
    none = pr_prose.render(facts)
    assert none.text.strip(), "no narrative at all when no model is configured"
    assert none.source == "template"
    assert "phone_number" in none.text
    # A refusal exists, so the deterministic narrative must not read as complete.
    assert "decision" in none.text.lower(), none.text


def test_narrative_provenance_is_stated_on_both_paths():
    """Unlabelled prose must never be able to read as human-written.

    If only model-written narrative carried a label, a reader would learn that
    unlabelled prose is human -- and every deterministic narrative would then be
    silently miscredited. So both paths state their author.

    This is also the one place the user-facing goal and the codebase's honesty rule
    meet: the prose is allowed to read like a person wrote it, and is required to say
    that a machine did.
    """
    from app import pr_prose

    class _R:
        text, ok, attempts, answered_by = "Readable prose.", True, (), "openrouter"

        def stated_outcome(self):
            return "answered by openrouter"

    model = pr_prose.render({"field_name": "x"}, call_chain=lambda p: _R())
    template = pr_prose.render({"field_name": "x"})

    for res in (model, template):
        line = res.provenance_line()
        assert line.strip(), f"{res.source} path states no provenance"
        assert "_" in line, "provenance is not rendered as markdown emphasis"

    assert "written by" in model.provenance_line().lower()
    assert "openrouter" in model.provenance_line()
    assert "not model-written" in template.provenance_line()
    assert model.provenance_line() != template.provenance_line()


def test_the_pr_narrative_is_reachable_from_the_webhook():
    """A narrative renderer nobody calls is the built-but-unreachable shape again.

    The learning loop's author-fetch was written, tested and unreachable for weeks.
    This asserts the wiring rather than trusting it: the renderer must be called, its
    output must reach format_pr_body, and its budget must be reset per push.
    """
    import inspect
    import ast


    from app import webhook as W

    assert hasattr(W, "_render_pr_narrative"), "no narrative renderer exists"
    assert hasattr(W, "reset_prose_budget"), "prose has no per-push budget reset"

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tree = ast.parse(open(os.path.join(root, "app", "webhook.py"),
                          encoding="utf-8").read())

    calls = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            name = (getattr(node.func, "id", "")
                    or getattr(node.func, "attr", ""))
            if name in ("_render_pr_narrative", "reset_prose_budget",
                        "format_pr_body"):
                calls.setdefault(name, set()).add(fn.name)

    assert calls.get("_render_pr_narrative"), (
        "_render_pr_narrative is defined but never called -- the PR body would carry "
        "no narrative and nothing would say so")
    assert calls.get("reset_prose_budget"), (
        "reset_prose_budget is never called, so a provider closed by one push stays "
        "closed for the life of the process")

    # The narrative must reach the body, not merely be computed.
    shared = calls["_render_pr_narrative"] & calls.get("format_pr_body", set())
    assert shared, (
        f"_render_pr_narrative is called from {calls['_render_pr_narrative']} and "
        f"format_pr_body from {calls.get('format_pr_body')} -- no function does both, "
        f"so the narrative is computed and discarded")

    # AND it must be PASSED IN. Both being called from the same function is not
    # enough: deleting the keyword argument leaves the renderer running and its output
    # dropped on the floor.
    #
    # The first version of this asserted `"fix_summary=" in inspect.getsource(W)`,
    # with an `or` fallback that made it nearly unfalsifiable -- a mutation removing
    # the keyword argument SURVIVED it. That is the same "gate that cannot be made to
    # fail" shape the reachability audit records about its own first version.
    passes_narrative = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if (getattr(node.func, "id", "") or getattr(node.func, "attr", "")) \
                != "format_pr_body":
            continue
        for kw in node.keywords:
            if kw.arg and "prose" in ast.dump(kw.value):
                passes_narrative = True
    assert passes_narrative, (
        "format_pr_body is called but no keyword argument carries the narrative, so "
        "_render_pr_narrative runs and its output is discarded -- the PR body would "
        "have no narrative and nothing would say why")


def test_an_exhausted_llm_chain_states_what_happened():
    """The failure must be legible afterwards, not a print() to stdout.

    Replaced defect, app/fix_generator.py:

        except Exception as e:
            print(f"  ⚠️  LLM error: {e}. Using template fix.")
            return _generate_with_template(...)

    The template fix it returned was real and its provenance was honestly `template`.
    What was wrong is that afterwards a RATE-LIMITED model and a NEVER-CONFIGURED one
    were indistinguishable: the only record was stdout, there was no activity event,
    and the PR body said nothing about an attempt having been made.
    """
    from app import llm_chain as C

    class _Boom(Exception):
        def __init__(self, status):
            super().__init__(f"status {status}")
            self.status_code = status

    with _with_provider_credentials():
        chain = C.fix_chain()
        assert chain, "fix chain is empty even with credentials present"

        # Every provider rate-limits.
        #
        # payload=PAYLOAD_PUBLIC throughout this test and its siblings below: the
        # prompt here is the literal fixture on the next line, so it carries nothing
        # from any repository. These tests exercise the chain's FAILURE CLASSIFICATION,
        # which is orthogonal to the data boundary -- and run()'s default is
        # PAYLOAD_SOURCE, so without saying so a hosted provider would be refused
        # before the classification under test ever ran. That refusal is asserted
        # separately in test_a_hosted_provider_cannot_silently_receive_repository_content.
        res = C.run(lambda p: (_ for _ in ()).throw(_Boom(429)), chain=chain,
                    budget=C.Budget(), payload=C.PAYLOAD_PUBLIC)
        assert not res.ok
        assert res.text == "" and res.answered_by == ""
        stated = res.stated_outcome()
        assert stated, "an exhausted chain returned no statement of what happened"
        assert "rate_limited" in stated, stated
        assert chain[0].name in stated, (
            f"the statement does not name the providers tried: {stated}")

    # And nothing configured at all is a DIFFERENT statement, not the same silence.
    empty = C.run(lambda p: "x", chain=[], budget=C.Budget())
    assert not empty.ok
    assert "none configured" in empty.stated_outcome(), empty.stated_outcome()
    assert empty.stated_outcome() != stated, (
        "a rate-limited chain and an unconfigured one produce the same statement, "
        "which is the ambiguity this replaces")


def test_a_provider_with_no_credential_is_not_in_the_chain():
    """Three names over one endpoint is not a fallback chain.

    The first draft of _generate_with_llm read llm_config.base_url() for every entry,
    so each provider in the chain hit the SAME endpoint and the answer was credited to
    whichever came first. Measured: with no OpenRouter key set, a local Ollama reply
    was reported as `answered by openrouter`. Same misreporting backend_label() exists
    to prevent, reached from the other side -- a correctly-named model that was never
    called.

    Two things stop it: each entry now uses its own base_url and its own credential,
    and an entry with no credential is excluded rather than being a guaranteed 401.
    """
    import os as _os

    from app import llm_chain as C
    from app.llm_providers import PROVIDERS

    keyed = [p for p in PROVIDERS.values() if p.key_env]
    assert keyed, "no provider declares a credential -- this check would be vacuous"

    saved = {p.key_env: _os.environ.get(p.key_env) for p in keyed}
    try:
        for p in keyed:
            _os.environ.pop(p.key_env, None)
        names = {p.name for p in C.prose_chain()}
        for p in keyed:
            assert p.name not in names, (
                f"{p.name} needs {p.key_env}, which is unset, yet it is in the chain")
        # Keyless providers remain, because a self-hosted endpoint that
        # authenticates nothing is a legitimate configuration.
        keyless = {p.name for p in PROVIDERS.values()
                   if not p.key_env and p.usable_today}
        assert names == keyless, f"expected only keyless providers, got {names}"
    finally:
        for k, v in saved.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v


def test_a_malformed_request_stops_the_chain_instead_of_multiplying():
    """400 is OUR bug. Asking three providers the same bad question hides the cause."""
    from app import llm_chain as C

    class _Boom(Exception):
        def __init__(self, status):
            super().__init__(f"status {status}")
            self.status_code = status

    with _with_provider_credentials():
        chain = C.fix_chain()
    assert len(chain) >= 1, "fix chain empty with credentials present"

    calls = []

    def _bad(p):
        calls.append(p.name)
        raise _Boom(400)

    res = C.run(_bad, chain=chain, budget=C.Budget(), payload=C.PAYLOAD_PUBLIC)
    assert not res.ok
    assert len(calls) == 1, (
        f"a malformed request was retried against {len(calls)} providers; 400 is "
        f"terminal because the payload will fail everywhere")
    assert "bad_request" in res.stated_outcome()

    # A retryable status DOES advance, or the chain is pointless.
    retried = []

    def _limited(p):
        retried.append(p.name)
        raise _Boom(429)

    C.run(_limited, chain=chain, budget=C.Budget(), payload=C.PAYLOAD_PUBLIC)
    assert len(retried) == len(chain), (
        f"429 should try every provider; tried {len(retried)} of {len(chain)}")

    # Classification is the thing both behaviours rest on.
    assert C.classify(_Boom(429))[0] == "rate_limited"
    assert C.classify(_Boom(402))[0] == "no_credit"
    assert C.classify(_Boom(401))[0] == "auth"
    assert C.classify(_Boom(400))[0] == "bad_request"
    assert C.classify(_Boom(503))[0] == "unreachable"
    assert C.classify(OSError("connection refused"))[0] == "unreachable", (
        "a refused connection must be transient, or a stopped local Ollama would "
        "terminate the chain")
    for retryable in C.RETRYABLE:
        assert retryable not in C.TERMINAL


def test_an_empty_model_response_is_a_failure_not_a_fix():
    """A blank body looks like success and would be reported as the wrong cause.

    Left unhandled it returns "" to generate_fix, the diff contract rejects it as an
    empty fix, and the PR records a contract violation rather than "the model returned
    nothing" -- a true statement about the wrong layer.
    """
    from app import llm_chain as C

    with _with_provider_credentials():
        chain = C.fix_chain()
    res = C.run(lambda p: "   \n  ", chain=chain, budget=C.Budget(),
                payload=C.PAYLOAD_PUBLIC)
    assert not res.ok, "whitespace was accepted as a fix"
    assert "empty_response" in res.stated_outcome(), res.stated_outcome()


def test_a_symbol_delta_never_reports_unknown_as_clean():
    """An unparsable revision must not read as "nothing was removed".

    This is the whole reason `SymbolDelta.reliable` exists. A PR containing a syntax
    error would otherwise be announced as safe, because the head parses to zero symbols
    and the subtraction comes out empty.
    """
    from app import repo_index as R

    base = R.extract("src/user.py",
                     "class User:\n    pass\n\ndef helper(): pass\n\nMAX = 1\n")
    head = R.extract("src/user.py", "class User:\n    pass\n\nMAX = 1\n")

    d = R.delta(base, head)
    assert d.removed == ("helper",), d.removed
    assert d.added == ()
    assert d.method == "ast" and d.reliable and d.is_breaking

    # Reverse direction is an ADDITION, which is not breaking.
    rev = R.delta(head, base)
    assert rev.added == ("helper",) and rev.removed == ()
    assert not rev.is_breaking

    # The dangerous case.
    broken = R.extract("src/user.py", "class User:\n  def (((\n")
    u = R.delta(base, broken)
    assert u.removed == (), "a delta against an unparsable head must not guess"
    assert u.reliable is False, (
        "an unparsable revision was reported as reliable, so a syntax error would be "
        "announced as 'no symbols removed'")
    assert u.is_breaking is False
    assert "unknown rather than empty" in u.reason, u.reason

    # Mixed exactness downgrades to the weaker side rather than claiming exact.
    tb = R.extract("a.ts", "export const A = 1\nexport const B = 2\n")
    th = R.extract("a.ts", "export const A = 1\n")
    m = R.delta(tb, th)
    assert m.removed == ("B",) and m.method == "regex", (m.removed, m.method)


def test_a_failed_revision_fetch_is_not_an_empty_file():
    """`_fetch_file_at_sha` returned "" for both, and the PR analyser subtracts.

    With "" standing in for a failure, the two directions are confidently wrong:

        head fetch fails -> head looks empty -> EVERY base symbol looks removed
        base fetch fails -> base looks empty -> a real removal reported as clean

    The three older callers test truthiness, so None is behaviourally identical for
    them; only the analyser needed the distinction.
    """
    import inspect

    from app import webhook as W

    src = inspect.getsource(W._fetch_file_at_sha)
    assert "return None" in src, (
        "_fetch_file_at_sha no longer signals failure distinctly, so an unfetchable "
        "revision parses as an empty file")
    assert 'return ""' not in src.split("def ")[-1] or "content" in src

    # Every older caller must still be truthiness-based, or None breaks them.
    whole = inspect.getsource(W)
    for guard in ("if config_content:", "if not old_content or not new_content:"):
        assert guard in whole, (
            f"caller guard {guard!r} is gone -- verify it still tolerates None")


def test_a_pull_request_being_opened_is_analysed_not_ignored():
    """The trigger Ripple did not have.

    Before this, `pull_request` + `opened` fell through to {"status": "ignored"}: Ripple
    reacted only to a push landing on the default branch, never to a PR under review --
    which is the moment telling someone "this breaks three other repos" is useful.

    Driven with a stubbed GitHub API so it needs no token and no network, but through
    the REAL handler, because a test that reimplements the dispatch proves nothing about
    the dispatch.
    """
    import asyncio

    from app import webhook as W

    BASE = ("class User:\n    pass\n\n\ndef legacy_helper():\n    return 1\n")
    HEAD = ("class User:\n    pass\n")

    calls = []

    def _fake_api(method, path, token, data=None, **kw):
        calls.append(path)
        if "/pulls/7/files" in path:
            return [{"filename": "src/user.py", "status": "modified"},
                    {"filename": "docs/readme.md", "status": "modified"},
                    {"filename": "src/brand_new.py", "status": "added"}]
        return {"error": "unexpected"}

    def _fake_fetch(repo, p, sha, token):
        if p != "src/user.py":
            return None
        return BASE if sha == "base111" else HEAD

    saved = (W._github_api, W._fetch_file_at_sha, W._get_token)
    W._github_api = _fake_api
    W._fetch_file_at_sha = _fake_fetch
    W._get_token = lambda _id: "tok"
    try:
        payload = {
            "action": "opened",
            "repository": {"full_name": "acme/api", "default_branch": "main"},
            "installation": {"id": 42},
            "pull_request": {"number": 7,
                             "base": {"sha": "base111"},
                             "head": {"sha": "head222"}},
        }
        res = asyncio.new_event_loop().run_until_complete(
            W._handle_pr_opened(payload, payload["pull_request"]))
    finally:
        W._github_api, W._fetch_file_at_sha, W._get_token = saved

    assert res["status"] == "analysed", res
    assert res["repo"] == "acme/api" and res["pr"] == 7
    assert res["symbols_removed"] == 1, res
    syms = [r["symbol"] for r in res["runs"]]
    assert syms == ["legacy_helper"], syms
    assert res["runs"][0]["method"] == "ast", "an exact parse was reported as inexact"

    # A markdown file is skipped with a reason, not silently dropped.
    assert any("readme" in s["path"] for s in res["skipped"]), res["skipped"]
    # An added file has no base revision, so nothing can have been removed from it.
    assert any("brand_new" in s["path"] for s in res["skipped"]), res["skipped"]

    # AND THE DISPATCH MUST ROUTE HERE -- proven by DRIVING THE REAL ROUTE, not by
    # reading it.
    #
    # Two weaker versions were tried and both had a mutant survive:
    #   1. `assert "_handle_pr_opened" in routed`  -- a mutation to `if False and ...`
    #      survived, because the call was still present, merely unreachable. Presence
    #      is not reachability.
    #   2. checking the branch tests `event_type` -- a mutation to
    #      `if event_type == "push"` survived, because it still references the name
    #      while comparing it to the wrong value.
    #
    # Structural checks have a floor there. `_verify_signature` is a no-op with no
    # WEBHOOK_SECRET set, so the route can be driven with a minimal request and the
    # dispatch answers for itself.
    import asyncio as _aio
    import json as _json

    class _Req:
        def __init__(self, payload, event):
            self._body = _json.dumps(payload).encode()
            self.headers = {"X-GitHub-Event": event}

        async def body(self):
            return self._body

    saved2 = (W._github_api, W._fetch_file_at_sha, W._get_token)
    W._github_api = _fake_api
    W._fetch_file_at_sha = _fake_fetch
    W._get_token = lambda _id: "tok"
    saved_secret = os.environ.pop("WEBHOOK_SECRET", None)
    try:
        routed_res = _aio.new_event_loop().run_until_complete(
            W.github_webhook(_Req(payload, "pull_request")))
    finally:
        W._github_api, W._fetch_file_at_sha, W._get_token = saved2
        if saved_secret is not None:
            os.environ["WEBHOOK_SECRET"] = saved_secret

    assert routed_res.get("status") == "analysed", (
        f"a real pull_request webhook did not reach the analyser -- got "
        f"{routed_res!r}. Either the dispatch branch is missing, guarded by a "
        f"constant, or comparing event_type to the wrong value.")
    assert routed_res.get("pr") == 7, routed_res
    assert routed_res.get("symbols_removed") == 1, routed_res


def test_a_pr_with_nothing_removed_still_terminates_explicitly():
    """"No breaking symbols" must be distinguishable from "the analysis crashed".

    Same rule the push path follows: every change gets a ChangeRun so a run that leads
    nowhere still emits a terminal state. A PR that returns silence is indistinguishable
    from one Ripple never looked at.
    """
    import asyncio

    from app import webhook as W

    def _fake_api(method, path, token, data=None, **kw):
        if "/files" in path:
            return [{"filename": "src/user.py", "status": "modified"}]
        return {"error": "unexpected"}

    # Additive change only: a symbol gained, none lost.
    def _fake_fetch(repo, p, sha, token):
        return ("def a(): pass\n" if sha == "b1"
                else "def a(): pass\n\n\ndef b(): pass\n")

    saved = (W._github_api, W._fetch_file_at_sha, W._get_token)
    W._github_api, W._fetch_file_at_sha = _fake_api, _fake_fetch
    W._get_token = lambda _id: "tok"
    try:
        payload = {"action": "synchronize",
                   "repository": {"full_name": "acme/api"},
                   "installation": {"id": 1},
                   "pull_request": {"number": 3, "base": {"sha": "b1"},
                                    "head": {"sha": "h1"}}}
        res = asyncio.new_event_loop().run_until_complete(
            W._handle_pr_opened(payload, payload["pull_request"]))
    finally:
        W._github_api, W._fetch_file_at_sha, W._get_token = saved

    assert res["status"] == "analysed"
    assert res["symbols_removed"] == 0
    assert res["runs"] == [], "no symbol was removed, so there is nothing to run"

    # A missing token or a malformed payload REFUSES rather than ignoring, because
    # "ignored" reads as a choice not to look.
    empty = asyncio.new_event_loop().run_until_complete(
        W._handle_pr_opened({"action": "opened", "repository": {},
                             "pull_request": {}}, {}))
    assert empty["status"] == "refused", empty
    assert "payload" in empty["reason"], empty


def _impact_fixture(truncated=False):
    """Three indexed repos: a producer, an exact consumer, an approximate one."""
    from app import repo_index as R

    producer = R.build("acme/models", [
        ("src/user.py", "class User:\n    phone_number = ''\n"),
    ])
    consumer = R.build("acme/checkout", [
        ("src/pay.py", "from src.user import User\n\ndef pay(u):\n"
                       "    return u.phone_number\n"),
        ("src/unrelated.py", "def totals():\n    return 1\n"),
    ])
    web = R.build("acme/web", [("app/form.ts", "const x = row.phone_number\n")])
    web.truncated = truncated
    return [producer, consumer, web]


class _FakeChain:
    """Stands in for a ChainResult so ranking can be driven without a model."""

    def __init__(self, text, ok=True, outcome="no model answered"):
        self.text, self.ok, self._outcome = text, ok, outcome
        self.attempts = ()

    def stated_outcome(self):
        return self._outcome


def test_impact_analysis_refuses_rather_than_claiming_nothing_is_affected():
    """"Nothing is affected" and "I did not look" must never render identically.

    This gate outlived the Stage 3 seam it was written for. The seam returned a
    hardcoded refusal; Stage 4 replaced it with a real cross-repo search, so the
    property is now asserted in BOTH directions -- an unsearched estate refuses, and
    a fully searched one is allowed to answer. Only asserting the refusal would let
    a search that finds nothing ever pass by refusing always.
    """
    import contextlib

    from app import impact as I
    from app import repo_index as R
    from app import webhook as W

    @contextlib.contextmanager
    def _estate(indexes):
        """Control what the search can see.

        `indexes` is patched rather than RIPPLE_DATA_DIR because that variable is read
        into _DATA_DIR_CANDIDATES at IMPORT time -- setting it inside a test that has
        already imported the module has no effect, and the first version of this gate
        silently wrote a fixture index into the real data directory as a result.
        """
        saved = W._repo_index.indexed_repos
        W._repo_index.indexed_repos = lambda: list(indexes)
        try:
            yield
        finally:
            W._repo_index.indexed_repos = saved

    facts = R.extract("src/u.py", "def gone(): pass\n")
    d = R.delta(facts, R.FileFacts(path="src/u.py", language="python", method="ast"))

    # No index exists at all -> UNKNOWN, never "none".
    with _estate([]):
        out = W._analyse_pr_impact("acme/api", "gone", d, "tok")

    assert out["affected"] == [], out["affected"]
    assert out["refusals"], (
        "impact analysis returned no affected repos AND no refusal, which the funnel "
        "would record as 'nothing is affected'")
    assert any("UNKNOWN rather than none" in r for r in out["refusals"]), out["refusals"]

    # A COMPLETE search over a real estate is allowed to answer, and does.
    found = I.find("phone_number", "acme/models", "src/user.py",
                   indexes=_impact_fixture(),
                   known_repos=["acme/models", "acme/checkout", "acme/web"])
    keys = [c.key() for c in found.candidates]
    assert "acme/checkout:src/pay.py" in keys, keys
    assert "acme/models:src/user.py" not in keys, (
        "the file whose change started the analysis was reported as its own consumer")
    assert "acme/checkout:src/unrelated.py" not in keys, (
        "an unrelated file was named, which is the false positive that would make a "
        "live demo untrustworthy")

    # And a genuinely unused symbol over a complete estate is a FACT, not a refusal.
    clean = I.find("no_such_symbol_anywhere", "acme/models", "src/user.py",
                   indexes=_impact_fixture(),
                   known_repos=["acme/models", "acme/checkout", "acme/web"])
    assert clean.candidates == []
    assert clean.is_confident_none(), (
        "a complete search that found nothing must be able to SAY nothing is "
        "affected -- otherwise the refusal is unfalsifiable and carries no signal")


def test_the_model_cannot_add_a_file_the_index_did_not_find():
    """The architecture rule, enforced rather than documented.

    A model asked "which files use this?" will answer with plausible paths, and a
    plausible path that does not exist is a false positive delivered with full
    confidence. Ranking is therefore intersected with the candidate set: an invented
    path is discarded and RECORDED, never returned.
    """
    from app import impact as I

    found = I.find("phone_number", "acme/models", "src/user.py",
                   indexes=_impact_fixture())
    real = {c.key() for c in found.candidates}
    assert len(real) >= 2, real

    lying = _FakeChain(
        "acme/web:app/form.ts | renders the field\n"
        "acme/checkout:src/pay.py | reads it during payment\n"
        "acme/billing:src/invoice.py | I am confident this exists\n")
    ranked = I.rank(found, call_chain=lambda _p: lying)

    returned = {c.key() for c in ranked.candidates}
    assert returned == real, (
        f"ranking changed the candidate SET, not just its order: {returned ^ real}")
    assert "acme/billing:src/invoice.py" not in returned
    assert any("acme/billing" in v for v in ranked.violations), ranked.violations


def test_the_model_cannot_delete_a_candidate_the_index_found():
    """Omission is not counter-evidence.

    A deterministic hit is a parse or a pattern match in a file that exists. A model
    forgetting to list it says nothing about the file, so an omitted candidate is
    APPENDED with the omission recorded -- dropping it would let a model silently
    shrink the impact set, which is the same dishonesty as inventing one, inverted.
    """
    from app import impact as I

    found = I.find("phone_number", "acme/models", "src/user.py",
                   indexes=_impact_fixture())
    real = {c.key() for c in found.candidates}

    forgetful = _FakeChain("acme/web:app/form.ts | renders the field\n")
    ranked = I.rank(found, call_chain=lambda _p: forgetful)

    assert {c.key() for c in ranked.candidates} == real, (
        "a candidate the index found was dropped because the model did not mention it")
    assert any("omitted" in v for v in ranked.violations), ranked.violations


def test_a_granted_repo_that_was_never_indexed_is_not_reported_as_clean():
    """A repo nobody read must not look like a repo with no consumers.

    This is the distinction that makes the whole answer usable: an estate of ten
    repos where two were indexed can only speak for two. `unsearchable` names the
    rest, and `is_confident_none` refuses while any of them remain.
    """
    from app import impact as I

    found = I.find("phone_number", "acme/models", "src/user.py",
                   indexes=_impact_fixture(),
                   known_repos=["acme/models", "acme/checkout", "acme/web",
                                "acme/legacy", "acme/mobile"])

    unsearched = {u["repo"] for u in found.unsearchable}
    assert unsearched == {"acme/legacy", "acme/mobile"}, unsearched
    assert not found.searched_everything

    clean = I.find("nothing_uses_this", "acme/models", "src/user.py",
                   indexes=_impact_fixture(), known_repos=["acme/legacy"])
    assert clean.candidates == []
    assert not clean.is_confident_none(), (
        "an estate with an unindexed repo reported 'nothing is affected' as a fact")
    assert clean.refusals, "the incomplete search produced no refusal"


def test_an_approximate_match_is_not_presented_as_an_exact_one():
    """A regex hit and an AST hit are both "found"; only one is a fact.

    Python is parsed. TypeScript is pattern-matched, so a name inside a comment or a
    string can over-match. Both are worth showing, but a reviewer reading top-down
    must meet the parse results first and must be told which is which.
    """
    from app import impact as I

    found = I.find("phone_number", "acme/models", "src/user.py",
                   indexes=_impact_fixture())
    conf = [(c.key(), c.confidence) for c in found.candidates]

    assert ("acme/checkout:src/pay.py", "exact") in conf, conf
    assert ("acme/web:app/form.ts", "approximate") in conf, conf

    order = [c.confidence for c in found.candidates]
    assert order == sorted(order, key=lambda c: c != "exact"), (
        f"an approximate match outranked an exact one: {conf}")


def test_a_truncated_index_makes_the_answer_partial():
    """An index that stopped reading cannot support "no consumers".

    The file cap is declared in repo_index rather than discovered, and it has to
    survive into the answer -- a repo indexed up to 2000 files and a repo fully
    indexed give the same empty list for a symbol in file 2001.
    """
    from app import impact as I

    found = I.find("nothing_uses_this", "acme/models", "src/user.py",
                   indexes=_impact_fixture(truncated=True),
                   known_repos=["acme/models", "acme/checkout", "acme/web"])

    assert found.partial, "a truncated index produced a non-partial answer"
    assert not found.is_confident_none()
    assert any("file cap" in r for r in found.refusals), found.refusals


def test_an_ambiguous_symbol_says_so_instead_of_listing_noise():
    """`phone_number` matching four files is evidence; `id` matching four hundred is not.

    A bare-name index cannot tell a reference from a coincidence, so ambiguity is
    reported as a property of the symbol rather than being hidden inside a confident
    ranking.
    """
    from app import impact as I

    short = I.find("id", "acme/models", "src/user.py", indexes=_impact_fixture())
    assert short.ambiguity, "a two-character symbol was treated as unambiguous"
    assert any("coincidence" in r for r in short.refusals), short.refusals
    assert not short.is_confident_none()

    named = I.find("phone_number", "acme/models", "src/user.py",
                   indexes=_impact_fixture())
    assert not named.ambiguity, named.ambiguity


def test_cross_repo_impact_is_reachable_from_the_real_pr_route():
    """Driven through the actual dispatch, not a structural check.

    Stage 3 learned this the hard way: asserting the call APPEARS in the source
    survived both `if False and ...` and a wrong-valued condition, because presence
    is not reachability. So this drives the webhook route end to end and asserts a
    consumer in a DIFFERENT repo comes back on the response.
    """
    import asyncio

    from app import repo_index as R
    from app import webhook as W

    # `phone_number` is a CLASS attribute -- the shape every worked example uses, and
    # the one the extractor originally missed entirely.
    BASE = "class User:\n    phone_number = ''\n"
    HEAD = "class User:\n    pass\n"

    def _fake_api(method, path, token, data=None, **kw):
        if "/pulls/7/files" in path:
            return [{"filename": "src/user.py", "status": "modified"}]
        return {"error": "unexpected"}

    def _fake_fetch(repo, p, sha, token):
        if p != "src/user.py":
            return None
        return BASE if sha == "base111" else HEAD

    # A consumer in ANOTHER repo, indexed exactly as installation would index it, and
    # injected rather than written to disk: R.save() here wrote a fixture into the real
    # data directory, because RIPPLE_DATA_DIR is read at import time.
    consumer = R.build("acme/checkout", [
        ("src/pay.py", "from src.user import User\n\ndef pay(u):\n"
                       "    return u.phone_number\n")])

    saved = (W._github_api, W._fetch_file_at_sha, W._get_token,
             W._repo_index.indexed_repos)
    W._github_api = _fake_api
    W._fetch_file_at_sha = _fake_fetch
    W._get_token = lambda _id: "tok"
    W._repo_index.indexed_repos = lambda: [consumer]
    try:
        payload = {
            "action": "opened",
            "repository": {"full_name": "acme/api", "default_branch": "main"},
            "installation": {"id": 42},
            "pull_request": {"number": 7,
                             "base": {"sha": "base111"},
                             "head": {"sha": "head222"}},
        }
        res = asyncio.new_event_loop().run_until_complete(
            W._handle_pr_opened(payload, payload["pull_request"]))
    finally:
        (W._github_api, W._fetch_file_at_sha, W._get_token,
         W._repo_index.indexed_repos) = saved

    assert res["status"] == "analysed", res
    assert res["symbols_removed"] == 1, (
        f"a removed CLASS ATTRIBUTE was not detected: {res}")
    runs = res["runs"]
    assert runs, res
    affected = [(h["repo"], h["path"]) for r in runs for h in r.get("affected", [])]
    assert ("acme/checkout", "src/pay.py") in affected, (
        f"the cross-repo consumer never reached the PR response: {affected}")
    assert all(h.get("confidence") in ("exact", "approximate")
               for r in runs for h in r.get("affected", [])), runs


def test_only_one_prose_grade_transport_exists():
    """Narrative and ranking must share one client.

    Two prose call sites means two places to read a credential, ignore `base_url`, or
    hardcode a model -- exactly the four-bug shape `/test-llm` shipped with for
    months. The diagnostic endpoint keeps its own client on purpose: its job is to
    report what a raw call does, so routing it through the shared helper would make
    it test the helper instead of the configuration. Every other constructor is a
    regression, so the OWNERS are named rather than merely counted.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("app/webhook.py").read_text(encoding="utf-8"))

    owners = sorted(
        f.name for f in ast.walk(tree)
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "Anthropic" for n in ast.walk(f))
    )
    assert owners == ["_ask_provider", "test_llm"], (
        f"an unexpected Anthropic client was constructed in {owners}; prose-grade "
        f"calls must go through _ask_provider")

    callers = {
        f.name for f in ast.walk(tree)
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "_ask_provider" for n in ast.walk(f))
    }
    assert "_impact_ranker" in callers, (
        "impact ranking does not go through the shared transport")
    assert "_render_pr_narrative" in callers, (
        "narrative rendering no longer goes through the shared transport")


def test_a_ranking_note_goes_through_the_same_claim_check_as_the_narrative():
    """A second prose surface must not bypass the first one's guardrail.

    This is how a gate quietly stops applying: `pr_prose.check` was written to hold
    PR narrative to earned claims, then ranking added a NEW model-authored sentence
    beside it. The first real run against a local model produced
    "necessitating changes to maintain the integrity and functionality of the
    application" -- an invented consequence, on a pull request, unchecked.

    Notes are DROPPED rather than edited: rewriting a model's sentence to remove a
    claim leaves prose the model never wrote, presented as though it did.
    """
    from app import impact as I

    found = I.find("phone_number", "acme/models", "src/user.py",
                   indexes=_impact_fixture())
    keys = [c.key() for c in found.candidates]
    assert len(keys) >= 2, keys

    overclaiming = _FakeChain(
        f"{keys[0]} | this has been verified and is safe to merge\n"
        f"{keys[1]} | reads the field during checkout\n")
    ranked = I.rank(found, call_chain=lambda _p: overclaiming)

    by_key = {c.key(): c for c in ranked.candidates}
    assert by_key[keys[0]].note == "", (
        f"an unearned claim reached the pull request: {by_key[keys[0]].note!r}")
    assert by_key[keys[1]].note, "a clean note was dropped along with the bad one"
    assert any("note dropped" in v for v in ranked.violations), ranked.violations

    # The CANDIDATE survives a bad note. The index found it; the prose is separate.
    assert set(by_key) == set(keys), (
        "a candidate was discarded because its note was rejected -- the deterministic "
        "hit is evidence and does not depend on the model writing well about it")

    # Hedging about a reference the index established exactly is also dropped, and
    # matched as a FAMILY: the first version listed "may contain" and a real model run
    # immediately produced "may reference", so every spelling below must be caught by
    # one pattern rather than by an enumeration that grows one bug at a time.
    for hedge in ("this file likely contains references to it",
                  "it may reference the symbol in its code",
                  "might possibly use the field",
                  "appears to reference the removed name",
                  "could import the removed helper"):
        fresh = I.find("phone_number", "acme/models", "src/user.py",
                       indexes=_impact_fixture())
        weak = _FakeChain(f"{keys[0]} | {hedge}\n")
        got = I.rank(fresh, call_chain=lambda _p: weak)
        note = {c.key(): c.note for c in got.candidates}[keys[0]]
        assert note == "", f"a hedge was published: {hedge!r} -> {note!r}"
        assert any("hedged" in v for v in got.violations), (hedge, got.violations)

    # PADDING is dropped too. These are VERBATIM notes from a real deepseek-coder-v2
    # run: given only a path and a language, the model restated the candidate row for
    # every single candidate, despite a prompt forbidding exactly that. Publishing them
    # would make the pull request read like a generated form.
    for padding in (
        "This file is written in Python and contains an exact match to the removed "
        "symbol, suggesting it may be part of a larger module",
        "This file is written in TypeScript and contains an approximate match to the "
        "removed symbol",
        "The exact match for the removed symbol indicates a direct reference",
    ):
        fresh = I.find("phone_number", "acme/models", "src/user.py",
                       indexes=_impact_fixture())
        padded = _FakeChain(f"{keys[0]} | {padding}\n")
        got = I.rank(fresh, call_chain=lambda _p: padded)
        note = {c.key(): c.note for c in got.candidates}[keys[0]]
        assert note == "", f"padding was published: {note!r}"
        assert set(c.key() for c in got.candidates) == set(keys), (
            "a candidate was lost when its note was dropped as padding")

    # And a clause that says something real survives, so the check is not vacuous.
    fresh = I.find("phone_number", "acme/models", "src/user.py",
                   indexes=_impact_fixture())
    good = _FakeChain(f"{keys[0]} | reads the field when building the payment "
                      f"payload, so the call site changes shape\n")
    kept = I.rank(fresh, call_chain=lambda _p: good)
    assert {c.key(): c.note for c in kept.candidates}[keys[0]], (
        "an informative note was dropped, so the check rejects everything and carries "
        "no signal")


def _pr_run(symbol="phone_number", path="src/user.py", line=5, **over):
    """One analysed run, in the flattened shape the PR handler produces."""
    run = {
        "symbol": symbol, "path": path, "line": line, "method": "ast",
        "affected": [
            {"repo": "acme/checkout", "path": "src/pay.py", "language": "python",
             "confidence": "exact", "note": "reads it when building the payload"},
            {"repo": "acme/web", "path": "app/P.tsx", "language": "typescript",
             "confidence": "approximate", "note": ""},
        ],
        "refusals": [], "unsearchable": [], "violations": [],
        "ambiguity": "", "partial": False,
    }
    run.update(over)
    return run


class _FakeGitHub:
    """Records calls and replays whatever comments have been "posted" so far."""

    def __init__(self, review=None, issue=None, fail_read=False):
        self.calls = []
        self.review = list(review or [])
        self.issue = list(issue or [])
        self.fail_read = fail_read
        self._next_id = 100

    def __call__(self, method, path, token, data=None, **kw):
        self.calls.append((method, path, data))
        if method == "GET" and "/pulls/" in path and "/comments" in path:
            return {"error": "boom"} if self.fail_read else list(self.review)
        if method == "GET" and "/issues/" in path:
            return {"error": "boom"} if self.fail_read else list(self.issue)
        if method == "POST" and path.endswith("/reviews"):
            for c in (data or {}).get("comments", []):
                self._next_id += 1
                self.review.append({"id": self._next_id, "body": c["body"]})
            return {"id": 1}
        if method == "POST" and "/issues/" in path:
            self._next_id += 1
            self.issue.append({"id": self._next_id, "body": (data or {})["body"]})
            return {"id": self._next_id}
        if method == "PATCH":
            target = int(path.rsplit("/", 1)[-1])
            for bucket in (self.review, self.issue):
                for c in bucket:
                    if c["id"] == target:
                        c["body"] = (data or {})["body"]
            return {"id": target}
        return {"error": f"unexpected {method} {path}"}


def test_a_review_comment_anchors_to_a_line_that_is_actually_in_the_diff():
    """The one physical constraint that shapes this whole feature.

    GitHub accepts an inline comment only on a line in THIS pull request's diff, and
    the affected files are in OTHER repositories -- so there is no line to attach
    them to. The comment therefore anchors on the line where the symbol was REMOVED,
    which is in the diff on the BASE side, and names the consumers in its body.

    `side` must be LEFT: a removed line exists only in the base revision, and asking
    for it on the RIGHT is a 422.
    """
    from app import repo_index as R
    from app import review_comments as RC

    base = R.extract("src/user.py",
                     "import os\n\nclass User:\n    email = ''\n    phone_number = ''\n")
    head = R.extract("src/user.py", "import os\n\nclass User:\n    email = ''\n")
    d = R.delta(base, head)

    assert d.removed == ("phone_number",), d.removed
    assert d.removed_at == {"phone_number": 5}, (
        f"the base line of the removed symbol was not established: {d.removed_at}")

    p = RC.plan([_pr_run(line=d.removed_at["phone_number"])])
    assert len(p.anchored) == 1 and not p.unanchored, p.summary()
    c = p.anchored[0]
    assert c.to_github() == {"path": "src/user.py", "line": 5, "side": "LEFT",
                             "body": c.body}, c.to_github()


def test_a_comment_with_no_anchor_is_not_given_an_invented_line():
    """A guessed line number is worse than no line number.

    If the base line could not be established -- an unparsable base, or a regex
    language where the definition was matched but not located -- the comment goes to
    pull-request level. Defaulting to line 1 would either 422 or land an
    authoritative-looking comment on an import statement.
    """
    from app import review_comments as RC

    p = RC.plan([_pr_run(line=0)])
    assert not p.anchored and len(p.unanchored) == 1, p.summary()
    assert p.unanchored[0].line == 0
    assert not p.unanchored[0].anchored

    # It still carries the full body -- unanchored is about placement, not content.
    assert "acme/checkout" in p.unanchored[0].body


def test_re_running_on_the_same_pull_request_updates_instead_of_duplicating():
    """`synchronize` fires on every push, so this runs repeatedly by design.

    Identity is an invisible marker naming the symbol and file, not the body text --
    matching on text would break precisely when a re-run changes the narrative, which
    is the only reason to re-run at all.
    """
    from app import review_comments as RC

    gh = _FakeGitHub()
    first = RC.post("acme/api", 7, "headsha", RC.plan([_pr_run()]), "tok", api=gh)
    assert first["created"] == ["phone_number"], first
    assert len(gh.review) == 1, gh.review

    # Second run, DIFFERENT body -- the note changed, as it would on a re-analysis.
    changed = _pr_run()
    changed["affected"][0]["note"] = "a completely different clause"
    second = RC.post("acme/api", 7, "headsha", RC.plan([changed]), "tok", api=gh)

    assert second["updated"] == ["phone_number"], second
    assert second["created"] == [], second
    assert len(gh.review) == 1, (
        f"a second comment was posted for the same symbol: {len(gh.review)} comments")
    assert "a completely different clause" in gh.review[0]["body"], (
        "the comment was not actually updated with the new body")

    patches = [c for c in gh.calls if c[0] == "PATCH"]
    assert len(patches) == 1, patches


def test_posting_refuses_when_existing_comments_cannot_be_read():
    """Failing to read is not the same as there being nothing there.

    If the existing-comment read fails and the code treats it as "no comments yet",
    every push duplicates every comment. That is the behaviour that gets an app
    uninstalled, so a read failure REFUSES to post and says why.
    """
    from app import review_comments as RC

    gh = _FakeGitHub(fail_read=True)
    out = RC.post("acme/api", 7, "headsha", RC.plan([_pr_run()]), "tok", api=gh)

    assert out["created"] == [] and out["updated"] == [], out
    assert out["refused"], "a read failure silently proceeded to post"
    assert "duplicat" in out["refused"][0], out["refused"]
    assert not any(m == "POST" for m, _p, _d in gh.calls), (
        "a comment was posted despite the read failing")


def test_the_comment_body_lists_only_files_the_index_found():
    """The ledger is template-written. The model contributes prose, never paths.

    A model that cannot be trusted to enumerate files is not asked to. The narrative
    is appended as a separate paragraph, so a model that names a file it invented adds
    a sentence -- it cannot add a row.
    """
    from app import review_comments as RC

    run = _pr_run()
    body = RC.body_for("phone_number", "src/user.py", run,
                       narrative="The consumer builds its payload from this field.")

    for c in run["affected"]:
        assert c["path"] in body, f"{c['path']} is missing from the comment"
    assert "acme/nonexistent" not in body

    rows = [ln for ln in body.splitlines() if ln.startswith("- `acme/")]
    assert len(rows) == len(run["affected"]), (
        f"the ledger has {len(rows)} rows for {len(run['affected'])} affected files")

    assert "exact match" in body and "approximate match" in body, (
        "confidence was not carried into the comment, so a regex guess and a parse "
        "result read identically")


def test_a_comment_states_what_was_not_searched():
    """A comment listing two consumers and silent about eight unindexed repos reads
    as a complete answer.

    Same property `impact.is_confident_none` protects, one layer out: by the time it
    is prose on a pull request, an omitted caveat is invisible.
    """
    from app import review_comments as RC

    partial = _pr_run(
        affected=[],
        unsearchable=[{"repo": "acme/legacy", "reason": "granted but never indexed"}],
        refusals=["acme/web was indexed up to its file cap"],
        partial=True)
    body = RC.body_for("phone_number", "src/user.py", partial)

    assert "Not established" in body, body
    assert "acme/legacy" in body and "unknown" in body
    assert "file cap" in body
    assert "Every granted repository was indexed" not in body, (
        "an incomplete search claimed a complete one")

    # And the honest complete case is allowed to say so, or the caveat carries no
    # signal because it is always present.
    clean = _pr_run(affected=[], unsearchable=[], refusals=[], partial=False)
    clean_body = RC.body_for("phone_number", "src/user.py", clean)
    assert "Every granted repository was indexed" in clean_body, clean_body
    assert "Not established" not in clean_body


def test_a_symbol_with_no_analysis_gets_no_comment_at_all():
    """Silence is better than a comment asserting something nobody checked.

    A run with no impact keys means the analysis never ran for that symbol. Rendering
    it would produce "No indexed file references it" -- a claim with nothing behind
    it, on someone's pull request.
    """
    from app import review_comments as RC

    p = RC.plan([{"symbol": "mystery", "path": "src/x.py", "line": 3}])
    assert not p.all_comments, p.summary()
    assert p.skipped and p.skipped[0]["symbol"] == "mystery", p.skipped
    assert "never" in p.skipped[0]["why"] or "no impact" in p.skipped[0]["why"]


def test_the_comment_cap_is_stated_rather_than_silently_applied():
    """A refactor removing 200 symbols must not bury the review under its output.

    The cap is fine; a cap that hides how much it dropped is not -- the reviewer
    would read 20 comments as the complete set.
    """
    from app import review_comments as RC

    runs = [_pr_run(symbol=f"sym_{i}") for i in range(RC.MAX_COMMENTS_PER_PR + 5)]
    p = RC.plan(runs)

    assert len(p.all_comments) == RC.MAX_COMMENTS_PER_PR, len(p.all_comments)
    assert p.truncated_at == RC.MAX_COMMENTS_PER_PR, p.truncated_at
    assert len(p.skipped) == 5, p.skipped
    assert "capped" in p.summary(), p.summary()


def test_review_comments_are_posted_from_the_real_pull_request_route():
    """Driven through the actual dispatch, for the reason Stage 3 established.

    Asserting the call appears in the source survived both `if False and ...` and a
    wrong-valued condition. So this drives `_handle_pr_opened` end to end and asserts
    a comment carrying the marker was actually POSTed.
    """
    import asyncio

    from app import repo_index as R
    from app import review_comments as RC
    from app import webhook as W

    BASE = "class User:\n    email = ''\n    phone_number = ''\n"
    HEAD = "class User:\n    email = ''\n"

    gh = _FakeGitHub()

    def api(method, path, token, data=None, **kw):
        if "/pulls/9/files" in path:
            return [{"filename": "src/user.py", "status": "modified"}]
        return gh(method, path, token, data, **kw)

    consumer = R.build("acme/checkout", [
        ("src/pay.py", "from models.user import User\n\ndef pay(u):\n"
                       "    return u.phone_number\n")])

    saved = (W._github_api, W._fetch_file_at_sha, W._get_token,
             W._repo_index.indexed_repos, W._granted_repos)
    W._github_api = api
    W._fetch_file_at_sha = lambda r, p, sha, t: (
        None if p != "src/user.py" else (BASE if sha == "b1" else HEAD))
    W._get_token = lambda _i: "tok"
    W._repo_index.indexed_repos = lambda: [consumer]
    W._granted_repos = lambda _i: ["acme/api", "acme/checkout"]
    try:
        payload = {"action": "opened",
                   "repository": {"full_name": "acme/api", "default_branch": "main"},
                   "installation": {"id": 42},
                   "pull_request": {"number": 9, "base": {"sha": "b1"},
                                    "head": {"sha": "h2"}}}
        res = asyncio.new_event_loop().run_until_complete(
            W._handle_pr_opened(payload, payload["pull_request"]))
    finally:
        (W._github_api, W._fetch_file_at_sha, W._get_token,
         W._repo_index.indexed_repos, W._granted_repos) = saved

    assert res["status"] == "analysed", res
    assert res.get("review"), f"no review was attempted: {res.get('review')}"
    assert res["review"].get("created") == ["phone_number"], res["review"]

    posted = [d for m, p, d in gh.calls if m == "POST" and p.endswith("/reviews")]
    assert posted, f"no review was POSTed: {[c[:2] for c in gh.calls]}"
    comments = posted[0]["comments"]
    assert len(comments) == 1, comments
    assert comments[0]["side"] == "LEFT" and comments[0]["line"] == 3, comments[0]
    assert RC.marker_in(comments[0]["body"]) == ("phone_number", "src/user.py")
    assert "acme/checkout" in comments[0]["body"], comments[0]["body"]


def test_a_failure_to_comment_does_not_discard_the_analysis():
    """The expensive part is the cross-repo search. The cheap part is the comment.

    A token without `pull_requests: write` must not throw away a completed analysis,
    and the reason must be RETURNED rather than swallowed -- a permissions problem
    that renders as "nothing to say" is indistinguishable from a clean result.

    Both halves inject a fake API. The first version of this test left `_github_api`
    real for its happy-path sanity check and made two live requests to api.github.com
    with a bogus token; they failed, `existing_markers` refused, and the assertion
    `assert out` was satisfied by that refusal. It passed for the wrong reason and was
    network-dependent.
    """
    from app import webhook as W

    def exploding(*_a, **_k):
        raise RuntimeError("403 Resource not accessible by integration")

    gh = _FakeGitHub()
    saved_api = W._github_api
    W._github_api = gh
    try:
        ok = W._post_review_comments("acme/api", 7, "sha", [_pr_run()], "tok")
        assert ok.get("created") == ["phone_number"], (
            f"the happy path did not post: {ok}")

        saved_post = W._review_comments.post
        W._review_comments.post = exploding
        try:
            failed = W._post_review_comments("acme/api", 7, "sha", [_pr_run()], "tok")
        finally:
            W._review_comments.post = saved_post
    finally:
        W._github_api = saved_api

    assert failed["status"] == "post_failed", failed
    assert "403" in failed["error"], failed
    assert failed["status"] != "nothing_to_say", (
        "a permissions failure was reported as having nothing to say")


def test_no_gate_reaches_the_real_github_api():
    """No test may make a live request. Asserted, not trusted.

    Written because one did: the failure-to-comment gate above left `_github_api`
    unpatched and hit api.github.com twice per run. A network-dependent test is flaky
    in CI, and one that passes BECAUSE the request failed is worse than no test.
    """
    from app import webhook as W

    attempted = []
    saved = W._github_api
    W._github_api = lambda m, p, t, d=None, **k: (
        attempted.append((m, p)) or {"error": "no network in tests"})
    try:
        for name in ("test_a_failure_to_comment_does_not_discard_the_analysis",
                     "test_review_comments_are_posted_from_the_real_pull_request_route",
                     "test_re_running_on_the_same_pull_request_updates_instead_of_"
                     "duplicating"):
            globals()[name]()
    finally:
        W._github_api = saved

    assert attempted == [], (
        f"a gate called the module-level GitHub client instead of an injected fake, "
        f"which in CI is a live request: {attempted}")


def test_a_regex_language_reports_the_line_the_symbol_is_actually_on():
    """The anchor for a non-Python file must be right, and nothing else covered it.

    Written because a mutation survived. The anchor gate uses a `.py` file, so it
    exercises the AST extractor only -- the regex path had no line-number coverage at
    all, and the bug it hides is subtle: every `_REGEX_DEFINES` pattern opens with
    `^\\s*`, and `\\s` matches a newline, so a definition preceded by a blank line
    matches STARTING on that blank line. `export function bar` on line 3 reported as
    line 2.

    One line early is not a rounding error. GitHub rejects a comment on a line that
    is not in the diff, and when it does not reject it, the comment lands on
    unrelated code while looking authoritative.
    """
    from app import repo_index as R

    ts = R.extract("a.ts", "export const Foo = 1\n"        # line 1
                           "\n"                            # line 2 -- blank
                           "export function bar() {}\n"     # line 3
                           "\n"                            # line 4
                           "export class Baz {}\n")         # line 5
    assert ts.method == "regex", ts.method
    assert ts.defines_at.get("Foo") == 1, ts.defines_at
    assert ts.defines_at.get("bar") == 3, (
        f"a definition preceded by a blank line was located one line early: "
        f"{ts.defines_at}")
    assert ts.defines_at.get("Baz") == 5, ts.defines_at

    # Same shape in a second regex language, so the fix is in the shared path.
    go = R.extract("m.go", "package m\n\n\nfunc Handle() {}\n")
    assert go.defines_at.get("Handle") == 4, go.defines_at

    # And the line survives a persistence round trip, since the anchor is read back
    # from the stored index rather than recomputed at comment time.
    idx = R.build("acme/web", [("a.ts", "export const Foo = 1\n\n"
                                        "export function bar() {}\n")])
    back = R.RepoIndex.from_dict(idx.to_dict())
    assert back.files["a.ts"].defines_at == {"Foo": 1, "bar": 3}, (
        f"line numbers were lost or corrupted on round trip: "
        f"{back.files['a.ts'].defines_at}")


def test_installing_indexes_repository_paths_without_the_archive_wrapper():
    """The install path had never run for real, and it was wrong in two ways.

    `fetch_tree` returns (tree, cleanup_root): GitHub wraps an archive in a
    `{owner}-{repo}-{sha}/` directory, so `tree` is the repository root and
    `cleanup_root` is the temp parent the CALLER must remove.

    Walking the parent made every indexed path carry the wrapper --
    `tree/owner-repo-abc123/src/pay.py` instead of `src/pay.py`. Nothing local catches
    that: the index stays self-consistent, symbol matching still works, the comment
    renders. It breaks exactly where it is most visible -- the comment names a file
    that does not exist in the repository, and a follow-on pull request tries to edit
    that nonexistent path.

    Not walking with a `finally` also leaked one temp tree per repository, and an
    installation is the one moment this runs up to MAX_REPOS times in a row.

    Driven with a REAL wrapped directory, because a flat fixture cannot tell the two
    paths apart and would pass either way.
    """
    import os
    import shutil
    import tempfile

    from app import repo_index as R
    from app import webhook as W

    parent = tempfile.mkdtemp(prefix="ripple-test-tree-")
    wrapped = os.path.join(parent, "tree", "acme-checkout-abc123")
    os.makedirs(os.path.join(wrapped, "src"))
    with open(os.path.join(wrapped, "src", "pay.py"), "w") as fh:
        fh.write("def pay(u):\n    return u.phone_number\n")

    # A file OUTSIDE the wrapper. Without it the gate cannot fail: walking the temp
    # parent finds exactly the same file set as walking the tree, so a mutant that
    # walks the wrong root still produces the right answer. The first version of this
    # test had only the wrapped file and both mutants survived it.
    with open(os.path.join(parent, "STRAY.py"), "w") as fh:
        fh.write("SHOULD_NOT_BE_INDEXED = 1\n")

    import app.repo_workspace as RW
    saved = (RW.fetch_tree, R.save)
    stored = {}
    RW.fetch_tree = lambda repo, ref, token, **kw: (wrapped, parent)
    R.save = lambda idx: stored.setdefault("idx", idx) and ""
    try:
        stats = W._build_symbol_index("acme/checkout", "tok")
        # Checked BEFORE this test does its own cleanup. The first version asserted
        # after the finally block that removed `parent` itself, so the leak assertion
        # could never fail -- the same unfalsifiable-gate shape as the Stage 3
        # reachability checks.
        leaked = os.path.exists(parent)
    finally:
        RW.fetch_tree, R.save = saved
        shutil.rmtree(parent, ignore_errors=True)

    assert stats["status"] == "indexed", stats
    idx = stored["idx"]
    paths = sorted(idx.files)
    assert paths == ["src/pay.py"], (
        f"indexed paths carry the archive wrapper or reach outside the repository "
        f"tree, so a comment would name a file that does not exist: {paths}")
    assert not any("acme-checkout-abc123" in p for p in paths), paths
    assert not any("STRAY" in p or p.startswith("..") for p in paths), (
        f"a file outside the repository tree was indexed: {paths}")

    assert not leaked, (
        "the temp tree was leaked; an install runs this once per granted repository, "
        "up to MAX_REPOS times in a row")


class _FakeForkAPI:
    """Stands in for the GitHub API across the fork/branch/PR sequence."""

    def __init__(self, *, login="ripplebot", upstream_info=None, fork_exists=False,
                 fork_ready_after=0, open_prs=None, fail_pr_list=False,
                 upstream_sha="upstreamsha1234", fork_sha="STALEforksha0000",
                 fail_ref=False, fail_commit=False, fail_pr=False):
        self.calls = []
        self.login = login
        self.upstream_info = upstream_info if upstream_info is not None else {
            "default_branch": "main", "archived": False}
        self.fork_exists = fork_exists
        self.fork_ready_after = fork_ready_after   # polls before the fork answers
        self._polls = 0
        self.open_prs = list(open_prs or [])
        self.fail_pr_list = fail_pr_list
        self.upstream_sha = upstream_sha
        #: The FORK's own default-branch head, DELIBERATELY different from the
        #: upstream's.
        #:
        #: WHY THIS FIELD EXISTS. Without it this fake returned `upstream_sha` for
        #: EVERY /git/ref/heads/ path, fork or upstream -- so a branch cut from the
        #: fork's stale default was byte-identical to one cut from the upstream head,
        #: and test_a_fork_branch_is_cut_from_the_upstream_sha_not_the_fork_default
        #: passed either way. Measured by mutation on 2026-09-13: rewriting
        #: open_fork_pr to read the fork's ref left that gate GREEN, so the gate whose
        #: entire name is that property had been asserting nothing about it since it
        #: was written. A fake that cannot express the difference cannot test it.
        self.fork_sha = fork_sha
        self.fail_ref = fail_ref
        self.fail_commit = fail_commit
        self.fail_pr = fail_pr
        self.created_refs = []
        self.committed = []
        #: (path, payload) per commit. Kept ALONGSIDE `committed` rather than
        #: replacing it, because the existing gates compare `committed` as a list of
        #: paths -- widening that field would have changed six assertions to prove
        #: one new thing. This records the bytes, which is what an end-to-end check
        #: needs in order to assert the committed content is the deterministic fix
        #: and not something a model produced.
        self.commits = []
        self.pr_payload = None

    def __call__(self, method, path, token, data=None, **kw):
        self.calls.append((method, path, data))

        if method == "GET" and path == "/user":
            return {"login": self.login}
        if method == "GET" and path.startswith("/repos/") and path.count("/") == 3:
            owner = path.split("/")[2]
            if owner == self.login:                     # the fork
                if self.fork_exists:
                    return {"default_branch": "main"}
                if self._polls >= self.fork_ready_after:
                    return {"default_branch": "main"}
                self._polls += 1
                return {"error": "404 Not Found"}
            return dict(self.upstream_info)              # the upstream
        if method == "POST" and path.endswith("/forks"):
            return {"full_name": f"{self.login}/repo"}
        if method == "GET" and "/pulls?state=open" in path:
            return {"error": "boom"} if self.fail_pr_list else list(self.open_prs)
        if method == "GET" and "/git/ref/heads/" in path:
            # The OWNER decides which sha comes back. Returning upstream_sha for both
            # is what made the branch-provenance gate vacuous -- see fork_sha above.
            if f"/repos/{self.login}/" in path:
                return ({"error": "404"} if not self.fork_sha
                        else {"object": {"sha": self.fork_sha}})
            return ({"error": "404"} if not self.upstream_sha
                    else {"object": {"sha": self.upstream_sha}})
        if method == "POST" and path.endswith("/git/refs"):
            if self.fail_ref:
                return {"error": "422 Reference already exists"}
            self.created_refs.append((path, data))
            return {"ref": data["ref"]}
        if method == "GET" and "/contents/" in path:
            return {"error": "404 Not Found"}            # new file
        if method == "PUT" and "/contents/" in path:
            if self.fail_commit:
                return {"error": "403 Forbidden"}
            self.committed.append(path)
            self.commits.append((path, data))
            return {"commit": {"sha": "newcommit"}}
        if method == "POST" and path.endswith("/pulls"):
            if self.fail_pr:
                return {"error": "422 Validation Failed"}
            self.pr_payload = data
            return {"number": 7, "html_url": f"https://github.com/{path.split('/')[2]}"
                                             f"/{path.split('/')[3]}/pull/7"}
        return {"error": f"unexpected {method} {path}"}


def test_a_raising_github_client_becomes_a_refusal_not_an_aborted_run():
    """fork_pr's failure contract is "return an error", but pr_engine's client RAISES.

    Found by RUNNING the firing path, not by any gate -- which is why this test exists.
    Every function in fork_pr checks `result.get("error")` and turns it into a stated
    refusal, and that is what makes its documented property true: "one archived
    repository does not stop a run over twenty". But `pr_engine._github_request` raises
    on any non-2xx, so passing it in directly turns the first refusal into an unhandled
    exception that aborts the whole run. The Stage 4 CLI wiring did exactly that, and
    the fake-api gates could not see it because a fake returns errors by construction.

    `error_returning_api` adapts at the boundary rather than changing
    `_github_request`, whose same-repo callers depend on it raising.
    """
    from app import fork_pr as F

    calls = []

    def raising(method, path, token, data=None, **kw):
        calls.append((method, path))
        raise Exception(f"GitHub API {method} {path}: 404 Not Found")

    # 1. Unadapted, the run explodes -- this is the defect, pinned so it cannot return.
    try:
        F.open_fork_pr("acme/gone", "b", "t", "body", [("a.py", "eA==")], "tok",
                       api=raising, sleep=lambda _s: None)
        unadapted_raised = False
    except Exception:                                              # noqa: BLE001
        unadapted_raised = True
    assert unadapted_raised, (
        "the raising client no longer raises, so this test proves nothing -- if "
        "_github_request was changed to return errors, delete the adapter instead")

    # 2. Adapted, the same failure is a REFUSAL and the run completes.
    calls.clear()
    res = F.open_fork_pr("acme/gone", "b", "t", "body", [("a.py", "eA==")], "tok",
                         api=F.error_returning_api(raising), sleep=lambda _s: None)
    assert not res.opened, "a 404 upstream somehow produced an opened pull request"
    assert res.refusals, "the failure produced no stated refusal"
    assert "404" in " ".join(res.refusals), (
        f"the refusal drops the status, so the cause is unrecoverable: {res.refusals}")
    assert calls, "the adapter swallowed the call entirely"

    # 3. And a multi-repo run must SURVIVE one bad repo -- the actual property.
    run = F.open_fork_prs(
        {"acme/gone": [("a.py", "eA==")], "acme/also-gone": [("b.py", "eA==")]},
        branch="b", title="t", body="body", token="tok",
        api=F.error_returning_api(raising), dry_run=False)
    assert len(run.failed) == 2, (
        f"a failing repo aborted the run instead of being recorded: {run.summary()}")
    assert not run.opened

    # 4. The CLI must USE the adapter -- an unadapted call site reintroduces the bug.
    #    Checked at the `api=` ARGUMENT, not anywhere in the function: the import line
    #    also contains the name, so a bare substring check passed while the call site
    #    had been reverted to the raising client. Caught by mutation, and the same
    #    too-loose-substring shape as the skip-reason gate.
    import inspect

    from app import cli
    src = inspect.getsource(cli._run_fork_follow_ons)
    assert "api=error_returning_api(" in src, (
        "the CLI passes a raising client straight to open_fork_prs, so the first "
        "unreachable repository aborts the whole run")


def test_a_fork_pr_resolves_end_to_end_with_no_model_and_commits_the_template_fix():
    """The whole path, model-free: source in -> fork PR payload out. Added 2026-09-13.

    The seven sibling gates above each pin ONE property of `open_fork_pr` against a
    fake. This pins the property none of them can see: that the bytes which reach the
    commit are the ones the DETERMINISTIC template produced, with no model configured
    and no model reachable.

    That matters because of where this runs. Railway has no model at all -- no local
    Ollama, and the hosted providers are refused by the data boundary unless the
    operator opts in. So if any part of this path needed inference, the deployed
    service would resolve a fork, create a branch, and then commit nothing.

    The model entry points are replaced with raisers rather than merely left
    unconfigured: "no model was configured" and "no model was called" are different
    claims, and only the second one is what this asserts.
    """
    import base64
    import importlib
    import os as _os

    keys = ("ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN", "RIPPLE_ALLOW_PAID_MODEL",
            "RIPPLE_ALLOW_HOSTED_MODEL", "RIPPLE_SELF_HOSTED")
    saved = {k: _os.environ.get(k) for k in keys}

    import app.fix_generator as fg
    import app.llm_chain as lc

    real_llm, real_run = fg._generate_with_llm, lc.run

    def _boom(*a, **k):
        raise AssertionError("a model was called on the deterministic path")

    try:
        for k in keys:
            _os.environ.pop(k, None)
        import app.llm_config as L
        importlib.reload(L)
        assert not L.is_configured()

        fg._generate_with_llm = _boom
        lc.run = _boom

        from app.fix_templates import apply_fix_template
        from app.fork_pr import open_fork_pr

        # The locked target's shape: a removed numpy alias, reached as an attribute.
        original = (
            "import numpy as np\n"
            "\n"
            "def check(kwargs, keys, names):\n"
            "    a = np.alltrue([isinstance(v, list) for v in kwargs.values()])\n"
            "    b = np.alltrue([v in names for v in keys])\n"
            "    return a and b\n"
        )
        fixed, _ = apply_fix_template(
            code=original, language="python", change_type="field_renamed",
            field_name="alltrue", new_name="all")
        assert fixed != original and "np.alltrue" not in fixed

        rel = "do_mpc/sampling/_samplingplanner.py"
        gh = _FakeForkAPI(fork_exists=True, upstream_sha="realupstreamsha99")
        result = open_fork_pr(
            upstream="do-mpc/do-mpc", branch="ripple/field_renamed-alltrue",
            title="fix: rename np.alltrue to np.all (removed in NumPy 2.0)",
            body="body",
            edits=[(rel, base64.b64encode(fixed.encode()).decode())],
            token="t", api=gh, sleep=lambda s: None)

        assert result.opened, f"resolution failed: {result.refusals}"

        # The committed BYTES are the template's output -- the thing no sibling
        # gate checks.
        assert len(gh.commits) == 1, f"{len(gh.commits)} commits, expected 1"
        cpath, cbody = gh.commits[0]
        assert gh.login in cpath and "do-mpc/do-mpc" not in cpath, (
            f"content was committed outside the fork: {cpath}")
        sent = base64.b64decode(cbody["content"]).decode()
        assert sent == fixed, "the committed bytes are not the template's output"
        assert "np.alltrue" not in sent, "the removed alias was committed"
        assert cbody.get("branch") == "ripple/field_renamed-alltrue"

        # And the branch/PR shape still holds on this same run, so the end-to-end
        # path cannot pass while the boundary properties regress.
        assert gh.created_refs, "no branch was created"
        _, ref_body = gh.created_refs[0]
        assert ref_body["sha"] == "realupstreamsha99", (
            "the branch was not cut from the upstream sha")
        assert ref_body["sha"] != gh.fork_sha, (
            "the branch was cut from the fork's own stale default branch")
        assert gh.pr_payload["head"] == f"{gh.login}:ripple/field_renamed-alltrue"
        assert gh.pr_payload["base"] == "main"

        # Still no model, and none was reachable at any point.
        assert not L.is_configured()
    finally:
        fg._generate_with_llm = real_llm
        lc.run = real_run
        for k, v in saved.items():
            _os.environ.pop(k, None)
            if v is not None:
                _os.environ[k] = v
        import app.llm_config as L
        importlib.reload(L)


def test_a_fork_branch_is_cut_from_the_upstream_sha_not_the_fork_default():
    """The bug that gets a bot blocked.

    If the fork already exists and is 200 commits behind, branching from the FORK's
    default branch produces a pull request whose diff contains every intervening
    upstream commit -- hundreds of unrelated files. The branch must be cut from the
    UPSTREAM head sha, which is reachable inside the fork because a fork shares its
    object store with the parent.

    THIS TEST WAS VACUOUS UNTIL 2026-09-13. `_FakeForkAPI` returned `upstream_sha` for
    every /git/ref/heads/ path, so a branch cut from the fork's stale default was
    byte-identical to one cut from the upstream head and this assertion passed either
    way. Proven by mutation: rewriting open_fork_pr to read the fork's own ref left
    this gate green. The fake now serves a DISTINCT `fork_sha`, and the wrong-source
    assertion below is what makes the difference observable.
    """
    from app import fork_pr as F

    gh = _FakeForkAPI(fork_exists=True, upstream_sha="deadbeefcafe",
                      fork_sha="STALEforkhead0")
    res = F.open_fork_pr("acme/upstream", "ripple/drop-x", "fix: drop x", "body",
                         [("src/pay.py", "Y29udGVudA==")], "tok", api=gh,
                         sleep=lambda _s: None)

    assert res.opened, res.refusals
    assert gh.created_refs, "no branch was created"
    _, ref_body = gh.created_refs[0]
    assert ref_body["sha"] == "deadbeefcafe", (
        f"the branch was cut from {ref_body['sha']!r}, not the upstream head")
    assert ref_body["sha"] != gh.fork_sha, (
        "the branch was cut from the FORK's own default branch -- on a fork that is "
        "behind, the pull request would carry every intervening upstream commit")
    assert gh.created_refs, "no branch was created"
    _path, payload = gh.created_refs[0]
    assert payload["sha"] == "deadbeefcafe", (
        f"the branch was cut from {payload['sha']!r}, not the upstream head -- a stale "
        f"fork would put every intervening upstream commit in the diff")
    # The fork of acme/upstream is ripplebot/upstream -- the NAME is inherited, the
    # owner is the token's account.
    assert _path == "/repos/ripplebot/upstream/git/refs", (
        f"the branch was created on the wrong repository: {_path}")


def test_the_pull_request_crosses_the_fork_boundary_and_targets_the_upstream():
    """`head` must be `owner:branch`, and the PR must be POSTed to the UPSTREAM.

    Posting to the fork opens a pull request from the fork to itself, which nobody
    ever sees. A bare branch name in `head` is a 422 across forks.
    """
    from app import fork_pr as F

    gh = _FakeForkAPI(fork_exists=True)
    res = F.open_fork_pr("acme/upstream", "ripple/drop-x", "fix: drop x", "body",
                         [("src/pay.py", "Y29udGVudA==")], "tok", api=gh,
                         sleep=lambda _s: None)

    assert res.opened, res.refusals
    assert gh.pr_payload["head"] == "ripplebot:ripple/drop-x", gh.pr_payload
    assert gh.pr_payload["base"] == "main", gh.pr_payload

    pr_posts = [p for m, p, _d in gh.calls if m == "POST" and p.endswith("/pulls")]
    assert pr_posts == ["/repos/acme/upstream/pulls"], (
        f"the pull request was not opened against the upstream: {pr_posts}")

    # And the commit went to the FORK, never the upstream.
    assert all("/repos/ripplebot/" in p for p in gh.committed), gh.committed


def test_an_async_fork_is_waited_for_and_refused_if_it_never_appears():
    """`POST /forks` returns 202 -- queued, not done.

    Creating a ref against a fork that has not materialised fails in a way that reads
    like a bad sha. And if it never appears, that must be a stated refusal rather than
    a confusing downstream error.
    """
    from app import fork_pr as F

    slept = []
    gh = _FakeForkAPI(fork_exists=False, fork_ready_after=3)
    fork = F.ensure_fork("acme/upstream", "tok", api=gh, sleep=slept.append)
    assert fork.usable, fork.refusals
    assert fork.created and fork.ready
    assert slept, "the fork was assumed ready immediately -- no polling happened"

    never = _FakeForkAPI(fork_exists=False, fork_ready_after=10**6)
    out = F.ensure_fork("acme/upstream", "tok", api=never, sleep=lambda _s: None)
    assert not out.usable
    assert any("UNKNOWN" in r for r in out.refusals), out.refusals
    assert any("nothing was pushed" in r for r in out.refusals), out.refusals


def test_a_failed_pr_list_read_does_not_open_a_duplicate():
    """`synchronize` fires on every push, so this runs repeatedly.

    A failed read is treated as "already open" on purpose: duplicating pull requests
    on a repository you do not own is how an integration gets banned, and it is not
    recoverable by the person who did it.
    """
    from app import fork_pr as F

    blind = _FakeForkAPI(fork_exists=True, fail_pr_list=True)
    assert F.existing_pr_for_head("acme/upstream", "ripplebot:b", "tok", api=blind)

    res = F.open_fork_pr("acme/upstream", "ripple/drop-x", "t", "b",
                         [("a.py", "eA==")], "tok", api=blind, sleep=lambda _s: None)
    assert res.already_open and not res.opened, res
    assert not blind.committed, "content was committed despite the duplicate guard"

    seen = _FakeForkAPI(fork_exists=True,
                        open_prs=[{"head": {"label": "ripplebot:ripple/drop-x"}}])
    again = F.open_fork_pr("acme/upstream", "ripple/drop-x", "t", "b",
                           [("a.py", "eA==")], "tok", api=seen, sleep=lambda _s: None)
    assert again.already_open and not again.opened, again


def test_an_archived_upstream_is_refused_before_anything_is_forked():
    """Discovering an archived repo after forking litters the user's account.

    Also the reason the check is a refusal with a sentence rather than a bare False --
    a 403 surfacing three calls deeper reads like a credential problem.
    """
    from app import fork_pr as F

    gh = _FakeForkAPI(upstream_info={"default_branch": "main", "archived": True})
    res = F.open_fork_pr("acme/dead", "ripple/x", "t", "b", [("a.py", "eA==")],
                         "tok", api=gh, sleep=lambda _s: None)

    assert not res.opened
    assert any("archived" in r for r in res.refusals), res.refusals
    assert not any(p.endswith("/forks") for _m, p, _d in gh.calls), (
        "a fork was created for an archived repository")

    off = _FakeForkAPI(upstream_info={"default_branch": "main",
                                      "has_pull_requests": False})
    res2 = F.open_fork_pr("acme/noprs", "ripple/x", "t", "b", [("a.py", "eA==")],
                          "tok", api=off, sleep=lambda _s: None)
    assert any("pull requests disabled" in r for r in res2.refusals), res2.refusals


def test_no_pull_request_is_opened_when_nothing_committed():
    """An empty pull request wastes a maintainer's attention, which is the whole
    resource this strategy spends."""
    from app import fork_pr as F

    gh = _FakeForkAPI(fork_exists=True, fail_commit=True)
    res = F.open_fork_pr("acme/upstream", "ripple/x", "t", "b",
                         [("a.py", "eA==")], "tok", api=gh, sleep=lambda _s: None)

    assert not res.opened
    assert any("no file was committed" in r for r in res.refusals), res.refusals
    assert not any(p.endswith("/pulls") and m == "POST"
                   for m, p, _d in gh.calls), "an empty pull request was opened"


def test_a_rejected_pull_request_says_the_branch_still_exists():
    """The branch and commits are real even when the PR is rejected.

    Reporting only "failed" would send someone hunting for a commit that is sitting
    on their fork, and a retry would hit the already-exists path confused.
    """
    from app import fork_pr as F

    gh = _FakeForkAPI(fork_exists=True, fail_pr=True)
    res = F.open_fork_pr("acme/upstream", "ripple/x", "t", "b",
                         [("a.py", "eA==")], "tok", api=gh, sleep=lambda _s: None)

    assert not res.opened
    assert any("exist on" in r and "rejected" in r for r in res.refusals), res.refusals
    assert gh.committed, "the test did not reach the commit step"


def test_an_approximate_match_is_never_turned_into_a_commit():
    """The bar for editing someone else's repo is higher than for mentioning it.

    A regex hit is good enough to say "this may need a look" in a comment and not
    good enough to open a pull request against. Stage 5 reports both; Stage 6 edits
    only what was parsed.
    """
    from app import follow_on as F

    impact = {
        "affected": [
            {"repo": "acme/checkout", "path": "src/pay.py", "language": "python",
             "confidence": "exact"},
            {"repo": "acme/web", "path": "app/P.tsx", "language": "typescript",
             "confidence": "approximate"},
        ],
        "partial": False, "unsearchable": [],
    }
    p = F.plan_follow_ons("phone_number", "acme/api", 7, impact)

    repos = [x.repo for x in p.proposals]
    assert repos == ["acme/checkout"], repos
    assert any(m["repo"] == "acme/web" and "approximate" in m["why"]
               for m in p.mentioned_only), p.mentioned_only
    assert all(x.exact_only for x in p.proposals)


def test_the_originating_repo_does_not_get_a_rival_pull_request():
    """It is already fixing itself in the pull request under review.

    Opening a second PR against the same repo would race the one being reviewed and
    conflict with it.
    """
    from app import follow_on as F

    impact = {"affected": [
        {"repo": "acme/api", "path": "src/other.py", "language": "python",
         "confidence": "exact"}],
        "partial": False, "unsearchable": []}
    p = F.plan_follow_ons("phone_number", "acme/api", 7, impact)

    assert p.proposals == [], [x.repo for x in p.proposals]
    assert "same repository" in p.mentioned_only[0]["why"], p.mentioned_only


def test_one_follow_on_per_repo_not_one_per_file():
    """Three files in one repo is one change, not three reviews.

    Also what makes idempotency tractable: the branch is derived from the symbol, so
    a re-run finds its own branch rather than opening a rival.
    """
    from app import follow_on as F

    impact = {"affected": [
        {"repo": "acme/checkout", "path": f"src/f{i}.py", "language": "python",
         "confidence": "exact"} for i in range(3)],
        "partial": False, "unsearchable": []}
    p = F.plan_follow_ons("phone_number", "acme/api", 7, impact)

    assert len(p.proposals) == 1, [x.repo for x in p.proposals]
    assert len(p.proposals[0].files) == 3
    for i in range(3):
        assert f"src/f{i}.py" in p.proposals[0].body

    b1 = F.branch_name("phone_number", "acme/api", 7)
    b2 = F.branch_name("phone_number", "acme/api", 7)
    assert b1 == b2 and b1 == p.proposals[0].branch, (b1, b2)


def test_a_follow_on_says_when_the_upstream_search_was_incomplete():
    """It still raises what it found -- withholding a real consumer helps nobody --
    but the body must not present a partial set as the full blast radius."""
    from app import follow_on as F

    impact = {"affected": [
        {"repo": "acme/checkout", "path": "src/pay.py", "language": "python",
         "confidence": "exact"}],
        "partial": True,
        "unsearchable": [{"repo": "acme/legacy", "reason": "never indexed"}]}
    p = F.plan_follow_ons("phone_number", "acme/api", 7, impact)

    assert len(p.proposals) == 1
    body = p.proposals[0].body
    assert "incomplete" in body, body
    assert "never indexed" in body or "were never indexed" in body, body


def test_a_failed_read_does_not_open_a_duplicate_pull_request():
    """`synchronize` fires on every push. Without this, each push opens another PR in
    every implicated repository -- the fastest way to get an app banned.

    A FAILED read is treated as "already open" on purpose: the safe direction when
    the alternative is spamming a repository the author does not own.
    """
    from app import follow_on as F

    impact = {"affected": [
        {"repo": "acme/checkout", "path": "src/pay.py", "language": "python",
         "confidence": "exact"}], "partial": False, "unsearchable": []}
    prop = F.plan_follow_ons("phone_number", "acme/api", 7, impact).proposals[0]

    assert F.already_open(prop, "tok", api=lambda *a, **k: {"error": "boom"}), (
        "a failed read was treated as 'nothing open', which duplicates on every push")
    assert F.already_open(
        prop, "tok", api=lambda *a, **k: [{"head": {"ref": prop.branch}}])
    assert not F.already_open(
        prop, "tok", api=lambda *a, **k: [{"head": {"ref": "someone/else"}}])


def test_the_follow_on_repo_cap_is_stated():
    """Beyond the cap a human should decide, and the reviewer must be told the number
    they can see is not the number affected."""
    from app import follow_on as F

    impact = {"affected": [
        {"repo": f"acme/r{i}", "path": "src/x.py", "language": "python",
         "confidence": "exact"} for i in range(F.MAX_FOLLOW_ON_REPOS + 3)],
        "partial": False, "unsearchable": []}
    p = F.plan_follow_ons("phone_number", "acme/api", 7, impact)

    assert len(p.proposals) == F.MAX_FOLLOW_ON_REPOS, len(p.proposals)
    assert p.truncated_at == F.MAX_FOLLOW_ON_REPOS
    assert "capped" in p.summary(), p.summary()


def test_a_gitlab_merge_request_refuses_rather_than_guessing_a_base():
    """GitLab parity routes into the SAME analysis, and refuses on a missing base.

    A merge request payload without both revisions has nothing to subtract. Inventing
    a base would make every symbol look either removed or untouched depending on
    which way it guessed -- confident, and wrong in one direction or the other.
    """
    import asyncio

    from app import webhook as W

    run = asyncio.new_event_loop().run_until_complete

    ok = run(W._handle_gitlab_merge_request({
        "project": {"path_with_namespace": "grp/api"},
        "object_attributes": {"action": "open", "iid": 4,
                              "diff_refs": {"base_sha": "b1", "head_sha": "h2"}}}))
    assert ok["status"] == "accepted", ok
    assert ok["platform"] == "gitlab" and ok["mr"] == 4, ok
    assert "NOT wired" in ok["note"], (
        "the GitLab path claimed more than it does -- the file-fetch adapter is "
        "still missing and the response must say so")

    no_base = run(W._handle_gitlab_merge_request({
        "project": {"path_with_namespace": "grp/api"},
        "object_attributes": {"action": "open", "iid": 4}}))
    assert no_base["status"] == "refused", no_base
    assert "UNKNOWN rather than none" in no_base["why"], no_base

    ignored = run(W._handle_gitlab_merge_request({
        "project": {"path_with_namespace": "grp/api"},
        "object_attributes": {"action": "close", "iid": 4}}))
    assert ignored["status"] == "ignored", ignored


def test_follow_ons_are_planned_from_the_real_pull_request_route():
    """Reachability driven, not asserted structurally -- the Stage 3 lesson."""
    import asyncio

    from app import repo_index as R
    from app import webhook as W

    BASE = "class User:\n    email = ''\n    phone_number = ''\n"
    HEAD = "class User:\n    email = ''\n"
    gh = _FakeGitHub()

    def api(method, path, token, data=None, **kw):
        if "/pulls/21/files" in path:
            return [{"filename": "src/user.py", "status": "modified"}]
        return gh(method, path, token, data, **kw)

    consumer = R.build("acme/checkout", [
        ("src/pay.py", "from models.user import User\n\ndef pay(u):\n"
                       "    return u.phone_number\n")])

    saved = (W._github_api, W._fetch_file_at_sha, W._get_token,
             W._repo_index.indexed_repos, W._granted_repos)
    W._github_api = api
    W._fetch_file_at_sha = lambda r, p, s, t: (
        None if p != "src/user.py" else (BASE if s == "b1" else HEAD))
    W._get_token = lambda _i: "tok"
    W._repo_index.indexed_repos = lambda: [consumer]
    W._granted_repos = lambda _i: ["acme/api", "acme/checkout"]
    try:
        payload = {"action": "opened",
                   "repository": {"full_name": "acme/api", "default_branch": "main"},
                   "installation": {"id": 42},
                   "pull_request": {"number": 21, "base": {"sha": "b1"},
                                    "head": {"sha": "h2"}}}
        res = asyncio.new_event_loop().run_until_complete(
            W._handle_pr_opened(payload, payload["pull_request"]))
    finally:
        (W._github_api, W._fetch_file_at_sha, W._get_token,
         W._repo_index.indexed_repos, W._granted_repos) = saved

    fo = res.get("follow_ons")
    assert fo, f"follow-on planning never ran: {res.keys()}"
    assert [p["repo"] for p in fo["proposals"]] == ["acme/checkout"], fo
    assert fo["proposals"][0]["exact_only"] is True, fo
    assert "src/pay.py" in fo["proposals"][0]["files"], fo


def test_the_symbol_index_records_exact_versus_approximate():
    """A regex hit and an AST hit must not present with the same confidence.

    Python is parsed with `ast`, which is exact. TypeScript, Go and the rest are
    regex-matched, which is not -- a symbol inside a comment or a template literal can
    be missed or over-matched. Stage 4 ranks candidates, and a ranker that cannot tell
    a parse fact from a pattern guess will show a false positive with full confidence.

    Same reasoning as `source_regions.SCANNED` declaring which languages have a real
    scanner instead of pretending they all do.
    """
    from app import repo_index as R

    py = R.extract("src/models.py", "class User:\n    pass\n\nMAX = 5\n")
    assert py.method == "ast", py.method
    assert "User" in py.defines
    assert "MAX" in py.defines, (
        "module-level constants are exported symbols; a self-test on this repo caught "
        "FORBIDDEN_CLAIMS reporting 'defined in 0 files' while living in pr_prose.py")

    ts = R.extract("src/models.ts", "export interface User { id: string }\n")
    assert ts.method == "regex", ts.method
    assert "User" in ts.defines

    # A file that does not parse is UNPARSED, not empty. "Defines nothing" and "I could
    # not read this" are different facts and Stage 4 must not confuse them.
    broken = R.extract("src/broken.py", "def (((\n")
    assert broken.method == "unparsed", broken.method
    assert broken.defines == ()

    # An unsupported language says so rather than silently contributing nothing.
    other = R.extract("README.md", "# hello\n")
    assert other.method in ("unsupported", "regex"), other.method

    # Local variables must not pollute the module surface, or ranking drowns in noise.
    fn = R.extract("src/f.py", "def go():\n    tmp = 1\n    return tmp\n")
    assert "go" in fn.defines
    assert "tmp" not in fn.defines, (
        "a local variable is not part of the module's surface")


def test_a_partial_index_says_so_rather_than_looking_clean():
    """"No consumers found" and "I stopped reading" must be distinguishable.

    Install-time indexing scales with `installs x repo_size` rather than with change
    volume, so the caps are real and small. The failure they invite is the dangerous
    one: a repo that was truncated looks, to a later cross-repo search, exactly like a
    repo with nothing in it.
    """
    from app import repo_index as R

    # Over the file cap -> truncated is TRUE and the reason is counted.
    many = ((f"src/f{i}.py", f"def f{i}(): pass\n") for i in range(R.MAX_FILES_PER_REPO + 5))
    idx = R.build("acme/big", many)
    assert idx.truncated is True
    assert idx.skipped.get("file_cap", 0) >= 5, idx.skipped
    assert idx.stats()["truncated"] is True, "stats hide the truncation"

    # A file too large is skipped with its own reason, not merged into a total.
    big = R.build("acme/one", [("src/huge.py", "x = 1\n" * (R.MAX_FILE_BYTES))])
    assert big.skipped.get("too_large") == 1, big.skipped
    assert not big.files

    # Unreadable is separate from not-scannable: one is unknown, the other is by design.
    mixed = R.build("acme/mix", [
        ("src/a.py", "def a(): pass\n"),
        ("src/b.py", None),
        ("docs/readme.md", "# hi\n"),
    ])
    assert "a" in [s for f in mixed.files.values() for s in f.defines]
    assert mixed.skipped.get("unreadable") == 1, mixed.skipped
    assert mixed.skipped.get("not_scannable", 0) + \
           mixed.skipped.get("unsupported_language", 0) >= 1, mixed.skipped

    # A corrupt stored index reads back as None -- re-index -- NOT as an empty index,
    # which would make a broken file look like a repo with no symbols.
    import os as _os
    path = R._path_for("acme/corrupt")
    with open(path, "w") as fh:
        fh.write("{not json")
    try:
        assert R.load("acme/corrupt") is None
    finally:
        _os.remove(path)


def test_installing_builds_the_symbol_index_and_declares_the_repo_cap():
    """Indexing must happen with no command, and the cap must be visible.

    "Whenever Ripple is given access to a repo, it learns it" is the requirement, so an
    index that only appears when someone calls an endpoint does not satisfy it. And an
    org over MAX_REPOS must be TOLD which repos have no index -- unindexed repos are
    unknown to cross-repo analysis, not clean.
    """
    import ast

    from app import repo_index as R
    from app import webhook as W

    assert hasattr(W, "_build_symbol_index"), "no symbol-index builder is wired"

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tree = ast.parse(open(os.path.join(root, "app", "webhook.py"),
                          encoding="utf-8").read())

    callers, cap_readers = set(), set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            name = (getattr(getattr(node, "func", None), "id", "")
                    or getattr(getattr(node, "func", None), "attr", ""))
            if isinstance(node, ast.Call) and name == "_build_symbol_index":
                callers.add(fn.name)
            if isinstance(node, ast.Attribute) and node.attr == "MAX_REPOS":
                cap_readers.add(fn.name)

    assert callers, (
        "_build_symbol_index is defined but never called, so installing an app would "
        "index nothing and cross-repo analysis would have no data")
    assert any("install" in c for c in callers), (
        f"the index is built from {sorted(callers)}, none of which is the install "
        f"handler -- it would not happen automatically on being granted a repo")
    assert cap_readers & callers, (
        f"MAX_REPOS is not consulted where indexing happens ({sorted(callers)}), so an "
        f"org over the cap would silently get a partial picture")

    # And the cap must be a real number, not a placeholder.
    assert 1 <= R.MAX_REPOS <= 500
    assert 1 <= R.MAX_FILES_PER_REPO <= 100_000
    assert R.MAX_FILE_BYTES >= 1024


def test_the_index_finds_a_symbol_across_repos():
    """The whole point: a symbol changed in one repo, found in another.

    Stage 4 depends on this exact shape, so it is asserted here rather than assumed.
    """
    from app import repo_index as R

    producer = R.build("acme/models", [
        ("src/user.py", "class User:\n    phone_number: str\n"),
    ])
    consumer = R.build("acme/checkout", [
        ("src/pay.py", "from src.user import User\n\ndef pay(u: User):\n"
                       "    return u.phone_number\n"),
        ("src/unrelated.py", "def totals():\n    return 1\n"),
    ])

    assert [f.path for f in producer.definers("User")] == ["src/user.py"]
    refs = [f.path for f in consumer.referencers("phone_number")]
    assert refs == ["src/pay.py"], refs
    assert "src/unrelated.py" not in refs, (
        "an unrelated file was reported as a consumer, which is the false positive that "
        "would make the demo untrustworthy")

    # AST hits sort before regex hits, so ranking sees the confident ones first.
    mixed = R.build("acme/mixed", [
        ("a.ts", "export const User = 1\n"),
        ("b.py", "class User: pass\n"),
    ])
    methods = [f.method for f in mixed.definers("User")]
    assert methods and methods[0] == "ast", methods


def test_fix_capability_is_a_property_of_the_model_not_the_server():
    """Same Ollama, same wire format, opposite trust -- decided by the loaded model.

    `Provider.fix_capable` used to be a provider-level flag carrying model-level
    reasoning: the ollama note read "qwen2.5-coder:3b ... NOT as a fix engine", which is
    a fact about a MODEL attached to a SERVER. Measured 2026-08-26, on one host:

        qwen2.5-coder:3b        deleted the function parameter -> breaks every caller
        deepseek-coder-v2:16b   BYTE-IDENTICAL wrong answer, at 5x the size
        qwen3-coder:30b         returned the file UNCHANGED, matching the codemod's
                                refusal. Confirmed from chain provenance that the model
                                answered rather than the template falling back.

    The flag was wrong in the DANGEROUS direction the moment a better model was
    configured: reporting a capable backend as incapable is safe, the reverse is not.
    """
    import os as _os

    from app import llm_chain as C
    from app.llm_providers import (MODEL_MEASUREMENTS, RECOMMENDED_FIX_MODEL,
                                   RECOMMENDED_PROSE_MODEL, model_is_fix_capable)

    assert MODEL_MEASUREMENTS, "no models measured -- every assertion here is vacuous"

    # Every entry must carry evidence, and a capable one must say what it PASSED.
    for name, facts in MODEL_MEASUREMENTS.items():
        assert facts.evidence.strip(), f"{name} has no evidence for its rating"
        assert facts.gen > 0 and facts.prompt > 0, f"{name} has no measured throughput"
        if facts.fix_capable:
            assert len(facts.evidence) > 60, (
                f"{name} is trusted to write code on a one-line justification; say "
                f"what it was probed with and what it did")

    # An unmeasured model gets no benefit of the doubt.
    assert not model_is_fix_capable("some-model-nobody-measured")
    assert not model_is_fix_capable("")

    # The recommendations must exist in the table and be self-consistent.
    assert model_is_fix_capable(RECOMMENDED_FIX_MODEL), (
        f"{RECOMMENDED_FIX_MODEL} is recommended for fixes but not marked capable")
    assert RECOMMENDED_PROSE_MODEL in MODEL_MEASUREMENTS
    # Prose is allowed to use a model fixes may not -- that asymmetry is the point.
    prose_facts = MODEL_MEASUREMENTS[RECOMMENDED_PROSE_MODEL]
    fix_facts = MODEL_MEASUREMENTS[RECOMMENDED_FIX_MODEL]
    assert prose_facts.prompt > fix_facts.prompt, (
        "the prose model is recommended for ingesting context faster; if that is no "
        "longer true the recommendation has no basis")

    # And the chain must actually consult the model. Env-isolated, because chain
    # composition depends on configuration and a test reading the ambient shell
    # asserts whatever the developer happens to have exported.
    saved = {k: _os.environ.get(k) for k in
             ("ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "ANTHROPIC_AUTH_TOKEN")}
    try:
        _os.environ["ANTHROPIC_BASE_URL"] = "http://localhost:11434"
        _os.environ["ANTHROPIC_AUTH_TOKEN"] = "test-credential"

        _os.environ["ANTHROPIC_MODEL"] = RECOMMENDED_FIX_MODEL
        assert "ollama" in [p.name for p in C.fix_chain()], (
            "the recommended fix model does not put the local backend in the fix chain")

        incapable = next(m for m, f in MODEL_MEASUREMENTS.items() if not f.fix_capable)
        _os.environ["ANTHROPIC_MODEL"] = incapable
        assert "ollama" not in [p.name for p in C.fix_chain()], (
            f"{incapable} is measured as unfit to write code, yet the local backend is "
            f"still in the fix chain -- capability is not being read from the model")
        assert "ollama" in [p.name for p in C.prose_chain()], (
            "a model unfit for code is still fit for prose; excluding it from prose is "
            "stricter than the measurement supports")
    finally:
        for k, v in saved.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v


def test_the_local_3b_model_is_not_in_the_fix_chain():
    """Measured: it produces compiling-but-wrong code on the shapes that matter.

    On the three shapes the deterministic codemod REFUSES -- the only shapes where an
    LLM adds anything -- qwen2.5-coder:3b got two of three wrong on 2026-08-26:

        function parameter  export function dial(): void { console.log(); }
        aliased then used   export function label(user) { return ''; }

    Both are brace-balanced with the target removed, so the diff contract passes them
    and tsc compiles them. NO GATE HERE CAN SEE THE DIFFERENCE, which is exactly why
    the exclusion has to be a rule rather than a review habit: a fallback to this model
    would degrade fixes invisibly while still reporting `[llm]`.

    Prose is the other way round -- a weak model writing a bad PR description is
    visible to whoever reads it and cannot break a build -- so prose_chain() is wider
    on purpose, and this pins the asymmetry.
    """
    from app import llm_chain as C
    from app.llm_providers import PROVIDERS

    # Credentials injected: the chains are credential-filtered, so without this the
    # composition would depend on the developer's shell rather than on the rule.
    with _with_provider_credentials():
        fix_names = {p.name for p in C.fix_chain()}
        prose_names = {p.name for p in C.prose_chain()}

    assert PROVIDERS["ollama"].fix_capable is False, (
        "ollama is marked fix_capable; the measurement above says otherwise")
    assert "ollama" not in fix_names, (
        f"the local 3B model is in the FIX chain: {sorted(fix_names)}")
    assert "ollama" in prose_names, (
        "ollama is excluded from prose too, which is stricter than the measurement "
        "supports -- bad prose is visible, bad code is not")
    assert fix_names, "the fix chain is empty, so no fix could ever be generated"
    assert fix_names < prose_names, (
        f"prose must be a strict superset of fix; fix={sorted(fix_names)} "
        f"prose={sorted(prose_names)}")

    # Every excluded provider must carry the reason, or the exclusion is folklore.
    for prov in PROVIDERS.values():
        if not prov.fix_capable:
            assert "MEASURED" in prov.note, (
                f"{prov.name} is excluded from the fix chain with no measurement "
                f"recorded in its note")


def test_an_unconfigured_install_never_resolves_to_a_paid_backend():
    """Free by default, paid by explicit opt-in. Added 2026-09-13.

    The fallbacks were api.anthropic.com and claude-sonnet-4, so llm_config FAILED OPEN
    to a metered third-party API. The empty case was safe -- nothing configured meant
    nothing attempted -- so the exposure was the HALF-configured case, which is the one
    that actually occurs:

        key set, ANTHROPIC_MODEL unset      -> billed for a model nobody chose
        OpenRouter key in ANTHROPIC_AUTH_TOKEN, BASE_URL forgotten
                                            -> customer source POSTed to Anthropic
                                               under a non-Anthropic credential. Auth
                                               fails, but the SEND already happened.

    Both are asserted below, because the empty case passing is what made the defect
    invisible for as long as it existed.
    """
    import importlib
    import os as _os

    import app.llm_config as L

    keys = ("ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN", L.PAID_OPT_IN_ENV)
    saved = {k: _os.environ.get(k) for k in keys}
    try:
        for k in keys:
            _os.environ.pop(k, None)
        importlib.reload(L)

        # 1. Nothing configured -> free, self-hosted, and OFF.
        assert not L.is_paid_backend()
        assert not L.is_configured()

        # 2. THE REAL DEFECT: a credential present, no model named. Under the old
        #    defaults this silently resolved to paid Claude on api.anthropic.com.
        _os.environ["ANTHROPIC_API_KEY"] = "sk-ant-not-a-real-key"
        _os.environ["ANTHROPIC_BASE_URL"] = "https://api.anthropic.com"
        assert L.is_paid_backend(), "paid endpoint not recognised as paid"
        assert not L.is_configured(), (
            "a paid third-party backend is usable without the operator opting in -- "
            "customer source can be billed and sent off-network by default")
        reason = L.refusal_reason()
        assert L.PAID_OPT_IN_ENV in reason and "paid" in reason.lower(), (
            f"the paid refusal does not say why or how to accept it: {reason!r}")

        # 3. Opting in is NOT enough on its own: the model must be named, because the
        #    free default names a local model a hosted API rejects as nonexistent.
        _os.environ[L.PAID_OPT_IN_ENV] = "1"
        assert L.paid_backend_lacks_explicit_model()
        assert not L.is_configured(), (
            "a paid backend is accepted with no ANTHROPIC_MODEL, so the request would "
            "carry a local model name to a hosted API")
        assert "ANTHROPIC_MODEL" in L.refusal_reason()

        # 4. Fully specified paid backend -> allowed. The opt-in must actually work,
        #    or this is a block rather than a gate.
        _os.environ["ANTHROPIC_MODEL"] = "claude-sonnet-4-5-20250929"
        assert L.is_configured(), (
            "a paying operator who set the key, the model and the opt-in is still "
            "refused -- the opt-in does not open")

        # 5. Only "1" counts. A check on truthiness would spend money on a typo.
        for refusal in ("0", "false", "no", "", "true", "yes"):
            _os.environ[L.PAID_OPT_IN_ENV] = refusal
            assert not L.is_configured(), (
                f"{L.PAID_OPT_IN_ENV}={refusal!r} was read as consent to pay")
        _os.environ[L.PAID_OPT_IN_ENV] = "1"

        # 6. THE SELF-HOSTED PATH MUST BE UNAFFECTED. If this regresses, the free
        #    default is worthless: the deployment it exists to serve stops working.
        _os.environ.pop(L.PAID_OPT_IN_ENV, None)
        _os.environ.pop("ANTHROPIC_API_KEY", None)
        _os.environ.pop("ANTHROPIC_MODEL", None)
        _os.environ["ANTHROPIC_BASE_URL"] = "http://ripple-llm.railway.internal:11434"
        assert not L.is_paid_backend()
        assert L.is_self_hosted() and L.is_configured(), (
            "a keyless self-hosted backend is no longer configured -- the paid opt-in "
            "has caught the free path it was supposed to protect")
    finally:
        for k, v in saved.items():
            _os.environ.pop(k, None)
            if v is not None:
                _os.environ[k] = v
        importlib.reload(L)


def test_every_model_declares_a_commercially_usable_licence():
    """Weights licences are read from the publisher, not inferred from the family.

    Qwen's own release notes: "All our open-source models, except for the 3B and 72B
    variants, are licensed under Apache 2.0." So qwen2.5-coder:3b is NON-COMMERCIAL
    while its 7B and 14B siblings are Apache-2.0 -- same family, same generation, same
    naming scheme, opposite answer on the only axis that can make the product
    undeliverable. A per-model licence string copied from a sibling would be wrong, and
    nothing downstream would ever notice.

    `commercial_ok` is therefore never guessed: an unmeasured model is False, the same
    rule model_is_fix_capable applies. Guessing yes is a licence breach rather than a
    degraded result.
    """
    from app.llm_providers import (DEFAULT_SELF_HOSTED_MODEL, LICENCES,
                                   MODEL_MEASUREMENTS, RECOMMENDED_FIX_MODEL,
                                   RECOMMENDED_PROSE_MODEL, licence_for,
                                   model_is_commercially_licensed)

    assert LICENCES, "no licences declared -- every assertion here is vacuous"

    for name, lic in LICENCES.items():
        assert lic.source.startswith("http"), (
            f"licence {name} cites no readable source; a licence claim nobody can "
            f"re-check is the same as no claim")
        if not lic.commercial_ok or not lic.osi_approved:
            assert lic.note.strip(), (
                f"licence {name} restricts use but does not say how")

    # Every measured model must reference a DECLARED licence, so adding a model forces
    # its terms to be looked up rather than assumed.
    for name, facts in MODEL_MEASUREMENTS.items():
        assert licence_for(facts.licence) is not None, (
            f"{name} declares licence {facts.licence!r}, which is not in LICENCES")

    # An unknown model gets no benefit of the doubt.
    assert not model_is_commercially_licensed("some-model-nobody-checked")
    assert not model_is_commercially_licensed("")

    # The three names Ripple puts its own weight behind must all be shippable.
    for role, chosen in (("fix", RECOMMENDED_FIX_MODEL),
                         ("prose", RECOMMENDED_PROSE_MODEL),
                         ("default", DEFAULT_SELF_HOSTED_MODEL)):
        assert model_is_commercially_licensed(chosen), (
            f"the {role} model is {chosen}, whose licence forbids commercial use -- "
            f"Ripple is a paid product, so this is a licence breach and not a "
            f"performance tradeoff")

    # And the known non-commercial entry must stay non-recommended. This pins the
    # specific trap: the 3B is the FASTEST model here at context ingestion (220 tok/s),
    # so it is exactly the one a future speed-driven change would reach for.
    assert not model_is_commercially_licensed("qwen2.5-coder:3b"), (
        "qwen2.5-coder:3b is recorded as commercially usable; Qwen carves the 3B "
        "variant out of its Apache-2.0 release")
    assert "qwen2.5-coder:3b" not in (RECOMMENDED_FIX_MODEL, RECOMMENDED_PROSE_MODEL,
                                      DEFAULT_SELF_HOSTED_MODEL)


def test_a_mechanical_rename_is_fixed_with_no_model_and_never_skipped_silently():
    """The locked target's edit must need zero inference, and a skip must say why.

    Stage 3 locked `np.alltrue` -> `np.all` in do-mpc precisely because it is a pure
    token rename. This pins the two properties the fork PR depends on:

    1. THE EDIT NEEDS NO MODEL. Asserted with every model env var cleared and
       is_configured() False, so a pass cannot be a model quietly answering. Railway
       has no reachable model at all, so if this needed one the deployed service
       would produce nothing.

    2. A SKIP IS NEVER SILENT. `_generate_fix_with_rag_fallback` ended with three
       `return content, ""` sites -- identical content, EMPTY explanation -- and the
       follow-on caller then recorded the fixed literal "no change generated",
       discarding even that. So a target was skipped with no record of whether the
       shape had no handler, the model was unavailable, or the model declined. Those
       are three different problems needing opposite responses.
    """
    import ast
    import importlib
    import inspect
    import os as _os

    keys = ("ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN", "RIPPLE_ALLOW_PAID_MODEL",
            "RIPPLE_ALLOW_HOSTED_MODEL", "RIPPLE_SELF_HOSTED")
    saved = {k: _os.environ.get(k) for k in keys}
    try:
        for k in keys:
            _os.environ.pop(k, None)

        import app.llm_config as L
        importlib.reload(L)
        assert not L.is_configured(), (
            "a model is configured, so this test cannot prove the edit is model-free")

        from app.change_types import canonical_op, category
        from app.fix_templates import apply_fix_template

        assert canonical_op("field_renamed") == "rename_field"
        assert category("field_renamed") == "mechanical"

        # The real shape, on code shaped like the locked target: a removed numpy
        # alias reached as an attribute and called.
        code = (
            "import numpy as np\n"
            "\n"
            "def check(kwargs, keys, names):\n"
            "    a = np.alltrue([isinstance(v, list) for v in kwargs.values()])\n"
            "    b = np.alltrue([v in names for v in keys])\n"
            "    return a and b\n"
        )
        fixed, explanation = apply_fix_template(
            code=code, language="python", change_type="field_renamed",
            field_name="alltrue", new_name="all")

        assert fixed != code, (
            "the deterministic template produced NO change for a mechanical rename "
            "in Python -- the locked target would be skipped and the fork PR would "
            "never open")
        assert "np.alltrue" not in fixed, f"the removed alias survives:\n{fixed}"
        assert fixed.count("np.all(") == 2, f"expected 2 renamed calls:\n{fixed}"

        # Minimal: only the two reference lines move, and the result still parses.
        before, after = code.splitlines(), fixed.splitlines()
        assert len(before) == len(after)
        moved = [i for i, (o, f) in enumerate(zip(before, after)) if o != f]
        assert len(moved) == 2, f"{len(moved)} lines changed, expected 2"
        ast.parse(fixed)

        # The reported count must match the real edit. name_variants() yields the
        # SAME string for snake and camel on an all-lowercase name, and summing per
        # variant KEY double-counted -- reporting "4 replacements made" for a
        # two-line edit, in text that goes into a pull request body on a repository
        # we do not own.
        assert "2 replacements made" in explanation, (
            f"the explanation miscounts the edit: {explanation!r}")

        # 2. NO SILENT SKIP. Every give-up path must carry a reason.
        import app.webhook as w
        src = inspect.getsource(w._generate_fix_with_rag_fallback)
        assert 'return content, ""' not in src, (
            "a give-up path returns an EMPTY explanation, so the caller cannot say "
            "why the file was skipped")
        assert "refusal_reason" in src, (
            "the no-model give-up does not report WHY no model was available; "
            "'no handler for this shape' and 'model misconfigured' need opposite "
            "responses")

        follow_src = inspect.getsource(w._execute_follow_on_prs)
        assert '"explanation": "no change generated"' not in follow_src, (
            "the follow-on caller replaces the generator's reason with a fixed "
            "string, discarding the only explanation of the skip")
        # Scoped to the skip RECORD, not the whole function: `why` also appears in
        # the activity log next to it, so a bare substring check passed while the
        # record itself had been stripped -- caught by mutation.
        marker = 'skipped.append({"repo": target_repo, "reason": "no fixes generated"'
        assert marker in follow_src, "the no-fixes skip record was restructured"
        record = follow_src.split(marker, 1)[1].split("})", 1)[0]
        assert "why" in record, (
            "a repo skipped for 'no fixes generated' records no per-file reasons, so "
            f"the caller cannot tell which problem occurred: {record.strip()[:120]!r}")

        # And prove the reason actually arrives, by running the real function on
        # content no template can fix, with no model available.
        from app.diff_engine import BreakingChange

        class _C:
            file_path = "x.py"
            language = "python"
            line_number = 1
            code_snippet = ""
            confidence = "high"
            match_reason = "t"

        judgement = BreakingChange(
            change_type="operation_removed", path="/x", method="GET",
            field_name="doThing", field_type="", location="body",
            severity="breaking", description="rpc removed")
        out, why = w._generate_fix_with_rag_fallback("x = 1\n", _C(), judgement, "")
        assert out == "x = 1\n", "content changed on a shape nothing can fix"
        assert why, (
            "the real function gave up with an empty reason -- exactly the silent "
            "skip this gate exists to prevent")
        assert "no fix" in why.lower(), f"the reason does not read as a refusal: {why!r}"
    finally:
        for k, v in saved.items():
            _os.environ.pop(k, None)
            if v is not None:
                _os.environ[k] = v
        import app.llm_config as L
        importlib.reload(L)


def test_execute_follow_ons_routes_to_the_fork_path_and_writes_nothing_without_it():
    """The CLI flag was parsed and then never read. Wired 2026-09-13.

    `cmd_run` did:

        execute_follow_ons = "--execute-follow-ons" in sys.argv   # never read again
        ...
        prs = create_prs(all_fixes, ...)                          # always this

    so the flag silently did nothing and every run went through `create_prs`, which
    opens a branch with `POST /repos/{repo}/git/refs`. That needs push permission, so
    it 403s on every repository the token cannot push to -- i.e. every open-source
    target, which is the only kind this flag exists for. A dead flag is worse than a
    missing one: it reads as supported.

    Four properties are pinned here, and the third is the one that matters most.
    """
    import inspect

    from app import cli
    from app import fork_pr

    src = inspect.getsource(cli.cmd_run)

    # 1. The flag must REACH the fork path, not just be parsed.
    assert "execute_follow_ons" in src
    assert "_run_fork_follow_ons" in src, (
        "cmd_run parses --execute-follow-ons but never routes on it -- the flag is "
        "dead again")
    branch_src = src.split("elif execute_follow_ons:", 1)
    assert len(branch_src) == 2, (
        "there is no branch on execute_follow_ons, so both paths cannot differ")

    # 2. The same-repo path must SURVIVE. It is correct wherever the token really can
    #    push, which is where Ripple started; replacing it would be a regression.
    assert "create_prs(" in src, (
        "the same-repo push path was removed; it is still correct for a repo the "
        "installer owns")

    # 3. WITHOUT THE FLAG, THE FORK PATH IS NEVER REACHED, and a dry run issues NO
    #    request. Asserted by executing the real function against a fake `api`, not by
    #    reading source: a source check passes while the wiring is inverted, and
    #    "issues requests and discards the result" would still create a fork.
    fork_src = inspect.getsource(cli._run_fork_follow_ons)
    assert "open_fork_prs" in fork_src

    api_calls = []
    run = fork_pr.open_fork_prs(
        {"acme/upstream": [("a.py", "Y29kZQ==")]},
        branch="ripple/x", title="t", body="b", token="tok",
        api=lambda *a, **k: api_calls.append(a) or {}, dry_run=True)
    assert api_calls == [], (
        "a dry run issued platform requests; ensure_fork CREATES a fork, so this "
        "would leave a real repository on the operator's account")
    assert not run.opened and run.refused, (
        "a dry run must report what it WOULD do as a stated refusal, not silently "
        "produce nothing")
    assert "dry run" in run.refused[0][1]

    # And with dry_run off, the SAME inputs do reach the platform -- otherwise the
    # check above would pass on a function that never works at all.
    api_calls.clear()
    fork_pr.open_fork_prs(
        {"acme/upstream": [("a.py", "Y29kZQ==")]},
        branch="ripple/x", title="t", body="b", token="tok",
        api=lambda *a, **k: api_calls.append(a) or {}, dry_run=False)
    assert api_calls, "with dry_run=False no request was made either -- dead code"

    # 4. Repository coordinates come from the CLONE, never from a guess. A wrong
    #    answer here does not fail loudly -- it opens a pull request against somebody
    #    else's repository.
    slug, why = fork_pr.upstream_from_git_remote("/definitely/not/a/clone")
    assert slug == "" and why, "a non-clone resolved to a repository anyway"

    for url, expected in (
        ("https://github.com/do-mpc/do-mpc.git", "do-mpc/do-mpc"),
        ("git@github.com:do-mpc/do-mpc.git", "do-mpc/do-mpc"),
        ("ssh://git@github.com/o/r", "o/r"),
        ("https://gitlab.com/o/r.git", ""),
        ("https://github.com/toomany/parts/here", ""),
    ):
        got = _slug_from_url(fork_pr, url)
        assert got == expected, f"{url} -> {got!r}, expected {expected!r}"

    # 5. The repo-relative path must survive a SYMLINKED path. Found by running the
    #    resolver against a real clone: this host has /home symlinked to /local/home,
    #    so git's --show-toplevel and the caller's path are two spellings of the same
    #    directory. With abspath the containment check rejected a file plainly inside
    #    the clone; realpath on both sides is the fix. A fake `git` cannot catch this,
    #    because a fake returns whatever spelling the test already used.
    import os
    import subprocess
    import tempfile

    root = tempfile.mkdtemp()
    try:
        real = os.path.join(root, "real")
        os.makedirs(os.path.join(real, "pkg", "sub"))
        target = os.path.join(real, "pkg", "sub", "mod.py")
        with open(target, "w") as fh:
            fh.write("x = 1\n")
        init = subprocess.run(["git", "init", "-q", real],
                              capture_output=True, text=True)
        if init.returncode == 0:
            link = os.path.join(root, "link")
            os.symlink(real, link)
            via_link = os.path.join(link, "pkg", "sub", "mod.py")
            rel, why = fork_pr.repo_relative_path(via_link)
            assert rel == "pkg/sub/mod.py", (
                f"a file reached through a symlinked clone root resolved to {rel!r} "
                f"({why}) -- the containment check is comparing unresolved paths")
            # And the basename-guessing predecessor must not be what we rely on.
            from app.pr_engine import _relative_file_path
            assert _relative_file_path(target, "o/r") != rel, (
                "pr_engine._relative_file_path now agrees, so this contrast is stale")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _slug_from_url(fork_pr, url: str) -> str:
    """Resolve a remote URL through the real parser by faking only `git`.

    Patching fork_pr._git rather than reimplementing the parsing keeps this test
    honest: a reimplementation would pass while the shipped parser was wrong.
    """
    saved = fork_pr._git
    fork_pr._git = lambda args, cwd: url if args[:2] == ["config", "--get"] else ""
    try:
        slug, _ = fork_pr.upstream_from_git_remote("/anywhere")
        return slug
    finally:
        fork_pr._git = saved


def test_a_hosted_provider_cannot_silently_receive_repository_content():
    """Free is not private. Added 2026-09-13.

    A free HOSTED tier costs nothing AND receives the prompt, and free tiers commonly
    reserve the right to log or train on what they are sent. Ripple's prompts carry the
    customer's source, so "we only use free models" is a COST claim and not a
    data-handling one. Stage 1 made the default free; that did nothing about privacy,
    because openrouter is free.

    Two payload classes are gated, not one. fix_generator sends whole file bodies;
    impact.rank and pr_prose send repository names, file paths and internal symbol
    names. The second set is not file contents but it identifies the company and
    discloses a private API surface, so it is still repository content.

    THE CHECK LIVES IN run() BECAUSE THAT IS THE ONLY CHOKE POINT. fix_generator tries
    the Configured custom-gateway entry FIRST, with chain=[configured], bypassing
    fix_chain() completely -- so a filter applied only to the registry chain would guard
    the path that is not taken. That specific bypass is asserted below.
    """
    import os as _os

    from app import llm_chain as C
    from app import llm_config as L
    from app import llm_providers as P

    keys = ("ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN", L.PAID_OPT_IN_ENV, L.HOSTED_OPT_IN_ENV,
            L.SELF_HOSTED_ASSERT_ENV)
    saved = {k: _os.environ.get(k) for k in keys}
    try:
        for k in keys:
            _os.environ.pop(k, None)

        local = P.PROVIDERS["ollama"]
        hosted = P.PROVIDERS["openrouter"]

        # 1. The classification must follow the HOST, so it cannot be made wrong by
        #    editing a field. A keyless public provider would otherwise read as local.
        assert local.data_boundary == P.WEIGHTS_LOCAL
        assert hosted.data_boundary == P.HOSTED_FREE
        assert P.PROVIDERS["anthropic"].data_boundary == P.HOSTED_PAID
        assert not local.sees_customer_data
        assert hosted.sees_customer_data, (
            "a free hosted tier is not treated as seeing customer data -- free was "
            "confused with private, which is the whole point of this gate")

        # Private addresses in every form Ripple's own deployments use.
        for url in ("http://localhost:11434", "http://127.0.0.1:11434",
                    "http://ripple-llm.railway.internal:11434",
                    "http://10.0.0.7:11434", "http://172.16.4.4:11434",
                    "http://192.168.1.9:11434", "http://ollama:11434"):
            assert P.data_boundary_for_base_url(url) == P.WEIGHTS_LOCAL, url

        # An unidentifiable PUBLIC host is NOT assumed local. This is the opposite
        # asymmetry from is_paid_base_url, deliberately: guessing "free" costs nothing,
        # guessing "local" discloses source.
        assert P.data_boundary_for_base_url("https://llm.example.com") == \
            P.HOSTED_UNKNOWN

        # 2. With no opt-in, BOTH gated payload classes are refused to a hosted
        #    provider, and a non-repository payload is not.
        for payload in (C.PAYLOAD_SOURCE, C.PAYLOAD_METADATA):
            ok, why = C.may_receive(hosted, payload)
            assert not ok, f"{payload} may reach a third party with no opt-in"
            assert L.HOSTED_OPT_IN_ENV in why, f"refusal does not say how: {why!r}"
            assert C.may_receive(local, payload)[0], (
                f"a LOCAL provider is refused {payload}; the gate has caught the "
                f"private path it exists to protect")
        assert C.may_receive(hosted, C.PAYLOAD_PUBLIC)[0]

        # 3. THE DISCLOSURE MUST NOT HAPPEN. A refusal that still issues the request
        #    and discards the reply has already sent the source.
        fired = []
        result = C.run(lambda p: fired.append(p.name) or "text",
                       chain=[hosted], payload=C.PAYLOAD_SOURCE)
        assert fired == [], (
            "the provider was CALLED despite being refused -- the prompt left the "
            "network and only the response was dropped")
        assert not result.ok

        # 4. And it must be STATED, not silent. Same requirement that made an
        #    exhausted chain report itself instead of print()ing to stdout.
        stated = result.stated_outcome()
        assert "refused" in stated and L.HOSTED_OPT_IN_ENV in stated, (
            f"the refusal is invisible to the PR body and activity log: {stated!r}")

        # 5. FAIL CLOSED. A caller that forgets `payload` must be treated as sending
        #    source, because the wrong default in the other direction is silent.
        fired.clear()
        assert not C.run(lambda p: fired.append(p.name) or "text",
                         chain=[hosted]).ok
        assert fired == [], "run()'s default payload class permits a disclosure"

        # 6. THE BYPASS PATH. fix_generator runs the Configured entry with its own
        #    one-element chain, so it must be covered by the same check.
        assert not C.may_receive(
            C.Configured(name="x", base_url="https://openrouter.ai/api"),
            C.PAYLOAD_SOURCE)[0], (
            "a custom gateway pointed at a third party bypasses the boundary check -- "
            "this is the path fix_generator tries FIRST")
        assert C.may_receive(
            C.Configured(name="x", base_url="http://localhost:11434"),
            C.PAYLOAD_SOURCE)[0]

        # 7. The opt-in must actually open, or this is a block and not a gate.
        _os.environ[L.HOSTED_OPT_IN_ENV] = "1"
        assert C.may_receive(hosted, C.PAYLOAD_SOURCE)[0]
        for refusal in ("0", "false", "no", "", "yes", "true"):
            _os.environ[L.HOSTED_OPT_IN_ENV] = refusal
            assert not C.may_receive(hosted, C.PAYLOAD_SOURCE)[0], (
                f"{L.HOSTED_OPT_IN_ENV}={refusal!r} read as consent to disclose")
        _os.environ.pop(L.HOSTED_OPT_IN_ENV, None)

        # 8. RIPPLE_SELF_HOSTED may reclassify an UNIDENTIFIABLE host, because the
        #    operator knows something inspection cannot. It must NOT launder a
        #    catalogued third party -- that would turn a config mistake into a silent
        #    disclosure.
        _os.environ[L.SELF_HOSTED_ASSERT_ENV] = "1"
        assert C.may_receive(
            C.Configured(name="x", base_url="https://llm.example.com"),
            C.PAYLOAD_SOURCE)[0], (
            "an operator cannot declare their own public-address model self-hosted, so "
            "a legitimate deployment shape is unusable")
        for third_party in ("https://openrouter.ai/api", "https://api.anthropic.com"):
            assert not C.may_receive(
                C.Configured(name="x", base_url=third_party),
                C.PAYLOAD_SOURCE)[0], (
                f"{L.SELF_HOSTED_ASSERT_ENV} launders {third_party} as self-hosted")
    finally:
        for k, v in saved.items():
            _os.environ.pop(k, None)
            if v is not None:
                _os.environ[k] = v


def test_the_llm_budget_is_reset_once_per_push():
    """A budget nobody resets closes every provider and then reports them closed.

    The ceiling is module-level because its scope is the PUSH while
    _generate_with_llm runs once per CONSUMER FILE -- a budget created inside the
    generator would reset on every call and cap nothing. That makes the reset call a
    wiring requirement, and a wiring requirement with no gate is how the learning
    loop's author-fetch ended up built and unreachable.
    """
    import ast

    from app import fix_generator as F
    from app.llm_chain import Budget

    assert hasattr(F, "reset_llm_budget"), "no reset function exists"

    # It must actually be called from the per-push entry, not merely defined.
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tree = ast.parse(open(os.path.join(root, "app", "webhook.py"),
                          encoding="utf-8").read())
    callers = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            name = (getattr(getattr(node, "func", None), "id", "")
                    or getattr(getattr(node, "func", None), "attr", ""))
            if isinstance(node, ast.Call) and name.endswith("reset_llm_budget"):
                callers.add(fn.name)
    assert callers, (
        "reset_llm_budget is defined but never called from app/webhook.py, so the "
        "per-provider ceiling would persist across every push for the life of the "
        "process")

    # Budget semantics: a rate-limited provider is CLOSED, not merely counted.
    b = Budget(per_provider=3)
    assert b.allows("x")
    b.record("x", "ok")
    assert b.allows("x"), "one successful call must not exhaust a budget of 3"
    b.record("x", "rate_limited")
    assert not b.allows("x"), (
        "a rate-limited provider must be closed for the rest of the run -- asking "
        "again is a question already answered")
    assert "rate limited" in b.why_skipped("x")

    b2 = Budget(per_provider=2)
    b2.record("y", "ok")
    b2.record("y", "ok")
    assert not b2.allows("y"), "the numeric ceiling is not enforced"
    assert "budget" in b2.why_skipped("y")


def test_provider_registry_never_claims_an_unreachable_backend():
    """`usable_today` must be derived from what can speak the format, not declared.

    The registry exists because the FORMAT TRAP in llm_config was read as "every free
    provider needs a second client first", and that turned out to be false: OpenRouter
    serves a native Anthropic Messages endpoint, so it is reachable by the client that
    already exists. Measured 2026-08-26 by error-envelope fingerprint and confirmed
    against OpenRouter's API reference.

    The risk in writing that down is a catalogue that drifts into wishful thinking --
    a provider marked usable because someone hopes it is. So `usable_today` reads
    FORMAT_CLIENTS, and this pins the consequences in both directions.
    """
    from app import llm_providers as P

    assert P.PROVIDERS, "empty registry -- every assertion below would be vacuous"

    # Every usable provider's format has a named client.
    for prov in P.usable():
        assert prov.wire_format in P.FORMAT_CLIENTS, (
            f"{prov.name} reports usable_today but no client speaks "
            f"{prov.wire_format!r}")

    # Every provider whose format has NO client must report unusable. This is the
    # direction that would let the catalogue lie.
    for prov in P.PROVIDERS.values():
        if prov.wire_format not in P.FORMAT_CLIENTS:
            assert not prov.usable_today, (
                f"{prov.name} speaks {prov.wire_format!r}, which no client here "
                f"implements, yet it reports usable_today")

    # The measured finding, asserted so a regression is loud: at least one FREE
    # provider is reachable with the client that already exists.
    free_usable = {p.name for p in P.free_and_usable()}
    assert free_usable, (
        "no free provider is reachable today. If that is now true, the OpenRouter "
        "Anthropic-format endpoint has gone away and the plan that depends on it "
        "needs revisiting")
    assert "openrouter" in free_usable, (
        f"openrouter is the measured free Anthropic-format backend; got {free_usable}")

    # And the ones genuinely blocked are blocked for the stated reason.
    blocked = {p.name for p in P.blocked_on_a_client()}
    assert blocked == {"groq", "cerebras"}, (
        f"expected groq and cerebras to be the OpenAI-format holdouts, got {blocked}")

    # Every note must say what was measured -- a catalogue of unsourced claims is how
    # the pipeline census and the '17.5%' number went wrong.
    for prov in P.PROVIDERS.values():
        assert prov.note.strip(), f"{prov.name} has no note"
        assert any(w in prov.note for w in ("MEASURED", "UNVERIFIED", "default")), (
            f"{prov.name}'s note does not say whether its claims were measured, "
            f"documented, or unverified: {prov.note[:60]}")


def test_llm_key_is_resolved_only_through_llm_config():
    """Two sites GATED on os.environ["ANTHROPIC_API_KEY"] while the call they
    guarded built its client from llm_config.api_key(). An LLM-gateway setup
    sets ANTHROPIC_AUTH_TOKEN and deliberately leaves ANTHROPIC_API_KEY unset,
    so both gates failed closed and fell back to the template -- while the
    caller believed the LLM had answered. Verified against a live LiteLLM
    proxy: 0 requests arrived until the gates were fixed.

    Pins the STRUCTURE, not the behaviour: any new direct read reintroduces the
    same silent divergence, and no unit test of either function would fail.

    GENERALISED from the single literal ANTHROPIC_API_KEY. The scan is now over every
    credential env var any provider in the registry declares, because the failure has
    nothing to do with which provider it is: a module that reads a key directly
    bypasses whatever fallback llm_config applies, and gates closed while the caller
    believed the LLM had answered. A second provider family (GROQ_API_KEY,
    CEREBRAS_API_KEY) read directly would have been invisible to the old scan.

    AND IT IS AN AST SCAN, NOT A TEXT SCAN, for two measured reasons.

    The text version matched `environ.get("ANTHROPIC_API_KEY")` exactly, so the
    TWO-ARGUMENT form in webhook.py's /test-llm -- `environ.get("ANTHROPIC_API_KEY",
    "")` -- slipped past it for as long as it existed. That endpoint gated on the wrong
    variable, ignored ANTHROPIC_BASE_URL and ANTHROPIC_MODEL, and reported a hardcoded
    model name, so the one diagnostic for "does my LLM config work" tested a different
    backend from the fix path.

    Then, once the real read was fixed, the text scan flagged the DOCSTRING that quotes
    the old line as documentation. Rewording the prose to dodge the matcher is the
    wrong repair: the same "a matcher that cannot tell code from prose" hazard was hit
    three times in the codemod fixtures, and the lesson each time was to make the
    matcher syntax-aware rather than to censor the writing.
    """
    import ast
    import os as _os

    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    app = _os.path.join(root, "app")

    from app.ast_compat import str_literal
    from app.llm_providers import PROVIDERS
    creds = {p.key_env for p in PROVIDERS.values() if p.key_env}
    creds.add("ANTHROPIC_API_KEY")      # the original case, kept explicit
    creds.add("ANTHROPIC_AUTH_TOKEN")   # the token llm_config prefers
    assert creds, "no credential env vars to scan for -- would pass vacuously"

    def _env_reads(tree):
        """Every env var name this AST actually READS, ignoring strings and comments."""
        found = set()
        for node in ast.walk(tree):
            # os.environ["X"]
            if isinstance(node, ast.Subscript):
                target = node.value
                if isinstance(target, ast.Attribute) and target.attr == "environ":
                    name = str_literal(getattr(node, "slice", None)) or str_literal(
                        getattr(getattr(node, "slice", None), "value", None))
                    if name:
                        found.add(name)
                continue
            if not isinstance(node, ast.Call) or not node.args:
                continue
            fn = node.func
            # os.environ.get("X") / environ.get("X") / os.getenv("X") / getenv("X")
            attr = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if attr not in ("get", "getenv"):
                continue
            if attr == "get":
                owner = getattr(fn, "value", None)
                owner_name = (getattr(owner, "attr", None)
                              or getattr(owner, "id", None))
                if owner_name != "environ":
                    continue
            name = str_literal(node.args[0])
            if name:
                found.add(name)
        return found

    offenders = []
    for name in sorted(_os.listdir(app)):
        if not name.endswith(".py") or name == "llm_config.py":
            continue
        path = _os.path.join(app, name)
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError as exc:
            raise AssertionError(f"app/{name} does not parse: {exc}") from exc
        for var in sorted(_env_reads(tree) & creds):
            offenders.append(f"{name}: {var}")
    assert not offenders, (
        "provider credential read outside llm_config -- route it through "
        f"llm_config so the token fallback is honoured: {offenders}"
    )


def test_added_required_field_template_does_not_claim_more_than_it_did():
    """fix_generator held its OWN inline added_required_field implementation for
    typescript/python/java, all three written against the demo fixture:
      python     -- a parameter literally named `age`, a dict named `payload`
      java       -- a literal replace of '{"name": "%s", "email": "%s"}'
      typescript -- an interface named *Request, inserting `data.{field}`

    On ordinary code the signature edit matched while the payload edit missed,
    and `explanation` was assigned OUTSIDE the `if match:` blocks -- so the PR
    body claimed the payload had been updated for code that does not send the
    field. The half-fix compiles, so the syntax-only validator passed it.
    Measured: java OVERSTATED, typescript was a silent no-op carrying a false
    note, python overstated.

    That whole branch is now DELETED and delegated to fix_templates, which had
    always implemented the same operation for all nine languages -- see
    test_added_required_field_delegates_to_fix_templates. This test remains as
    the behavioural guard on the invariant that outlives either implementation:
    a note must never describe more than the code actually does, because the
    reviewer trusts the note."""
    from app.diff_engine import BreakingChange
    from app.consumer_finder import ConsumerMatch
    from app.fix_generator import _generate_with_template

    bc = BreakingChange("added_required_field", "/users", "post", "country",
                        "string", "request_body", "breaking", "country added")
    cases = {
        "python": (
            "import requests\n\n"
            "def create_user(name: str, email: str):\n"
            '    resp = requests.post("/users", json={"name": name, "email": email})\n'
            "    return resp.json()\n"
        ),
        "typescript": (
            "export async function createUser(name: string, email: string) {\n"
            '  const res = await client.post("/users", { name, email });\n'
            "  return res.data;\n}\n"
        ),
        "java": (
            "public class AccountsClient {\n"
            "    public User createUser(String name, String email) {\n"
            '        String body = mapper.writeValueAsString(Map.of("name", name, "email", email));\n'
            '        return http.post("/users", body);\n    }\n}\n'
        ),
    }

    for lang, original in cases.items():
        cm = ConsumerMatch("c", 1, "post", "high", "calls POST /users", lang)
        code, note = _generate_with_template(original, cm, bc)

        claims_complete = (
            ("included in" in note or "and request payload" in note)
            and "RIPPLE-ACTION-REQUIRED" not in note
        )
        # Present in BOTH the signature and the payload.
        actually_complete = code.count("country") >= 2

        assert not (claims_complete and not actually_complete), (
            f"[{lang}] note claims a complete fix but the field is not sent:\n"
            f"  note: {note}\n  code: {code}"
        )
        # An unchanged file opens no PR, so a note describing edits that did not
        # happen must not be emitted either.
        if code == original:
            assert "Added" not in note, (
                f"[{lang}] code unchanged but note claims an edit: {note}"
            )


def test_added_required_field_delegates_to_fix_templates():
    """added_required_field was the ONE change type fix_generator implemented
    inline; removed_field, field_renamed and field_type_changed all delegate to
    fix_templates. So one operation had two implementations that disagreed on
    its CATEGORY:

      fix_templates  treats add_required as JUDGMENT -- annotates construction
                     sites, and says explicitly it did not invent a value
      inline version treated it as mechanical -- appended a REQUIRED positional
                     parameter and wrote the field into the payload, which
                     breaks every existing caller with a TypeError and silently
                     decides what value to send

    Only the fix_templates path is exercised by tools/coverage_matrix.py, so the
    implementation production actually used was ungated.

    Pins that the duplicate does not come back: fix_generator must return byte
    identical output to a direct apply_fix_template call."""
    from app.diff_engine import BreakingChange
    from app.consumer_finder import ConsumerMatch
    from app.fix_generator import _generate_with_template, _call_site_hints
    from app.fix_templates import apply_fix_template, MARKER

    bc = BreakingChange("added_required_field", "/users", "post", "country",
                        "string", "request_body", "breaking", "country added")
    original = (
        "def create_user(name, email):\n"
        '    return post("/users", {"name": name, "email": email})\n'
    )

    for lang in ("python", "typescript", "java", "go", "ruby", "rust",
                 "kotlin", "csharp"):
        cm = ConsumerMatch("c", 1, "post", "high", "r", lang)
        got_code, got_note = _generate_with_template(original, cm, bc)
        want_code, want_note = apply_fix_template(
            code=original, language=lang, change_type="add_required",
            field_name="country", site_hints=_call_site_hints(bc),
        )
        assert got_code == want_code, f"[{lang}] fix_generator diverged from fix_templates"
        assert got_note == want_note, f"[{lang}] explanation diverged"
        # JUDGMENT contract: a non-empty diff (so a PR opens) that is marked.
        assert got_code != original, f"[{lang}] no diff -> no PR -> silence"
        assert MARKER in got_code, f"[{lang}] judgment fix is not marked"


def test_add_required_anchors_on_the_call_site_not_the_field():
    """add_required annotated by field-name variants -- but a NEWLY required
    field is by definition absent from consumer code, so that match could only
    ever miss. Measured both ways (field absent AND field already present): every
    add_required fix landed as a file-top marker saying "somewhere in this file,
    supply X". Honest, but useless for review at scale.

    The anchor has to be the CALL SITE -- the endpoint path the contract and the
    consumer both name, or the constructed type for proto/GraphQL engines. The
    file-top marker stays as the last resort, because an unchanged file opens no
    PR and detection would become silence."""
    from app.diff_engine import BreakingChange
    from app.consumer_finder import ConsumerMatch
    from app.fix_generator import _generate_with_template
    from app.fix_templates import MARKER

    def first_line_is_file_marker(code):
        return "no construction site detected" in code.split("\n")[0]

    rest = BreakingChange("added_required_field", "/users", "post", "country",
                          "string", "request_body", "breaking", "x")
    proto = BreakingChange("added_required_field", "User", "post", "country",
                           "string", "request_body", "breaking", "x")

    # 1. Literal path at the call site -> line-level.
    code, note = _generate_with_template(
        'def create_user(name, email):\n'
        '    return post("/users", {"name": name, "email": email})\n',
        ConsumerMatch("c", 1, "x", "high", "r", "python"), rest)
    assert not first_line_is_file_marker(code), code
    assert "call site" in note, note
    marked = [l for l in code.split("\n") if MARKER in l]
    assert len(marked) == 1 and marked[0].strip().startswith("#"), code

    # 2. URL built rather than written literally -> still line-level, via the
    #    trailing path segment.
    code, _ = _generate_with_template(
        "const url = `${base}/users/${id}`;\n"
        "await client.post(url, { name, email });\n",
        ConsumerMatch("c", 1, "x", "high", "r", "typescript"), rest)
    assert not first_line_is_file_marker(code), code

    # 3. proto/GraphQL: `path` holds a message name, matched in any casing.
    code, _ = _generate_with_template(
        "func f() {\n\tu := User{Name: n}\n\treturn send(u)\n}\n",
        ConsumerMatch("c", 1, "x", "high", "r", "go"), proto)
    assert not first_line_is_file_marker(code), code

    # 4. No anchor anywhere -> file-top marker, NOT an unchanged file.
    src = "def helper(a)\n  a + 1\nend\n"
    code, note = _generate_with_template(
        src, ConsumerMatch("c", 1, "x", "high", "r", "ruby"), rest)
    assert first_line_is_file_marker(code), code
    assert code != src, "unchanged file opens no PR -- detection becomes silence"


def test_language_detection_has_exactly_one_map():
    """There were EIGHT extension->language maps, plus TWO copies of the
    scannable-file decision, and they disagreed WITH EACH OTHER -- not merely
    drifted. Production saw 5 languages while the benchmark measured 15, so the
    published recall figure described a capability the deployed system lacked.

    Pins the STRUCTURE: any new inline map re-forks the concept, and no unit
    test of any individual function would fail when it does. This is the same
    guard shape as test_fix_generated_is_logged_exactly_once, which caught a
    real regression."""
    import os as _os
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    app_dir = _os.path.join(root, "app")

    # An extension->language mapping is recognisable by a quoted dotted
    # extension used as a dict key.
    probes = ('".py":', "'.py':", '".go":', "'.go':", '".ts":', "'.ts':",
              '".rb":', "'.rb':", '".java":', "'.java':")
    offenders = []
    for name in sorted(_os.listdir(app_dir)):
        if not name.endswith(".py") or name == "languages.py":
            continue
        body = open(_os.path.join(app_dir, name)).read()
        hits = [q for q in probes if q in body]
        if hits:
            offenders.append(f"{name}: {hits}")
    assert not offenders, (
        "extension->language map outside app/languages.py -- import from there "
        f"instead: {offenders}"
    )


def test_no_second_language_detector_or_file_filter_is_defined():
    """A delegating wrapper is still a second place to change, and a second
    place to forget. Only languages.py may DEFINE these; everyone else imports.

    history_learner keeps a thin method because it is called as self._detect_
    language() from inside the class, so it is allowed -- but
    test_every_detector_resolves_to_the_canonical_one proves it still returns
    the canonical answer."""
    import os as _os
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    app_dir = _os.path.join(root, "app")
    allowed = {("history_learner.py", "def _detect_language")}
    banned = ("def _detect_lang(", "def _detect_language(", "def detect_language(",
              "def _language_from_path(", "def _is_code_file(", "def is_scannable(")
    offenders = []
    for name in sorted(_os.listdir(app_dir)):
        if not name.endswith(".py") or name == "languages.py":
            continue
        body = open(_os.path.join(app_dir, name)).read()
        for b in banned:
            if b in body and (name, b.rstrip("(")) not in allowed:
                offenders.append(f"{name}: {b}")
    assert not offenders, (
        f"second definition of a canonical concept: {offenders}"
    )


def test_every_detector_resolves_to_the_canonical_one():
    """Identity where possible, behaviour everywhere. Catches the failure the
    structural tests cannot see: an alias that points somewhere else, or a
    module that quietly re-adds a private fallback.

    Includes the three extensions that exposed the original disagreements:
      .js   -> fix_generator_multi said typescript, six others said javascript
               (that module is now DELETED -- it had zero production callers;
                the contradiction is pinned below so it cannot return)
      .yaml -> only rag_engine knew it, and the file filter rejected it anyway
      .kts  -> two of eight knew it"""
    from app import languages
    from app.rag_engine import _detect_language as rag
    from app.consumer_finder import _detect_language as cf
    from app.multi_step_reasoning import _detect_language as msr
    from app.rag_retriever import _language_from_path as rr
    from app.webhook import _detect_lang as wh, _is_code_file as wh_isfile
    from app.history_learner import HistoryLearner

    # Same function OBJECT: no wrapper can have been slipped in.
    for name, fn in (("rag_engine", rag), ("consumer_finder", cf),
                     ("multi_step_reasoning", msr),
                     ("rag_retriever", rr), ("webhook", wh)):
        assert fn is languages.detect, f"{name} no longer resolves to languages.detect"
    assert wh_isfile is languages.is_scannable, "webhook re-forked is_scannable"

    # history_learner is a method, so check behaviour instead.
    hl = HistoryLearner.__new__(HistoryLearner)
    exts = (".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".go", ".rs", ".rb",
            ".kt", ".kts", ".cs", ".swift", ".php", ".scala", ".sc", ".dart",
            ".yaml", ".yml", ".sh", ".bash", ".zsh", ".md", ".proto")
    for e in exts:
        want = languages.detect("f" + e)
        got = hl._detect_language("f" + e)
        assert got == want, f"history_learner disagrees on {e}: {got} != {want}"

    # One sentinel. None and "generic" were both in use before.
    assert languages.detect("README.md") == languages.UNKNOWN
    assert languages.detect("a.js") == "javascript", "the .js contradiction is back"


def test_benchmark_and_production_share_the_language_path():
    """PropBench's replay.py imports the detector from Ripple to score recall.
    It used to import app.rag_engine._detect_language (15 languages) while the
    webhook ran its own (5), so the headline number could not describe the
    deployed system. Now both resolve to the same object -- assert it here, in
    Ripple, because the harness lives in a different repository and its import
    is the thing that must not silently re-point."""
    from app import languages
    from app.webhook import _detect_lang as production
    from app.rag_engine import _detect_language as harness_path
    assert production is harness_path is languages.detect, (
        "benchmark and production no longer share the language path"
    )


def test_scannable_admits_the_languages_matchers_exist_for():
    """The file filter rejected .yaml and .sh AFTER matchers were written for
    them. rag_engine's own comment records the cost: 137 files a real PR had to
    change were skipped for having no matcher -- 24 of 36 on kubernetes#109798.
    The matchers landed; the filter still said no, so the fix was invisible."""
    from app.languages import is_scannable, detect
    for path in ("deploy/values.yaml", "hack/local-up-cluster.sh",
                 "src/Main.kt", "src/Client.cs", "src/a.rs", "src/a.rb"):
        assert is_scannable(path), f"{path} ({detect(path)}) is not scannable"
    # Vendored and generated code must still be refused: those are never
    # hand-edited, so a PR touching one is always wrong.
    for path in ("node_modules/x/index.js", "vendor/y.go", "api/user.pb.go",
                 "types/index.d.ts", "dist/app.min.js", "README.md"):
        assert not is_scannable(path), f"{path} should not be scannable"


def test_breaking_change_declares_the_fields_that_are_read():
    """Three sites read new_name / old_type off BreakingChange via getattr on a
    dataclass that never DECLARED them, so every read resolved to the falsy
    default and the code behind it was dead:

      fix_generator  the rename branch required new_name to delegate, so every
                     rename fell through to "Unsupported change type" -->
                     unchanged code --> no PR --> silence.
      fix_generator  the type-change branch could only recover old_type by
                     string-splitting field_type on an arrow.
      webhook        the rename commit message rendered, literally,
                     "fix: Rename field 'phone_number' to 'new name'".

    getattr with a default is the same failure shape as
    os.environ.get("ANTHROPIC_API_KEY"): a read that cannot fail, so nothing
    errors and the dead branch looks live."""
    import dataclasses
    from app.diff_engine import BreakingChange
    names = {f.name for f in dataclasses.fields(BreakingChange)}
    for required in ("new_name", "old_type", "new_type"):
        assert required in names, f"BreakingChange does not declare {required}"

    # Defaulted, so the 65 existing 8-positional construction sites still work.
    bc = BreakingChange("field_renamed", "/u", "get", "f", "string", "b",
                        "breaking", "d")
    assert bc.new_name == "" and bc.old_type == "" and bc.new_type == ""


def test_no_phantom_getattr_on_breaking_change():
    """Pins the STRUCTURE, because declaring the fields does not stop the next
    reader from reaching for one that does not exist. A getattr with a default
    silently succeeds, which is exactly why these three survived.

    Walks the AST rather than grepping text. The first version of this test
    grepped, and immediately failed on the COMMENTS in diff_engine.py,
    fix_generator.py and webhook.py that quote the old code to explain the bug --
    a gate that forbids describing the defect it prevents is worse than no gate,
    because the fix is to delete the explanation."""
    import ast
    import os as _os
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    app_dir = _os.path.join(root, "app")
    watched = {"breaking_change", "change", "bc"}
    fields = {"new_name", "old_type", "new_type", "field_name", "change_type",
              "field_type"}
    offenders = []

    for name in sorted(_os.listdir(app_dir)):
        if not name.endswith(".py"):
            continue
        tree = ast.parse(open(_os.path.join(app_dir, name)).read(), filename=name)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
                    and len(node.args) >= 2):
                continue
            target, attr = node.args[0], node.args[1]
            if (isinstance(target, ast.Name) and target.id in watched
                    and isinstance(attr, ast.Constant) and attr.value in fields):
                offenders.append(f"{name}:{node.lineno} getattr({target.id}, "
                                 f"{attr.value!r})")

    assert not offenders, (
        "BreakingChange field read via getattr -- declare it on the dataclass "
        f"and read it directly, so a missing field fails loudly: {offenders}"
    )


def test_rename_and_type_change_never_produce_silence():
    """Both branches used to return the code unchanged, and unchanged code opens
    no PR -- so a detected break produced nothing at all. Measured before:
    rename was silent ALWAYS (new_name could not exist), and type-change was
    silent unless field_type happened to be formatted "old -> new".

    Also pins that the fallbacks do not LIE. Routing an unnamed rename through
    field_removed would delete references to a field that still exists under a
    new name, and would report "Removed all references" while doing it."""
    from app.diff_engine import BreakingChange
    from app.consumer_finder import ConsumerMatch
    from app.fix_generator import _generate_with_template
    from app.fix_templates import MARKER

    sources = {
        "python": ("class W:\n    phone_number: str\n\n"
                   "def f(u):\n    return u.phone_number\n"),
        "javascript": "const x = obj.phoneNumber;\n",
        "ruby": "x = obj.phone_number\n",
        "java": "class W { private String phoneNumber; }\n",
    }

    def change(ct, **kw):
        ft = kw.pop("field_type", "string")
        return BreakingChange(ct, "/u", "get", "phone_number", ft, "b",
                              "breaking", "d", **kw)

    cases = [
        ("rename with a target", lambda: change("field_renamed", new_name="phone")),
        ("rename with no target", lambda: change("field_renamed")),
        ("type change via fields",
         lambda: change("field_type_changed", old_type="string", new_type="int32")),
        # No arrow in field_type: this is the shape the precedence bug broke.
        ("type change, fields only, plain field_type",
         lambda: change("field_type_changed", field_type="int32",
                        old_type="string", new_type="int32")),
        ("type change, arrow only",
         lambda: change("field_type_changed", field_type="string \u2192 int32")),
        ("type change, nothing known", lambda: change("field_type_changed")),
    ]

    for lang, src in sources.items():
        cm = ConsumerMatch("c", 1, "x", "high", "r", lang)
        for label, factory in cases:
            out, note = _generate_with_template(src, cm, factory())
            assert out != src, f"[{lang}] {label}: unchanged -> no PR -> silence"
            # Either a real transform, or an honest marked partial.
            transformed = "Renamed" in note or "Changed type" in note
            assert transformed or MARKER in out, (
                f"[{lang}] {label}: neither transformed nor marked: {note}")
            assert "Removed all references" not in note, (
                f"[{lang}] {label}: a rename/type-change must never report a "
                f"removal -- the field still exists: {note}")


def test_type_change_reads_the_fields_not_the_display_string():
    """The old expression was

        getattr(bc,'old_type','') or bc.field_type.split(' -> ')[0] if COND else ''

    which Python groups as (a or b) if COND else '', because a conditional
    expression binds looser than `or`. So with no arrow in field_type, old_type
    became '' EVEN IF the attribute held a value. The branch fired only on a
    string-formatting accident."""
    from app.diff_engine import BreakingChange
    from app.consumer_finder import ConsumerMatch
    from app.fix_generator import _generate_with_template

    # TypeScript source for the TypeScript handler. An earlier version of this
    # test handed PYTHON source to it, so no declaration matched and it took the
    # annotate path -- passing on a note that happened to contain the strings
    # being asserted rather than on a real edit.
    src = "interface W {\n  phoneNumber: string;\n}\n"
    cm = ConsumerMatch("c", 1, "x", "high", "r", "typescript")

    # field_type carries NO arrow; the declared fields must still be used.
    bc = BreakingChange("field_type_changed", "/u", "get", "phone_number",
                        "int32", "b", "breaking", "d",
                        old_type="string", new_type="int32")
    _, note = _generate_with_template(src, cm, bc)
    # TypeScript's native spelling of int32 is `number`; the contract pair is
    # reported alongside it so the PR body still names what the engine emitted.
    assert "number" in note and "int32" in note, note

    # Both arrow spellings still parse, for engines that only set field_type.
    for arrow in (" \u2192 ", " -> "):
        bc = BreakingChange("field_type_changed", "/u", "get", "phone_number",
                            f"string{arrow}int32", "b", "breaking", "d")
        _, note = _generate_with_template(src, cm, bc)
        assert "number" in note and "int32" in note, (arrow, note)


def test_type_change_engines_populate_the_structured_fields():
    """Nine engines emitted a type-change dialect, and every one COMPUTED the
    old and new type locally, formatted them into a display string, and threw
    the structured values away:

        field_type=f"{old_type} -> {new_type}"

    The consumer then had to recover them by splitting that string -- which is
    how the precedence bug in fix_generator came to exist, and why it depended on
    a formatting accident. migration_diff used a unicode arrow while the others
    used ASCII, and proto_diff passed only the NEW type, so the split could never
    work there at all.

    Asserts the values now travel as data, per engine, on real input."""
    from app.proto_diff import diff_proto
    from app.avro_diff import diff_avro
    from app.jsonschema_diff import diff_jsonschema
    from app.thrift_diff import diff_thrift
    from app.smithy_diff import diff_smithy
    from app.trpc_diff import diff_trpc

    cases = [
        ("proto", diff_proto,
         "message U {\n  string age = 1;\n}\n",
         "message U {\n  int32 age = 1;\n}\n", "u.proto", "string", "int32"),
        ("avro", diff_avro,
         '{"type":"record","name":"U","fields":[{"name":"age","type":"string"}]}',
         '{"type":"record","name":"U","fields":[{"name":"age","type":"int"}]}',
         "u.avsc", "string", "int"),
        ("jsonschema", diff_jsonschema,
         '{"properties":{"age":{"type":"string"}}}',
         '{"properties":{"age":{"type":"integer"}}}',
         "s.json", "string", "integer"),
        ("thrift", diff_thrift,
         "struct U {\n  1: string age\n}\n",
         "struct U {\n  1: i32 age\n}\n", "u.thrift", "string", "i32"),
    ]
    for name, fn, old, new, path, want_old, want_new in cases:
        changes = [c for c in fn(old, new, path) if "type_changed" in c.change_type]
        assert changes, f"{name}: no type change detected"
        c = changes[0]
        assert c.old_type == want_old, f"{name}: old_type {c.old_type!r} != {want_old!r}"
        assert c.new_type == want_new, f"{name}: new_type {c.new_type!r} != {want_new!r}"

    # smithy and trpc use different fixture shapes; assert only that the fields
    # are populated rather than pinning their type vocabulary.
    for name, fn, old, new, path in (
        ("smithy", diff_smithy,
         "structure U {\n    age: String\n}\n",
         "structure U {\n    age: Integer\n}\n", "u.smithy"),
        ("trpc", diff_trpc,
         "export const r = router({ getUser: publicProcedure.query(() => {}) });",
         "export const r = router({ getUser: publicProcedure.mutation(() => {}) });",
         "r.ts"),
    ):
        changes = [c for c in fn(old, new, path) if "type_changed" in c.change_type]
        if not changes:
            continue   # fixture did not trigger this engine's path
        c = changes[0]
        assert c.old_type and c.new_type, (
            f"{name}: emitted a type change with empty old_type/new_type: "
            f"{c.old_type!r} -> {c.new_type!r}")


def test_proto_detects_a_field_rename_and_respects_reserved():
    """A rename LOOKS like a removal plus an addition in a text diff, which is
    why no engine emitted rename_field -- the audit reported it as a canonical
    operation emitted by nothing, so fix_generator's rename branch was dead on
    both sides.

    In protobuf the field NUMBER is the wire identity: the format carries
    numbers, not names. A field whose number AND type reappear under a different
    name has been renamed, and this is not a similarity guess. The distinction
    matters because reporting it as a removal tells the consumer to DELETE
    references to a field that still exists.

    RESERVED must win: reserving is the protobuf idiom for deliberate
    retirement, so a coincidental number match must not override the author
    saying "gone"."""
    from app.proto_diff import diff_proto

    old = "message User {\n  string phone_number = 3;\n  string email = 1;\n}\n"

    # Same number, same type, new name -> rename.
    renamed = diff_proto(old, "message User {\n  string phone = 3;\n"
                              "  string email = 1;\n}\n", "u.proto")
    assert len(renamed) == 1, renamed
    assert renamed[0].change_type == "field_renamed", renamed[0].change_type
    assert renamed[0].new_name == "phone", renamed[0].new_name

    # RESERVED wins. The fixture must make the guard the DECIDING factor: an
    # earlier version reserved number 3 while giving 'phone' number 4, so the
    # number comparison already failed and deleting the guard did not change the
    # result -- the test passed for the wrong reason. Reserving the NAME while
    # 'phone' genuinely reuses number 3 is valid proto (reserving a name does not
    # reserve its number) and isolates the guard.
    reserved = diff_proto(old, 'message User {\n  reserved "phone_number";\n'
                               "  string phone = 3;\n  string email = 1;\n}\n",
                          "u.proto")
    kinds = {c.change_type for c in reserved}
    assert "field_removed" in kinds, kinds
    assert "field_renamed" not in kinds, "reserved must not be read as a rename"

    # Different type at the same number -> not a rename.
    retyped = diff_proto(old, "message User {\n  int32 phone = 3;\n"
                              "  string email = 1;\n}\n", "u.proto")
    assert "field_renamed" not in {c.change_type for c in retyped}


def test_proto_rename_reaches_a_real_fix_end_to_end():
    """The plumbing, the detection and the template must line up. Each was
    individually correct at some point today while the chain was broken:
    BreakingChange lacked new_name, then no engine emitted rename_field."""
    from app.proto_diff import diff_proto
    from app.consumer_finder import ConsumerMatch
    from app.fix_generator import _generate_with_template
    from app.smart_consumer_finder import find_residual_references

    change = diff_proto("message User {\n  string phone_number = 3;\n}\n",
                        "message User {\n  string phone = 3;\n}\n",
                        "api/user.proto")[0]
    consumer = ('def show(u):\n'
                '    print(u.phone_number)\n'
                '    return {"phone_number": u.phone_number}\n')
    cm = ConsumerMatch("clients/user.py", 2, "u.phone_number", "high", "r",
                       "python")
    fixed, note = _generate_with_template(consumer, cm, change)

    assert "u.phone_number" not in fixed, "attribute access was not renamed"
    assert "u.phone" in fixed
    assert "phone" in note and "phone_number" in note

    # The template PRESERVES string literals by design and says so. For a proto
    # contract that literal is the JSON wire name, so it is a live reference --
    # which is why residual detection must catch it rather than the PR claiming
    # a complete rename.
    assert "String literals and comments preserved" in note, note
    residual = find_residual_references(fixed, "phone_number", "python")
    assert residual, "a surviving reference must be flagged, not left silent"


def test_type_change_never_writes_a_contract_type_into_source():
    """change_field_type was wrong in all NINE languages, in three ways, and
    none of them reported a problem:

      java kotlin rust python ruby javascript  SILENT no-op -- 0 replacements, so
          no PR opened and a detected break produced silence.
      typescript csharp  WROTE THE CONTRACT NAME INTO SOURCE, producing
          `phoneNumber: int32;` and `public int32 PhoneNumber` -- neither
          compiles -- while reporting "1 type annotations updated".
      go  correct BY COINCIDENCE: proto's int32 is spelled int32 in Go too.

    Root cause was one thing, not nine: engines emit the CONTRACT's vocabulary
    (proto int32, JSON Schema integer, Thrift i64) and the handlers replaced
    those tokens literally in source, where they never appear.

    The confident-but-broken case is the worst of the three, because a reviewer
    trusts a diff that cannot build. So this pins BOTH directions: a contract
    name must never reach source, and the operation must never go silent."""
    from app.fix_templates import apply_fix_template, MARKER, native_type

    sources = {
        "go": "type W struct {\n\tPhoneNumber string\n}\n",
        "typescript": "interface W {\n  phoneNumber: string;\n}\n",
        "csharp": "class W {\n  public string PhoneNumber { get; set; }\n}\n",
        "java": "class W {\n  private String phoneNumber;\n}\n",
        "kotlin": "data class W(\n    val phoneNumber: String,\n)\n",
        "rust": "struct W {\n    phone_number: String,\n}\n",
        "python": "class W:\n    phone_number: str\n",
        "javascript": 'const w = {\n  phoneNumber: "x",\n};\n',
        "ruby": "class W\n  attr_accessor :phone_number\nend\n",
    }
    # Contract spellings that appear in NO language's type system.
    contract_only = ("int32", "int64", "sint64", "fixed32", "integer")

    for lang, src in sources.items():
        out, note = apply_fix_template(src, lang, "type_changed", "phone_number",
                                       old_type="string", new_type="int32")
        # 1. Never silent: unchanged code opens no PR.
        assert out != src, f"[{lang}] type change produced no diff -> silence"

        # 2. Either a real edit, or an honest marked partial -- never both absent.
        marked = MARKER in out
        assert marked or out != src, f"[{lang}] neither edited nor marked"

        # 3. A contract-only spelling must never land in a CODE line. It may
        #    appear in a RIPPLE-ACTION-REQUIRED comment, which is the point.
        code_lines = [l for l in out.split("\n") if MARKER not in l]
        for token in contract_only:
            if lang == "go" and token == "int32":
                continue        # genuinely Go's own spelling
            assert not any(token in l for l in code_lines), (
                f"[{lang}] wrote contract type {token!r} into source -- this "
                f"does not compile:\n{out}")

    # 4. Type MAPS but no declaration matches. Every fixture above declares the
    #    field, so the annotate fallback was never the deciding factor -- the
    #    same fixture artifact that made the `reserved` proto test pass for the
    #    wrong reason. This Java file only READS the field, so the mapped
    #    replacement finds nothing and silence is the only other outcome.
    reader_only = "class W {\n  int f(Other u) { return u.phoneNumber; }\n}\n"
    out, note = apply_fix_template(reader_only, "java", "type_changed",
                                   "phone_number", old_type="string",
                                   new_type="int32")
    assert out != reader_only, (
        "type mapped but no declaration matched -> returned unchanged code, "
        "which opens no PR and turns detection into silence")
    assert MARKER in out, f"must be marked when it cannot transform: {note}"

    # 5. The mapping itself: refuse rather than guess.
    assert native_type("typescript", "int32") == "number"
    assert native_type("csharp", "int32") == "int"
    assert native_type("java", "int64") == "long"
    assert native_type("rust", "int32") == "i32"
    assert native_type("python", "string") == "str"
    assert native_type("typescript", "SomeCustomMessage") == "", (
        "an unmapped type must return '' so the caller annotates instead of "
        "writing a name that does not compile")
    # Dialect spellings normalise onto one table.
    assert native_type("java", "integer") == native_type("java", "int32")
    assert native_type("kotlin", "i64") == native_type("kotlin", "int64")
    assert native_type("go", "boolean") == native_type("go", "bool")


def test_capability_registry_derives_rather_than_declares():
    """The registry must COMPUTE the three derivable facts, not restate them.

    A hand-maintained capability table would become the ninth place capability
    information lives, and it would drift exactly as the eight language
    detectors did. So this pins two things:

      1. detect() is derived from the AST of each engine, not from text. The
         first version grepped for quoted change-type names and reported that
         OpenAPI could detect rename_field -- because diff_engine.py has the
         comment `change_type: str  # "added_required_field", "removed_field",
         "renamed_field"`. A registry that infers capability from prose
         OVER-claims, which is the dangerous direction.
      2. The matrix is sparse. 53 of 120 naive (contract, operation) pairs come
         from engines; remove_package adds one per contract because it is
         detected at the push-event layer, not by any differ."""
    from app import capabilities as cap

    # 1. Prose must not create a capability.
    assert not cap.detect("openapi", "rename_field"), (
        "openapi claims rename_field -- derived from a comment, not from code")
    assert cap.detect("proto", "rename_field"), (
        "proto genuinely infers field renames by field number")

    # 2. Sparse, and event-layer ops counted for every contract.
    s = cap.summary()
    assert s["naive_cross_product"] == 120
    assert 50 < s["detectable_pairs"] < 80, s["detectable_pairs"]
    assert "remove_package" in cap.event_layer_ops()
    for contract in cap.CONTRACT_ENGINES:
        assert cap.detect(contract, "remove_package"), contract

    # 3. tRPC is the sparsity proof: it emits two operations, not twelve.
    trpc = {op for c, op in cap.detectable_pairs() if c == "trpc"}
    assert "remove_operation" in trpc and "remove_field" not in trpc, trpc

    # 4. Nothing claims a fix for a non-breaking or wire-only operation.
    from app.languages import languages
    for op in ("add_optional", "wire_incompatible"):
        assert not any(cap.generate_fix(l, op) for l in languages()), (
            f"{op} must not claim a code transformation")

    # 5. The judgment/mechanical inversion is real and worth pinning: annotating
    #    needs only a comment token, so JUDGMENT reaches every language, while
    #    MECHANICAL needs language-specific patterns and reaches fewer.
    judgment = sum(1 for l in languages() if cap.generate_fix(l, "add_required"))
    mechanical = sum(1 for l in languages() if cap.generate_fix(l, "remove_field"))
    assert judgment == len(languages()), judgment
    assert mechanical < judgment, (mechanical, judgment)


def test_unknown_validation_is_never_valid():
    """app/validated_fix.py ends its dispatch with

        else:
            # Can't validate -- assume valid
            return True, ""

    Measured, that returns VALID for: `phoneNumber: int32;` (TypeScript has no
    int32), `public int32 PhoneNumber` (C# has no int32), a half-fix that accepts
    a parameter and never sends it, and the literal string "!!! not rust". Six
    for six on garbage.

    For a product that modifies other people's code, UNKNOWN IS NOT VALID -- and
    it is not INVALID either, because claiming a fix is broken when you did not
    check is a different lie. Hence three states, with no path from absence of
    evidence to VALID."""
    from app.capability_claims import (ValidationState, validation_state,
                                       validates, VALIDATORS)

    assert len(ValidationState) == 3
    # Declaring a toolchain is not having one.
    for lang, spec in VALIDATORS.items():
        if not spec.is_wired:
            assert validation_state(lang) is ValidationState.UNABLE_TO_VALIDATE
            assert not validates(lang), f"{lang} counted as validated"
    # A language with no declared validator is also unable, never valid.
    assert validation_state("cobol") is ValidationState.UNABLE_TO_VALIDATE
    assert not validates("cobol")
    # UNABLE must not be truthy-coerced anywhere downstream.
    assert bool(ValidationState.UNABLE_TO_VALIDATE.value)  # the STRING is truthy
    assert not validates("swift")                          # the PREDICATE is not


def test_production_readiness_cannot_be_declared():
    """production is a pure function of the other five. If it could be set by
    hand it would be the same unverified assertion the registry replaced -- the
    repo claimed 12-language fix generation while change_field_type was broken in
    all nine languages it covered.

    So: no module may define a production/PRODUCTION_READY table, and flipping
    any one input must flip the verdict."""
    import os as _os
    from app import capability_claims as cc

    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    app_dir = _os.path.join(root, "app")
    banned = ("PRODUCTION_READY = {", "PRODUCTION = {", "production = {",
              "PRODUCTION_SUPPORTED = {")
    offenders = []
    for name in sorted(_os.listdir(app_dir)):
        if not name.endswith(".py"):
            continue
        body = open(_os.path.join(app_dir, name)).read()
        offenders += [f"{name}: {b}" for b in banned if b in body]
    assert not offenders, (
        f"production must be COMPUTED, not declared: {offenders}")

    # Stage 5 changed this. `validate_ok` is no longer 0: TypeScript has a real
    # container-backed runner, so 63 cells now validate. But `e2e_ok` is still 0,
    # so no FIXABLE cell is production-ready -- the blocker moved from two facts to
    # one rather than disappearing.
    #
    # This assertion deliberately does NOT hardcode 63. Pinning the number would
    # make adding a second language's validator a test failure, which punishes
    # progress; what must hold is that validation is real AND that real validation
    # alone does not confer readiness.
    s = cc.summary()
    assert s["validate_ok"] > 0, \
        "TypeScript validation is wired -- if this is 0 again, is_wired stopped resolving"
    assert s["e2e_ok"] > 0, \
        "Stage 6 registered one end-to-end fixture; if this is 0 the evidence was lost"
    fixable = [r for r in cc.claim_matrix()
               if "generate_fix" in cc.required_facts(r["operation"])]
    ready = [(r["language"], r["contract"], r["operation"])
             for r in fixable if r["production"]]
    # Every ready FIXABLE cell must be one that earned it, and at least one must
    # exist. Not zero (the mechanism must be able to say yes) and not any cell
    # without a fixture (breadth without proof is the thing being refused).
    #
    # This was `== [("typescript", "openapi", "remove_field")]` while one cell was
    # ready. A literal is the wrong assertion here for the same reason production
    # must be computed rather than declared: it asserts an inventory, not a rule.
    # The rule is "ready iff proven", and it is checked in both directions below.
    proven = sorted(cell for cell in cc.E2E_FIXTURES)
    assert ready, "no FIXABLE cell is production-ready -- the mechanism cannot say yes"
    assert sorted(ready) == proven, (sorted(ready), proven)
    wire = [r for r in cc.claim_matrix()
            if cc.required_facts(r["operation"]) == ("detect",)]
    assert wire and all(r["production"] for r in wire), (
        "wire_only/non_breaking must be ready on detection alone")
    # Detection and fix generation are NOT the blockers -- both are mostly true.
    assert s["generate_fix_ok"] > s["cells"] // 2, s["generate_fix_ok"]

    # The verdict must respond to its inputs. Before Stage 5 this cell was blocked
    # on TWO facts; TypeScript validation is now real, so exactly one remains. That
    # the count MOVED is the point -- a predicate whose output never changes when a
    # fact changes is not computing anything.
    # Stage 6: this cell now has BOTH facts, so there are no blocking reasons left.
    # That the list went 2 -> 1 -> 0 as each fact landed is how you can tell the
    # predicate computes over inputs rather than reciting a constant.
    reasons = cc.blocking_reasons("typescript", "openapi", "remove_field")
    assert reasons == [], reasons

    # A sibling cell differing in ONE dimension must still be blocked, otherwise
    # readiness leaked across the matrix. DERIVED, not named: this listed
    # python/openapi/remove_field until that cell shipped, which made adding a cell a
    # test failure for the third time. The invariant is "readiness does not leak",
    # and it is checked by walking every one-dimension neighbour of a ready cell and
    # requiring that the unproven ones are blocked.
    ready = sorted(cell for cell in cc.E2E_FIXTURES if cc.production_ready(*cell))
    assert ready, "no cell is ready -- this check would be vacuous"
    from app.capabilities import detectable_pairs as _pairs
    from app import languages as _langs
    neighbours_checked = 0
    for lang, contract, op in ready:
        candidates = (
            [(l, contract, op) for l in sorted(_langs.languages()) if l != lang]
            + [(lang, c, o) for c, o in _pairs() if (c, o) != (contract, op)]
        )
        for cell in candidates:
            if cell in cc.E2E_FIXTURES:
                continue          # proven in its own right, not a leak
            if "e2e_tested" not in cc.required_facts(cell[2]):
                # wire_only and non_breaking ops require ONLY detect -- generating a
                # fix for them would be wrong, so they are ready without a fixture by
                # design. Including them made this check fail on
                # python/proto/wire_incompatible, which is correct behaviour.
                continue
            assert cc.blocking_reasons(*cell), \
                f"{'/'.join(cell)} is ready without a fixture -- readiness leaked"
            neighbours_checked += 1
    assert neighbours_checked > 50, neighbours_checked

    # And a language with NO ValidatorSpec at all still reports both. This used to
    # ask about `go`, which was a declaration with an empty implemented_by; go is now
    # wired, so its only remaining blocker is the missing fixture. `java` has no spec
    # whatsoever, which is the state this assertion is actually about -- and unlike
    # go it cannot be quietly wired out from under the test.
    unvalidated = cc.blocking_reasons("java", "openapi", "remove_field")
    assert any("UNABLE_TO_VALIDATE" in r for r in unvalidated), unvalidated
    assert any("fixture" in r for r in unvalidated), unvalidated
    assert "java" not in cc.VALIDATORS, \
        "java gained a validator -- point this assertion at another unvalidated language"


def test_every_e2e_claim_names_the_test_that_proves_it():
    """A boolean e2e_tested: True is a comment. A test NAME is checkable.

    E2E_FIXTURES is empty today and that is correct, not an oversight: the bar is
    detection -> consumer discovery -> fix -> VALIDATION -> PR body, and
    validation does not exist. Several tests here cover diff -> fix, and one posts
    a synthetic push payload, but none reach a validated PR.

    The invariant is what matters: any claim added later must name a callable that
    exists in this module, so CI can confirm the evidence is real."""
    import tests.test_regression as self_mod
    from app.capability_claims import E2E_FIXTURES, e2e_tested

    for cell, test_name in E2E_FIXTURES.items():
        assert test_name, f"{cell} claims e2e with no evidence"
        assert hasattr(self_mod, test_name), (
            f"{cell} names {test_name!r}, which does not exist in the suite")
        assert callable(getattr(self_mod, test_name))

    # And an unclaimed cell must not read as tested. DERIVED: this named
    # python/openapi/remove_field until that cell shipped. The rule is
    # "e2e_tested iff registered", checked in both directions across the whole
    # matrix, which cannot be invalidated by adding a fixture.
    from app.capabilities import detectable_pairs as _pairs
    from app import languages as _langs
    unclaimed = 0
    for lang in sorted(_langs.languages()):
        for contract, op in _pairs():
            claimed = (lang, contract, op) in E2E_FIXTURES
            assert e2e_tested(lang, contract, op) == claimed, \
                f"{lang}/{contract}/{op}: e2e_tested disagrees with E2E_FIXTURES"
            unclaimed += 0 if claimed else 1
    assert unclaimed > 100, unclaimed
    for cell, test_name in E2E_FIXTURES.items():
        assert e2e_tested(*cell), cell


def test_cli_and_production_discover_the_same_consumers():
    """consumer_finder.find_consumers matched on the ENDPOINT PATH and HTTP
    METHOD while the webhook matched on the FIELD SYMBOL. Not two
    implementations of one question -- two different questions. So `ripple scan`
    could report a different consumer set than the service would for the
    identical change, and neither was wrong on its own terms. A local command
    that is not a preview of the service is worse than no local command.

    find_consumers now owns only the directory walk and the ConsumerMatch
    conversion; matching is delegated to smart_consumer_finder, which the webhook
    and the PropBench harness both use.

    Also pins the filter: the walk uses languages.is_scannable(), so vendored and
    generated files are refused. The old extension-only check accepted
    node_modules/*.js and *.pb.go, which the webhook has always rejected."""
    import os as _os
    import tempfile
    from app.diff_engine import BreakingChange
    from app.consumer_finder import find_consumers
    from app.smart_consumer_finder import find_matches_in_file
    from app.languages import detect, is_scannable

    files = {
        "svc/client.py": "def show(u):\n    return u.phone_number\n",
        "web/app.ts": "const p = user.phoneNumber;\n",
        "deploy/cfg.yaml": "field: phone_number\n",
        "node_modules/x.js": "const p = o.phoneNumber;\n",
        "api/user.pb.go": "PhoneNumber string\n",
    }
    with tempfile.TemporaryDirectory() as d:
        for rel, body in files.items():
            path = _os.path.join(d, rel)
            _os.makedirs(_os.path.dirname(path), exist_ok=True)
            open(path, "w").write(body)

        bc = BreakingChange("field_removed", "/users", "get", "phone_number",
                            "string", "b", "breaking", "x")
        cli = {_os.path.relpath(m.file_path, d) for m in find_consumers([d], bc)}

        # What production's matcher finds, filtered by production's own file rule.
        expected = set()
        for rel in files:
            path = _os.path.join(d, rel)
            if not is_scannable(path):
                continue
            if find_matches_in_file(open(path).read(), path, "phone_number",
                                    detect(path)):
                expected.add(rel)

        assert cli == expected, (
            f"CLI and production disagree.\n  cli={sorted(cli)}\n  "
            f"production={sorted(expected)}")
        # And the filter is doing real work in this fixture.
        assert "node_modules/x.js" not in cli, "vendored file scanned"
        assert "api/user.pb.go" not in cli, "generated file scanned"
        assert "deploy/cfg.yaml" in cli, (
            "yaml consumer missed -- the old extension-only filter excluded it")


def test_every_fix_attempt_ends_in_a_stated_outcome():
    """`fixed_code == content` means no PR opens -- correct as a PR rule,
    disastrous as an outcome. The fix loop logged fix_generated {changed: false}
    and stopped: a fact about the code, not a statement about what happened. Every
    silent-failure class found this year presented identically as "nothing
    happened" -- the rename that fell through on a missing dataclass field, the
    type change that no-opped in 6 of 9 languages, the `git rm` never read from
    the payload, the 44 change types that reached fix_templates as "Unknown".

    The outcome is DERIVED inside the fix_generated funnel, not passed by callers,
    for the same reason vector_for() derives the vector: a parameter someone must
    remember to set is how the package vector ended up built, tested, CI-gated and
    unreachable from production."""
    import tempfile
    from app.outcomes import Outcome, terminal_outcome, blocked_reason
    from app.fix_templates import MARKER

    src = "x = obj.phone_number\n"

    # 1. The derivation, including the case that used to be silence.
    assert terminal_outcome("field_removed", src, "y = 1\n") is Outcome.FIX_GENERATED
    assert terminal_outcome("field_removed", src,
                            f"# {MARKER}: verify\n" + src) is Outcome.HUMAN_ACTION_REQUIRED
    assert terminal_outcome("field_removed", src, src) is Outcome.BLOCKED
    # wire_only: unchanged is CORRECT, and must not read as a refusal. Collapsing
    # these two would repeat the capability registry's mistake of demanding a
    # transformation from a category that forbids one.
    assert terminal_outcome("field_number_changed", src,
                            src) is Outcome.NO_CHANGE_REQUIRED
    assert terminal_outcome("field_added", src, src) is Outcome.NO_CHANGE_REQUIRED

    # 2. A BLOCKED outcome without a reason is still silence.
    reason = blocked_reason("field_removed", "Unsupported change type for template fix")
    assert "remove_field" in reason and reason.strip(), reason

    # 3. It is actually EMITTED -- reachability, not just implementation.
    old = os.environ.get("RIPPLE_DATA_DIR")
    os.environ["RIPPLE_DATA_DIR"] = tempfile.mkdtemp()
    try:
        import importlib
        from app import activity
        importlib.reload(activity)
        activity.reset()
        from app.webhook import _log_fix_generated
        _log_fix_generated("o/c", "svc/a.py", False, "[template] unsupported",
                           change_type="field_removed", original_code=src,
                           fixed_code=src,
                           explanation="Unsupported change type for template fix")
        outcomes = [e for e in activity.all_events() if e["action"] == "outcome"]
        assert len(outcomes) == 1, f"expected one outcome event, got {outcomes}"
        assert outcomes[0]["outcome"] == "BLOCKED", outcomes[0]
        assert outcomes[0].get("reason"), "BLOCKED with no reason is still silence"
    finally:
        if old is None:
            os.environ.pop("RIPPLE_DATA_DIR", None)
        else:
            os.environ["RIPPLE_DATA_DIR"] = old


def test_absent_is_distinguishable_from_unreachable():
    """Three of the four real bugs in the fail-silent triage were ONE shape: an
    error path that conflates "it is not there" with "I could not look".

    That shape has now appeared four times in this codebase -- the 403-vs-404
    cache poisoning in PropBench twice, one false published claim about PRs that
    did exist, and bitbucket_support.get_file returning "" for any HTTPError. A
    caller reading "" concludes the spec does not exist, i.e. NO BREAKING
    CHANGES."""
    import inspect
    from app import bitbucket_support
    from app.jsonschema_diff import parse_json_schema, SchemaParseError

    # 1. Only 404 may be treated as absent.
    src = inspect.getsource(bitbucket_support.BitbucketClient.get_file)
    assert "e.code == 404" in src, (
        "get_file must special-case 404; any other status means 'could not look'")
    assert "raise" in src, "non-404 must propagate, not return ''"

    # 2. A malformed schema must not look like an empty schema.
    for bad in ("{not json", "[1, 2, 3]", ""):
        try:
            parse_json_schema(bad)
            raise AssertionError(f"parsed {bad!r} without complaint")
        except SchemaParseError:
            pass
    assert parse_json_schema('{"properties": {}}') == {"properties": {}}


def test_unreadable_reserved_list_never_becomes_a_rename():
    """reserved_numbers is what tells a DELIBERATE REMOVAL from a rename. A
    `reserved` statement the regex cannot match is INVISIBLE to the parse loop, so
    a malformed list under-reports -- and under-reporting flips a removal into a
    rename, telling consumers to rename references to a field that is gone.

    The first version of this fix only handled malformed ENTRIES inside a matched
    statement, which missed the real case: `reserved 3-abc;` matches neither regex,
    so the handler never ran. Statement count vs match count catches it."""
    from app.proto_diff import diff_proto

    old = "message User {\n  string phone_number = 3;\n}\n"

    def kinds(new):
        return {c.change_type for c in diff_proto(old, new, "u.proto")}

    # Unreadable reserved list -> refuse to infer a rename.
    assert "field_renamed" not in kinds(
        'message User {\n  reserved 3-abc;\n  string phone = 3;\n}\n')
    # Well-formed reserved -> a removal, in all three spellings.
    for reserved in ('reserved "phone_number";', "reserved 3;", "reserved 2-4;"):
        got = kinds(f"message User {{\n  {reserved}\n  string phone = 9;\n}}\n")
        assert "field_renamed" not in got, (reserved, got)
    # No reserved statement -> a rename, which is the whole point of the signal.
    assert "field_renamed" in kinds(
        "message User {\n  string phone = 3;\n}\n")


def test_fail_silent_gate_rejects_an_unexplained_swallow():
    """The gate enforces that no silent path is UNEXPLAINED -- not that none exist.

    Three things had to be true before this could gate anything, and two of them
    were wrong when Stage 5 started:

    1. The triage keyed on (file, LINE, func) and had already detached: Stage 3
       added 25 lines to webhook.py, so _retry_delay moved 1727 -> 1752 and two
       LEGITIMATE classifications pointed at lines that no longer existed. The
       dangerous direction is the other one -- a NEW swallow landing on line 1727
       would have INHERITED "LEGITIMATE". Hence the key is now
       (file, func, kind, caught, ordinal).
    2. Cross-references were prose ("As line 79.", "As line 458."), stale by
       construction for the same reason. They are now SAME_AS and must resolve.
    3. `caught` is part of the identity, so widening `except ValueError` to
       `except Exception` forfeits the classification instead of inheriting it.
    """
    import glob
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(_root, "tools"))
    import audit_fail_silent as A
    from fail_silent_triage import TRIAGE, FIXED, REAL_BUG, resolve_reason, SAME_AS

    root = os.path.dirname(os.path.dirname(os.path.abspath(A.__file__)))
    sites = {}
    for path in sorted(glob.glob(os.path.join(root, "app", "*.py"))):
        name = os.path.basename(path)
        for f in A.audit_file(path):
            sites[A.site_key(name, f)] = f

    # 1. The real repository passes.
    assert A.check(sites) == 0, "the gate must be green on the real tree"

    # 2. Every classification resolves to prose, and no REAL_BUG is left standing.
    for key, (bucket, _) in TRIAGE.items():
        assert bucket != REAL_BUG, f"{key} annotated instead of fixed"
        assert len(resolve_reason(key).strip()) >= 40, key

    # 3. An unclassified swallow fails.
    extra = ("brand_new.py", "_sneaky", "swallowed_except", "Exception", 0)
    assert A.check({**sites, extra: {"line": 1}}) == 1, \
        "a swallow nobody classified must fail the build"

    # 4. A fix coming back fails, even at a different line and exception clause.
    revert_file, revert_func = next(iter(FIXED))
    reverted = (revert_file, revert_func, "swallowed_except", "OSError", 0)
    assert A.check({**sites, reverted: {"line": 1}}) == 1, \
        f"a silent path returning to {revert_file}:{revert_func} must fail"

    # 5. A classification whose site vanished is stale, not silently dropped.
    fewer = dict(sites)
    fewer.pop(next(iter(sites)))
    assert A.check(fewer) == 1, "a stale classification must fail the build"

    # 6. A dangling cross-reference fails rather than reading as explained.
    key = next(k for k, (_, r) in TRIAGE.items() if isinstance(r, SAME_AS))
    bucket, original = TRIAGE[key]
    TRIAGE[key] = (bucket, SAME_AS("nowhere.py", "no_such_function"))
    try:
        assert A.check(sites) == 1, "a SAME_AS pointing at nothing must fail"
    finally:
        TRIAGE[key] = (bucket, original)
    assert A.check(sites) == 0, "restored"


def test_canonical_op_is_idempotent():
    """canonical_op() returned "" for an ALREADY-canonical operation.

    CHANGE_TYPE_MAP is keyed by raw engine dialects, so "remove_field" was not a
    key, fell through every suffix heuristic ("remove_field" does not contain
    "removed"), and returned the empty string. Three readers were affected:

      * outcomes.blocked_reason() rendered "no transformation exists for {op}" with
        a BLANK operation -- an empty explanation, produced by the function written
        in Stage 3 to abolish empty explanations.
      * fix_templates.apply_fix_template() would treat it as an unknown change
        type: unchanged code, no PR.
      * The capability registry is keyed by canonical ops, so asking it about
        "remove_field" asked about "".

    Same falsy-read shape as the phantom getattr fields and the wrong ANTHROPIC
    env var: a lookup that misses returns something usable-looking.
    """
    from app.change_types import canonical_op, CANONICAL_OPS

    for op in CANONICAL_OPS:
        assert canonical_op(op) == op, f"{op} is canonical but mapped to {canonical_op(op)!r}"
        assert canonical_op(canonical_op(op)) == op, f"{op} not idempotent"

    # Raw dialects still normalise, which is the original purpose.
    assert canonical_op("removed_field") == "remove_field"
    assert canonical_op("added_required_field") == "add_required"
    assert canonical_op("") == ""

    # The blank explanation this produced is gone.
    from app.outcomes import blocked_reason
    for change_type in ("remove_field", "removed_field"):
        reason = blocked_reason(change_type, "Unsupported change type")
        assert "for  in" not in reason, f"blank operation in: {reason}"
        assert "remove_field" in reason, reason


def test_registry_governs_routing_and_auto_is_never_unearned():
    """The registry could compute production_ready() all along and NOTHING ASKED.

    Only tools/ and tests/ imported it. Production decided with
    should_create_pr(confidence) alone, and format_pr_body() titled every result
    "## Ripple - Automated Fix" -- including cells the registry knew had four unmet
    blockers. Same defect shape as the package vector: built, tested, CI-gated, and
    unreachable from the path that mattered.
    """
    from app.routing import pr_level, Level
    from app import capability_claims as cc
    from app.capabilities import CONTRACT_ENGINES
    from app import languages
    from app.change_types import CANONICAL_OPS

    # 1. AUTO is impossible for any cell the registry has not cleared. Swept over
    #    the whole matrix rather than a sample, because the point is that no
    #    combination can slip through.
    checked = 0
    for lang in sorted(languages.languages()):
        for contract in sorted(CONTRACT_ENGINES):
            for op in sorted(CANONICAL_OPS):
                d = pr_level(lang, contract, op, confidence=0.99,
                             min_confidence=0.5)
                checked += 1
                if d.level is Level.AUTO:
                    assert cc.production_ready(lang, contract, op), \
                        f"AUTO for a cell the registry has not cleared: " \
                        f"{lang}/{contract}/{op}"
                else:
                    assert d.reasons, f"{d.level} with no reason: {lang}/{contract}/{op}"
    assert checked > 500, checked

    # 2. A raw engine dialect and its canonical form must route identically --
    #    otherwise the registry is answering a different question than the one the
    #    webhook asked.
    for raw, canon in (("removed_field", "remove_field"),
                       ("added_required_field", "add_required")):
        a = pr_level("typescript", "openapi", raw, 0.9, 0.5)
        b = pr_level("typescript", "openapi", canon, 0.9, 0.5)
        assert a == b, (raw, a, b)

    # 3. Below the threshold: no PR, with the reason stated.
    low = pr_level("typescript", "openapi", "removed_field", 0.10, 0.5)
    assert low.level is Level.BLOCKED and not low.opens_pr
    assert "below the configured minimum" in low.reasons[0]

    # 4. An unmappable change type is REVIEW with the gap named, never AUTO.
    unknown = pr_level("typescript", "openapi", "%%nonsense%%", 0.99, 0.5)
    assert unknown.level is Level.REVIEW and unknown.opens_pr
    assert "canonical operation" in unknown.reasons[0]

    # 5. The PR body -- the artifact a customer reads -- must not claim more than
    #    the level. And a MISSING decision must not upgrade the claim: absence of
    #    evidence is not clearance.
    from app.confidence import format_pr_body
    review = pr_level("swift", "proto", "removed_field", 0.92, 0.5)
    body = format_pr_body("Field removed", "acme/spec", 0.92, ["grep"], ["ref"],
                          decision=review)
    assert "Automated Fix" not in body.split("\n")[0], body.split("\n")[0]
    assert "human review required" in body.split("\n")[0]
    for reason in review.reasons:
        assert reason in body, reason
    bare = format_pr_body("Field removed", "acme/spec", 0.92, ["grep"], ["ref"])
    assert "Automated Fix" not in bare.split("\n")[0]

    # 6. Routing keeps NO language list of its own -- it asks. Checked structurally
    #    via the same AST helper the CI gate uses, so the test and the gate cannot
    #    disagree about what counts as a list.
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
    from audit_capabilities import _language_lists_declared_in
    router = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "app", "routing.py")
    assert _language_lists_declared_in(router, open(router).read()) == []

    # 7. The three modules that held eligibility lists are gone and stay gone.
    app_dir = os.path.dirname(router)
    for dead in ("ai_confidence.py", "impact_prediction.py",
                 "fix_generator_multi.py"):
        assert not os.path.exists(os.path.join(app_dir, dead)), dead


def test_governance_scope_is_exactly_what_was_verified():
    """The registry now governs THREE of five PR-creating entry points.

    Stages 3 and 6 were both verified by importing the new module and by tests, and
    neither check asked the question that mattered: how many ways are there into a
    PR? Five. `pr_level` was reachable from `github_webhook` and nothing else:

      gitlab_webhook      154 lines of pipeline inlined in the route handler
      bitbucket_webhook   154 lines, same shape
      app/cli.py:main     pr_engine, with its OWN _format_pr_body
      agent/core.py:main  separate package, imports nothing from app.routing

    Both webhooks are now governed: the decision moved into
    webhook._govern_consumer_fix (ONE pr_level call site in the file, shared by all
    three platforms) and each opens a ChangeRun and emits the outcome funnel. They
    remain OFF by default behind RIPPLE_ENABLE_EXPERIMENTAL_PLATFORMS -- governed is
    not enabled -- but being off is now a scope decision rather than a safety one.

    This test exists so the sentence cannot quietly become false in EITHER
    direction: a new ungoverned entry point fails, and a platform that regresses out
    of the governed set fails too. Two remain exempt, and shrinking that list is the
    unit of progress.

    It also records why three earlier audits missed the original gap. The
    duplication was INLINE IN ROUTE HANDLERS and in a second package, so filename
    pairs and module-level call graphs -- the two things used to size P0.1 as "~1
    day" -- could not see it. That estimate was wrong in the unusual direction:
    under-scoped.
    """
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
    import audit_pipeline_governance as G

    graph = G._call_graph()
    entries = G._entry_points(graph)

    governed = sorted(e for e in entries
                      if not [k for k in G.REQUIRED
                              if k not in G._reachable(graph, e)])
    assert governed == [
        "app/webhook.py:bitbucket_webhook",
        "app/webhook.py:github_webhook",
        "app/webhook.py:gitlab_webhook",
    ], governed

    # Every ungoverned entry point is either SWITCHED OFF or a named exemption,
    # and every named exemption is still real. DISABLED is now empty, which is the
    # progress: those two moved from "actively closed off" to "actually governed".
    ungoverned = sorted(set(entries) - set(governed))
    accounted = sorted(set(G.EXEMPT) | set(G.DISABLED))
    assert ungoverned == accounted, (ungoverned, accounted)
    assert not (set(G.EXEMPT) & set(G.DISABLED)), "an entry cannot be both"
    for fn, reason in {**G.EXEMPT, **G.DISABLED}.items():
        assert len(reason) > 40, fn

    # A disabled route must guard BEFORE it can open a PR, not merely contain a
    # guard somewhere.
    for entry in G.DISABLED:
        guard, pr_call = G._guard_position(entry)
        assert guard is not None, f"{entry} has no guard"
        assert pr_call is None or guard < pr_call, (entry, guard, pr_call)

    # The gate is green on the real tree, and is not vacuous.
    assert G.main([]) == 0
    G.EXEMPT.pop("app/cli.py:main")
    try:
        assert G.main([]) == 1, "an unlisted ungoverned entry point must fail"
    finally:
        G.EXEMPT["app/cli.py:main"] = (
            "app/cli.py calls pr_engine.create_prs, which has its OWN "
            "_format_pr_body and no routing decision. The CLI states no safety "
            "level. Left live because it is developer-invoked and opens nothing "
            "without an explicit command.")
    assert G.main([]) == 0


def test_running_revision_is_reported_or_explicitly_unknown():
    """After pushing 8 commits, "is the fix deployed?" was UNANSWERABLE.

    `/` returned a hardcoded "version": "0.1.0" that could not change, and
    /health/storage was byte-identical to before the push. Not "no" -- there was no
    way to tell, from inside or outside. That is the absent-vs-unreachable
    ambiguity that has now cost this project four times: get_file returning "" for
    both 404 and 503, PropBench caching a 403 as "unreachable", /propbench/results
    unable to distinguish zero submissions from wiped state, and this.

    The rule that matters: an undeterminable revision reports sha=None with
    source="unavailable", NOT a plausible-looking fallback. A wrong SHA is worse
    than no SHA, because it answers the question falsely.
    """
    import importlib
    import json
    from app import build_info as bi

    # 1. Platform-injected SHA is used and its ORIGIN is reported, because a SHA
    #    from the local working tree is not evidence about anything deployed.
    os.environ["RAILWAY_GIT_COMMIT_SHA"] = "a" * 40
    os.environ["RAILWAY_GIT_BRANCH"] = "main"
    try:
        importlib.reload(bi)
        info = bi.build_info()
        assert info["sha"] == "a" * 40, info
        assert info["short"] == "a" * 8
        assert info["source"] == "env:RAILWAY_GIT_COMMIT_SHA", info
        assert info["branch"] == "main"
        assert bi.is_determinable()
    finally:
        del os.environ["RAILWAY_GIT_COMMIT_SHA"]
        del os.environ["RAILWAY_GIT_BRANCH"]

    # 2. Local checkout: reported, but tagged as the working tree, and flagged
    #    dirty when it is -- a dirty tree corresponds to no commit at all.
    #
    #    The env keys MUST be cleared first. The first version of this assertion
    #    did not, and it passed locally and failed in CI: GitHub Actions always
    #    sets GITHUB_SHA, which is in _ENV_KEYS, so _from_env() correctly won and
    #    reported "env:GITHUB_SHA". The code was right; the test had assumed an
    #    environment rather than establishing one. A test that only holds on one
    #    machine is the same defect as a gate that cannot run.
    saved = {k: os.environ.pop(k, None) for k in bi._ENV_KEYS}
    try:
        importlib.reload(bi)
        info = bi.build_info()
        assert info["source"].startswith("git:working-tree"), info
        assert info["sha"], info
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
        importlib.reload(bi)

    # 3. THE CASE THAT MATTERS: nothing to go on. Must refuse, not guess.
    real_run = bi.subprocess.run

    def no_git(*a, **k):
        raise OSError("no git here")

    bi.subprocess.run = no_git
    saved3 = {k: os.environ.pop(k, None) for k in bi._ENV_KEYS}
    try:
        importlib.reload(bi)
        bi.subprocess.run = no_git          # reload restored the real one
        resolved = bi._resolve()
        assert resolved["sha"] is None, resolved
        assert resolved["source"] == "unavailable", resolved
        assert "refusal to guess" in resolved["detail"], resolved
        # No plausible-looking fallback anywhere in the payload.
        assert "0.1.0" not in json.dumps(resolved)
    finally:
        # Restore BOTH the patched call and the environment. The first version
        # popped the env keys and never put them back, so under CI (where
        # GITHUB_SHA is set) this test silently changed the environment for every
        # test that ran after it.
        bi.subprocess.run = real_run
        for k, v in saved3.items():
            if v is not None:
                os.environ[k] = v
        importlib.reload(bi)

    # 4. Both surfaces render the SAME function's output -- a second assembly is
    #    how two endpoints end up disagreeing about one fact.
    import asyncio
    from app import webhook
    root_body = asyncio.run(webhook.root())
    health_body = asyncio.run(webhook.health())
    assert root_body["build"] == health_body["build"], (root_body, health_body)
    assert set(root_body["build"]) == set(webhook.build_info())


def test_experimental_platforms_are_off_across_the_whole_surface():
    """All ELEVEN gitlab/bitbucket routes are switched off, not just the webhooks.

    Disabling only /webhook/gitlab and /webhook/bitbucket would have introduced a
    NEW silent failure: a user could still complete /auth/gitlab, see
    /auth/gitlab/status report a connection, register via /setup/gitlab/register --
    and then nothing would ever happen, with nothing saying why. A half-disabled
    platform is worse than a live one, because the product appears to work.

    The governance audit covers the two webhooks (they can open PRs). The other
    nine are pinned here, because nothing else would notice their guard being
    removed.
    """
    import ast
    import importlib

    GUARDED = {
        "app/webhook.py": ["gitlab_webhook", "bitbucket_webhook"],
        "app/gitlab_oauth.py": ["gitlab_auth_start", "gitlab_auth_callback",
                                "gitlab_auth_status"],
        "app/bitbucket_oauth.py": ["bitbucket_auth_start", "bitbucket_auth_callback",
                                   "bitbucket_auth_status"],
        "app/gitlab_setup.py": ["gitlab_setup_page", "register_gitlab_token",
                                "list_registered_projects"],
    }
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    checked = 0
    for rel, funcs in GUARDED.items():
        tree = ast.parse(open(os.path.join(root, rel)).read())
        found = {n.name: n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for name in funcs:
            assert name in found, f"{rel}: {name} is gone -- was the route renamed?"
            calls = [c for c in ast.walk(found[name]) if isinstance(c, ast.Call)]
            guards = [c for c in calls
                      if (c.func.id if isinstance(c.func, ast.Name)
                          else getattr(c.func, "attr", "")) == "experimental_disabled"]
            assert guards, f"{rel}:{name} has no experimental_disabled() guard"
            checked += 1
    assert checked == 11, checked

    # Default is OFF, and turning it on must be explicit -- a flag defaulting to ON
    # that someone must remember to clear is how a temporary decision becomes
    # permanent.
    from app import experimental
    saved = os.environ.pop("RIPPLE_ENABLE_EXPERIMENTAL_PLATFORMS", None)
    try:
        importlib.reload(experimental)
        assert experimental.experimental_enabled() is False
        os.environ["RIPPLE_ENABLE_EXPERIMENTAL_PLATFORMS"] = "1"
        assert experimental.experimental_enabled() is True
        os.environ["RIPPLE_ENABLE_EXPERIMENTAL_PLATFORMS"] = "true"   # only "1" counts
        assert experimental.experimental_enabled() is False
    finally:
        os.environ.pop("RIPPLE_ENABLE_EXPERIMENTAL_PLATFORMS", None)
        if saved is not None:
            os.environ["RIPPLE_ENABLE_EXPERIMENTAL_PLATFORMS"] = saved
        importlib.reload(experimental)

    # The refusal STATES a reason and how to reverse it -- 501 with an empty body
    # would be the same silence in a different costume.
    resp = experimental.experimental_disabled("gitlab", "webhook")
    assert resp.status_code == 501, resp.status_code
    body = json.loads(resp.body)
    assert body["error"] == "platform_disabled"
    assert body["platform"] == "gitlab"
    assert "RIPPLE_ENABLE_EXPERIMENTAL_PLATFORMS" in body["to_re_enable"]
    assert "silence" in body["reason"]
    assert "/dry-run" in body["what_still_works"]


def test_every_breaking_change_ends_in_exactly_one_terminal_state():
    """One breaking change in, exactly ONE terminal state out.

    app/outcomes.py records EVENT-level outcomes and several fire per change -- one
    per consumer file. Useful for tracing, useless for counting: you cannot compute
    "what happened to this change?" from a stream where the same change produced
    FIX_GENERATED four times and BLOCKED twice. Without a single answer per change,
    the Autonomous Resolution Rate has no denominator.

    Emission is structural, not remembered: ChangeRun is a context manager, so the
    state is emitted on return, break, AND exception. Before this, an exception
    mid-consumer-loop produced a logged process_spec_error and no statement about
    the change itself.

    Six states, not the five specified. NO_CHANGE_REQUIRED is separate because a
    wire_only change (a proto field number changed) requires no source edit at all.
    BLOCKED would report a correct refusal as a failure -- the mistake the registry
    made when it demanded a transformation from wire_only ops. RESOLVED would be
    worse: it would inflate the resolution rate with changes where Ripple did
    nothing, and that number is meant to be the one the company is built on.
    """
    from app import activity
    from app.run_outcome import (ChangeRun, Terminal, COUNTS_AS_RESOLVED,
                                 EXCLUDED_FROM_RATE)

    def terminal_of(body):
        before = len([e for e in activity.recent(500)
                      if e.get("action") == "change_terminal"])
        try:
            with ChangeRun(change_type="remove_field", spec="api/user.yaml",
                           repo="acme/api") as run:
                body(run)
        except RuntimeError:
            pass
        events = [e for e in activity.recent(500)
                  if e.get("action") == "change_terminal"]
        assert len(events) - before == 1, \
            f"expected exactly 1 terminal state, got {len(events) - before}"
        return events[-1]["terminal"]

    def raises(run):
        run.consumer_found("checkout.ts")
        raise RuntimeError("engine exploded")

    def early(run):
        run.consumer_found("checkout.ts")
        return                                    # an early return still emits

    cases = [
        (lambda r: None, Terminal.NO_CONSUMER),
        (lambda r: r.refused("checkout.ts", "no typescript handler for this op"),
         Terminal.BLOCKED),
        (lambda r: r.pr_created("https://pr/1", "checkout.ts"), Terminal.PARTIAL),
        (lambda r: r.pr_created("https://pr/1", "checkout.ts", validated=True),
         Terminal.RESOLVED),
        (lambda r: (r.pr_created("https://pr/1", "a.ts", validated=True),
                    r.refused("b.ts", "no handler for this language")),
         Terminal.PARTIAL),
        (lambda r: r.requires_no_change(), Terminal.NO_CHANGE_REQUIRED),
        (raises, Terminal.FAILED),
        (early, Terminal.BLOCKED),
    ]
    for body, expected in cases:
        assert terminal_of(body) == expected.value, expected

    # A refusal without a reason is the silence this exists to remove.
    try:
        ChangeRun("x", "y", "z").refused("a.ts", "")
        raise AssertionError("an unexplained refusal was accepted")
    except ValueError:
        pass

    # No caller may assert a state -- there is no setter, and terminal() is derived.
    assert not hasattr(ChangeRun("x", "y", "z"), "set_terminal")

    # RESOLVED is unreachable while nothing validates, exactly as AUTO is.
    unvalidated = ChangeRun("x", "y", "z")
    unvalidated.pr_created("https://pr/1", "a.ts")          # validated defaults False
    assert unvalidated.terminal() is Terminal.PARTIAL

    # ARR accounting: wire_only is excluded from the rate rather than counted as a win.
    assert Terminal.RESOLVED in COUNTS_AS_RESOLVED
    assert Terminal.NO_CHANGE_REQUIRED in EXCLUDED_FROM_RATE
    assert Terminal.NO_CHANGE_REQUIRED not in COUNTS_AS_RESOLVED

    # And the loop is still wrapped -- checked with the same helper the CI gate uses,
    # so the test and the gate cannot disagree about what "wrapped" means.
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
    from audit_pipeline_governance import _terminal_state_wrapping
    webhook_py = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "app", "webhook.py")
    assert _terminal_state_wrapping(webhook_py) == []


def test_golden_fixture_is_broken_satisfiable_and_claims_nothing_yet():
    """The golden fixture must FAIL to compile, be fixable, and claim nothing.

    A fixture that compiles in its broken state proves nothing. This one does not:
    `src/types.ts` is already regenerated from the after-spec, so every remaining
    reference to `phoneNumber` is a type error -- verified with a real `tsc`, 2
    errors, exit 2, and exit 0 after a correct two-edit fix.

    Three things this test enforces, none of which need node to check:

    1. The declared contract exists and is internally consistent.
    2. The MEASURED baseline is recorded rather than glossed. Ripple's TypeScript
       remove_field handler is currently a no-op on this fixture while reporting
       "Removed all references to field 'phoneNumber' (0 lines affected)" -- a
       false claim in a user-facing string. That is written down in expected.json
       so Stage 6 cannot quietly assume the transformation works.
    3. The claim now EXISTS and is earned: test_e2e_typescript_openapi_remove_field
       runs the whole path against a real compiler, so the registry cites a test
       rather than a boolean. A fixture existing was never evidence; a fixture a
       named test satisfies is.
    """
    import json as _json

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    base = os.path.join(root, "fixtures", "typescript-openapi", "remove-field")
    spec = _json.load(open(os.path.join(base, "expected.json")))

    # 1. the contract is coherent
    assert spec["cell"] == {"language": "typescript", "contract": "openapi",
                            "operation": "remove_field"}
    assert spec["change"]["field"] == "phoneNumber"
    assert spec["expect"]["typecheck_before_fix"] == "FAIL"
    assert spec["expect"]["typecheck_after_fix"] == "PASS"
    assert spec["expect"]["consumers_found"] == ["src/checkout.ts"]
    assert spec["expect"]["pr_files"] == ["src/checkout.ts"]
    for t in spec["expect"]["transformation"]:
        assert t["mechanical"] is True and t["why"], t

    # every file the contract mentions actually exists
    for rel in (spec["change"]["spec_before"], spec["change"]["spec_after"]):
        assert os.path.exists(os.path.join(base, rel)), rel
    for rel in spec["expect"]["consumers_found"] + spec["expect"]["untouched"]:
        assert os.path.exists(os.path.join(base, "consumer", rel)), rel

    # the removed field is gone from the after-spec and the regenerated type, and
    # still present in the consumer -- which is precisely why it does not compile
    after = open(os.path.join(base, spec["change"]["spec_after"])).read()
    types = open(os.path.join(base, "consumer", "src", "types.ts")).read()
    consumer = open(os.path.join(base, "consumer", "src", "checkout.ts")).read()
    untouched = open(os.path.join(base, "consumer", "src", "orders.ts")).read()
    assert "phoneNumber" not in after
    # Check the DECLARATION, not the word: types.ts documents in its header comment
    # why the field is absent, and a bare substring test fails on that comment. The
    # same text-vs-structure mistake as the gate that matched KNOWN_LANGUAGES inside
    # a docstring.
    assert not re.search(r"^\s*phoneNumber\??\s*:", types, re.MULTILINE), types
    assert "phoneNumber" in types, "the header should explain why it is absent"
    assert consumer.count("user.phoneNumber") == 2
    assert "phoneNumber" not in untouched     # so "untouched" is falsifiable

    # 2. the measured baseline is recorded, including the false claim
    m = spec["measured"]
    assert m["fixture_fails_without_a_fix"] is True
    assert m["fixture_is_satisfiable"] is True
    # Stage 6 re-measured: the codemod replaced the regex, so the output now parses
    # and validates. The Stage 4 record is KEPT under stage_4_baseline because it is
    # the reason the codemod exists -- deleting it would erase why.
    assert m["ripple_output_parses"] is True
    assert m["ripple_typecheck_after"] == "VALID"
    assert m["production_ready"] is True
    assert m["evidence_test"] == "test_e2e_typescript_openapi_remove_field"
    baseline = m["stage_4_baseline"]
    assert baseline["ripple_output_parses"] is False
    assert "user.};" in baseline["ripple_diff"]
    assert baseline["verdict"].startswith("BROKEN FIX")

    # Measured here, not trusted from the file. Stage 6 replaced the regex with
    # app/ts_codemod.py, so the handler now produces the CORRECT fix -- identical to
    # the hand-written reference -- and refuses shapes it cannot remove safely.
    from app.fix_templates import apply_fix_template
    fixed, explanation = apply_fix_template(
        code=consumer, language="typescript", change_type="removed_field",
        field_name="phoneNumber")
    assert fixed != consumer
    assert "user.}" not in fixed, "the corrupting substitution is back"
    assert "${user.phoneNumber}" not in fixed, "the template reference must be gone"
    assert "phone: user.phoneNumber" not in fixed, "the payload key must be gone"
    assert "user.email" in fixed and "user.fullName" in fixed, \
        "unrelated references must survive"
    assert "Removed references" in explanation

    # And a shape it cannot handle is REFUSED, with the code left alone -- the
    # explanation must not claim success. "Removed all references ... (0 lines
    # affected)" was a false claim that read identically to a corruption.
    judgment = ("const phone = user.phoneNumber;\n"
                "export const t = phone ? phone : user.email;\n")
    unchanged, why = apply_fix_template(
        code=judgment, language="typescript", change_type="removed_field",
        field_name="phoneNumber")
    assert unchanged == judgment
    assert "Could NOT remove" in why, why

    # 3. the claim is earned, and every registered claim names a real test
    from app.capability_claims import E2E_FIXTURES, e2e_tested
    assert e2e_tested("typescript", "openapi", "remove_field"), \
        "Stage 6 earned this claim; losing it means the evidence was dropped"
    # Deliberately NOT `len(E2E_FIXTURES) == 1`. Pinning the count made adding the
    # second fixture a test failure, which punishes the exact progress the registry
    # exists to enable. What must hold is that no entry is fiction: every claimed
    # cell names a test that exists in this module.
    import sys as _sys
    this_module = _sys.modules[__name__]
    for cell, test_name in sorted(E2E_FIXTURES.items()):
        assert hasattr(this_module, test_name), \
            f"{'/'.join(cell)} claims evidence from {test_name}, which does not exist"
        assert e2e_tested(*cell), f"{'/'.join(cell)} is registered but not e2e_tested"


def test_validation_never_turns_unknown_into_valid():
    """Three states, and UNABLE_TO_VALIDATE is not a pass.

    app/validated_fix.py -- deleted in Stage 5 -- got this wrong in the most
    expensive way available: it ended `else: return True, ''`, and its TypeScript
    check was brace-matching, so it returned VALID for `phoneNumber: int32` AND for
    `!!! not rust`. A validator that cannot fail converts "unproven" into "proven".

    Hermetic on purpose: no docker, no network, no node. The real three-case proof
    lives in tools/verify_validation.py, which is an acceptance check rather than a
    gate because it needs a container runtime -- and a gate that cannot run is the
    same defect as a matcher that cannot be reached.
    """
    import tempfile
    from app.capability_claims import ValidationState
    from app.validation import (Verdict, validate, validate_typescript,
                                validate_python, choose_backend, RUNNERS)

    # 1. A language with no runner makes no claim. `python` used to be in this list
    #    and was moved out when its runner was wired -- the cases in 4 below replace
    #    the coverage it provided, so wiring a language STRENGTHENS this test rather
    #    than shrinking it.
    for lang in ("rust", "cobol", "cobol2"):
        v = validate(lang, "/nonexistent")
        assert v.state is ValidationState.UNABLE_TO_VALIDATE, lang
        assert not v.is_valid
        assert "no validation runner" in v.reason
    assert set(RUNNERS) == {"typescript", "python", "go"}, sorted(RUNNERS)

    # 2. An unrecognised backend REFUSES rather than silently taking the weakest
    #    path. The first version branched `if docker ... else host`, so any unknown
    #    string ran with the least isolation -- the same shape as canonical_op()
    #    returning "" for input it did not recognise.
    v = validate_typescript(".", backend="bogus-backend")
    assert v.state is ValidationState.UNABLE_TO_VALIDATE, v.state
    assert "unknown validation backend" in v.reason

    # 3. A workspace without the project files cannot be typechecked meaningfully.
    empty = tempfile.mkdtemp()
    try:
        v = validate_typescript(empty, backend="host")
        assert v.state is ValidationState.UNABLE_TO_VALIDATE
        assert "package.json" in v.reason

        # 4. The python runner's own refusals, both reached before any container is
        #    started so this stays hermetic.
        #
        # 4a. python is docker-only. choose_backend()'s host path probes for a NODE
        #     binary, which is no evidence that python or mypy exist -- accepting it
        #     would run an unknown toolchain and call the result validation.
        v = validate_python(empty, backend="host")
        assert v.state is ValidationState.UNABLE_TO_VALIDATE, v.state
        assert "requires docker" in v.reason, v.reason

        # 4b. No pinned toolchain, no reproducible verdict.
        v = validate_python(empty, backend="docker")
        assert v.state is ValidationState.UNABLE_TO_VALIDATE, v.state
        assert "requirements-dev.txt" in v.reason, v.reason
    finally:
        import shutil as _sh
        _sh.rmtree(empty, ignore_errors=True)

    # 4. is_valid is True for exactly one state -- no truthiness accidents.
    for state in ValidationState:
        verdict = Verdict(state, "x")
        assert verdict.is_valid is (state is ValidationState.VALID), state

    # 5. The evidence names the backend, so a reader can tell how much isolation
    #    actually applied. "VALID" without provenance is the claim-without-evidence
    #    problem the capability registry exists to prevent.
    detail = Verdict(ValidationState.VALID, "ok",
                     evidence={"backend": "docker"}).as_detail()
    assert detail["validation"] == "VALID"
    assert detail["evidence_backend"] == "docker"

    # 6. `validate` is now a DERIVED fact: the dotted path must resolve to a
    #    callable. A declared path that does not import is a lie -- exactly what
    #    app/impact_prediction.py was before it was deleted.
    from app.capability_claims import VALIDATORS, validates, validation_state
    ts = VALIDATORS["typescript"]
    assert ts.implemented_by == "app.validation:validate_typescript"
    assert ts.is_wired and validates("typescript")
    assert validation_state("typescript") is ValidationState.VALID

    for wired in ("typescript", "python", "go"):
        assert VALIDATORS[wired].is_wired, wired
        assert validates(wired)
        assert validation_state(wired) is ValidationState.VALID
    assert (VALIDATORS["python"].implemented_by
            == "app.validation:validate_python")
    assert VALIDATORS["go"].implemented_by == "app.validation:validate_go"

    # All three declared validators are now wired, so there is no real unwired entry
    # left to assert against. The distinction is still tested -- by the synthetic
    # specs below, whose implemented_by paths deliberately do not resolve. That is
    # stronger than relying on one language staying unimplemented, which was only
    # ever true by accident of scheduling.

    # A path that does not resolve must NOT count as wired.
    from app.capability_claims import ValidatorSpec
    assert not ValidatorSpec("x", "t", implemented_by="app.nope:missing").is_wired
    assert not ValidatorSpec("x", "t", implemented_by="app.validation:not_a_func").is_wired
    assert not ValidatorSpec("x", "t", implemented_by="no_colon_here").is_wired

    # 7. The superseded stub is gone, not merely frozen.
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert not os.path.exists(os.path.join(root, "app", "validated_fix.py"))
    # 8. Adding a RUNNERS key is a capability claim, so it must be EARNED rather
    #    than merely typed. This was `len(RUNNERS) == 1` while TypeScript was the
    #    only runner; a count pin makes wiring a second language a test failure,
    #    which punishes the work instead of policing it. The invariant that actually
    #    matters is the two-way agreement: every runner has a wired ValidatorSpec
    #    pointing back at it, and every wired spec has a runner. That makes a key
    #    added without an implementation fail, and a spec claiming an implementation
    #    that is not dispatched fail too.
    from app.capability_claims import VALIDATORS as _V
    for lang, fn in RUNNERS.items():
        spec = _V.get(lang)
        assert spec is not None, \
            f"{lang} has a runner but no ValidatorSpec -- an undeclared capability"
        assert spec.is_wired, f"{lang} has a runner but its spec is not wired"
        assert spec.implemented_by.endswith(fn.__name__), (
            f"{lang}: spec points at {spec.implemented_by!r} but RUNNERS dispatches "
            f"to {fn.__name__} -- the declaration and the dispatch disagree")
    for lang, spec in _V.items():
        assert spec.is_wired == (lang in RUNNERS), (
            f"{lang}: is_wired={spec.is_wired} but RUNNERS membership="
            f"{lang in RUNNERS} -- a wired spec with no runner would be a claim "
            f"nothing can honour")

    # 8. choose_backend never invents one.
    backend, note = choose_backend()
    assert backend in ("", "docker", "host"), backend
    assert note


def _record_e2e_evidence(cell, verdict):
    """Merge one cell's compile proof into tests/.e2e_evidence.json.

    MERGE, not overwrite. Each e2e test owns exactly one key; a plain write would
    mean whichever test ran last was the only one with evidence, and
    audit_capabilities would then report fewer proven cells than actually
    compiled -- evidence lost silently, which is the failure mode this whole
    evidence file exists to prevent.

    Stale keys are NOT pruned here. A key for a cell no longer in E2E_FIXTURES is
    harmless (the audit intersects with E2E_FIXTURES), and pruning from inside a
    test would let a single failing test erase another's proof.
    """
    import json as _json
    import time as _time

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        ".e2e_evidence.json")
    data = {}
    if os.path.exists(path):
        try:
            with open(path) as fh:
                data = _json.load(fh)
        except (OSError, ValueError):
            data = {}
    if not isinstance(data.get("cells"), dict):
        data = {"cells": {}}
    data["cells"]["/".join(cell)] = {
        "ran_at": _time.time(),
        "backend": verdict.evidence["backend"],
        "typecheck_exit": verdict.evidence["typecheck_exit"],
        "validated": True,
    }
    with open(path, "w") as fh:
        _json.dump(data, fh, indent=2, sort_keys=True)


def test_e2e_typescript_openapi_remove_field():
    """THE golden path, end to end, against the real toolchain.

    detect -> canonical op -> fix -> APPLY -> tsc --noEmit -> minimal diff

    This is the test named in E2E_FIXTURES, so it is the evidence that makes
    typescript x openapi x remove_field production-ready. It must therefore run the
    real thing: no stubs, no monkeypatching, a real container, a real compiler.

    It SKIPS rather than passes when no validation backend exists. A test that
    quietly passes without a compiler would be the same defect as validated_fix.py
    returning True from absence of evidence -- and the capability registry would
    then be citing a test that proved nothing. Skipping is visible; a false pass is
    not.
    """
    import filecmp
    import shutil as _sh
    import tempfile
    from app.capability_claims import ValidationState
    from app.change_types import canonical_op, category, MECHANICAL
    from app.fix_templates import apply_fix_template
    from app.validation import validate, choose_backend

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    base = os.path.join(root, "fixtures", "typescript-openapi", "remove-field")
    consumer = os.path.join(base, "consumer")

    evidence_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 ".e2e_evidence.json")
    backend, note = choose_backend()
    if not backend:
        # Record NOTHING. tests/.last_run.json counts a skip as "passed", so without
        # a separate proof file the capability registry would honour this cell's e2e
        # claim on a runner with no docker -- AUTO fired by a test that did nothing.
        # That is the absence-of-evidence-as-proof defect, one layer up.
        print(f"      SKIP: no validation backend ({note}) -- no e2e evidence written")
        return

    # 1. the operation is mechanical, so a transformation is legitimate at all
    assert canonical_op("removed_field") == "remove_field"
    assert category("removed_field") == MECHANICAL

    work_root = tempfile.mkdtemp(prefix="ripple-e2e-")
    work = os.path.join(work_root, "consumer")
    try:
        _sh.copytree(consumer, work,
                     ignore=_sh.ignore_patterns("node_modules", ".git"))
        target = os.path.join(work, "src", "checkout.ts")

        # 2. the consumer genuinely does not compile first -- otherwise the rest of
        #    this test would prove nothing
        before = validate("typescript", work)
        assert before.state is ValidationState.INVALID, before.reason
        assert len(before.errors) == 2, before.errors

        # 3. generate and APPLY the fix (read before write -- the inverted order
        #    silently produced empty files and two false VALIDs in Stage 5)
        with open(target) as fh:
            original = fh.read()
        fixed, explanation = apply_fix_template(
            code=original, language="typescript", change_type="removed_field",
            field_name="phoneNumber")
        assert fixed != original, "the transformation did nothing"
        assert "user.}" not in fixed, "the transformation corrupted the file"
        with open(target, "w") as fh:
            fh.write(fixed)
        assert open(target).read() == fixed

        # 4. the real compiler accepts it
        after = validate("typescript", work)
        assert after.state is ValidationState.VALID, \
            f"{after.reason} :: {after.errors[:3]}"
        assert after.evidence["typecheck_exit"] == 0

        # 5. the diff is MINIMAL -- everything else byte-identical
        for untouched in ("src/orders.ts", "src/types.ts", "tsconfig.json",
                          "package.json"):
            assert filecmp.cmp(os.path.join(consumer, untouched),
                               os.path.join(work, untouched), shallow=False), \
                f"{untouched} was modified -- the PR would not be reviewable"

        # 6. Only now, having actually compiled, write the proof. audit_capabilities
        #    requires this for every E2E_FIXTURES claim, so a skipped run cannot
        #    stand in for a real one.
        _record_e2e_evidence(("typescript", "openapi", "remove_field"), after)
    finally:
        _sh.rmtree(work_root, ignore_errors=True)


def test_e2e_typescript_openapi_change_field_type():
    """The SECOND golden path, end to end, against the real toolchain.

    detect -> canonical op -> fix -> APPLY -> tsc --noEmit -> minimal diff

    Same five assertions as the remove_field cell, but the change is inverted and
    that is the point. A removal leaves the type declaration correct and the
    USAGES stale, so the fix edits usages. A type change leaves the usages correct
    and the DECLARATION stale, so the fix edits the declaration -- src/types.ts
    here, not the file that fails to compile. A fixture that edited the erroring
    file would be testing the wrong direction.

    Two hazards this fixture is shaped around, both real:

    1. `change_field_type` ANNOTATES instead of editing when the contract type has
       no native mapping (fix_templates.py, the `native_old and native_new` guard).
       integer->string is mapped for TypeScript (number->string), so the codemod
       genuinely edits. An unmapped pair would leave a comment, the file would
       still not compile, and step 4 would fail -- correctly, because that cell is
       not production-ready.

    2. `_change_type_typescript` rewrites EVERY `: number` in the file and ignores
       field_name. src/types.ts therefore declares exactly one number-typed field.
       A second one would be silently retyped and step 5 would fail.

    SKIPS rather than passes with no validation backend, for the same reason as
    the first cell: a pass without a compiler is evidence of nothing.
    """
    import filecmp
    import shutil as _sh
    import tempfile
    from app.capability_claims import ValidationState
    from app.change_types import canonical_op, category, MECHANICAL
    from app.fix_templates import apply_fix_template
    from app.validation import validate, choose_backend

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    base = os.path.join(root, "fixtures", "typescript-openapi", "change-field-type")
    consumer = os.path.join(base, "consumer")

    backend, note = choose_backend()
    if not backend:
        print(f"      SKIP: no validation backend ({note}) -- no e2e evidence written")
        return

    # 1. the operation is mechanical, so a transformation is legitimate at all
    assert canonical_op("field_type_changed") == "change_field_type"
    assert category("field_type_changed") == MECHANICAL

    work_root = tempfile.mkdtemp(prefix="ripple-e2e-cft-")
    work = os.path.join(work_root, "consumer")
    try:
        _sh.copytree(consumer, work,
                     ignore=_sh.ignore_patterns("node_modules", ".git"))
        target = os.path.join(work, "src", "types.ts")

        # 2. the consumer genuinely does not compile first. Two errors, both in
        #    contact.ts: .trim() and .startsWith() on a number.
        before = validate("typescript", work)
        assert before.state is ValidationState.INVALID, before.reason
        assert len(before.errors) == 2, before.errors

        # 3. generate and APPLY the fix (read before write)
        with open(target) as fh:
            original = fh.read()
        fixed, explanation = apply_fix_template(
            code=original, language="typescript", change_type="field_type_changed",
            field_name="phoneNumber", old_type="integer", new_type="string")
        assert fixed != original, "the transformation did nothing"
        assert "phoneNumber: string;" in fixed, fixed
        # the sibling string fields must survive verbatim -- proof the field-blind
        # regex was pointed at `: number` and not at `: string`
        assert fixed.count(": string;") == 4, fixed
        assert ": number" not in fixed, fixed
        with open(target, "w") as fh:
            fh.write(fixed)
        assert open(target).read() == fixed

        # 4. the real compiler accepts it
        after = validate("typescript", work)
        assert after.state is ValidationState.VALID, \
            f"{after.reason} :: {after.errors[:3]}"
        assert after.evidence["typecheck_exit"] == 0

        # 5. the diff is MINIMAL -- everything else byte-identical. contact.ts is
        #    listed because it is the file that ERRORED: the fix must repair it
        #    without editing it, which is the whole claim of this cell.
        for untouched in ("src/contact.ts", "src/orders.ts", "tsconfig.json",
                          "package.json"):
            assert filecmp.cmp(os.path.join(consumer, untouched),
                               os.path.join(work, untouched), shallow=False), \
                f"{untouched} was modified -- the PR would not be reviewable"

        # 6. proof written only after a real compile
        _record_e2e_evidence(("typescript", "openapi", "change_field_type"), after)
    finally:
        _sh.rmtree(work_root, ignore_errors=True)


def test_python_type_removed_no_longer_destroys_a_live_import():
    """The exact input the old regex corrupted while reporting success.

    `_TYPE_REF_PATTERNS['python']` opened with

        r'^\\s*from\\s+\\S+\\s+import\\s+.*\\b{name}\\b.*$'

    which deletes the ENTIRE import statement when the removed name appears anywhere
    on it. Measured: `from src.models import User, Address` vanished, so `Address` --
    used two functions below -- became undefined, while every `User` reference
    survived. It reported "Removed references to deleted type 'User' (1 lines
    affected): imports, declarations, type annotations and constructions".

    This was worse than the remove_field no-op: that one merely failed to fix, this
    one actively broke a file that had only one thing wrong with it.
    """
    from app.fix_templates import apply_fix_template

    src = ('from src.models import User, Address\n'
           '\n'
           '\n'
           'def f(u: User) -> str:\n'
           '    return u.email\n'
           '\n'
           '\n'
           'def g(a: Address) -> str:\n'
           '    return a.city\n')

    fixed, explanation = apply_fix_template(
        code=src, language="python", change_type="type_removed",
        field_name="User")

    # A live reference cannot be repaired mechanically, so the whole patch is refused
    # and the file comes back untouched. That is the correct answer.
    assert fixed == src, fixed
    assert "Address" in fixed, "the still-used neighbour was dropped from the import"
    assert "Could NOT remove" in explanation, explanation
    assert "NEEDS A HUMAN" in explanation, explanation
    # and the fabricated category list is gone
    assert "type annotations and constructions" not in explanation, explanation


def test_python_type_removed_completes_only_for_a_stale_import():
    """The one shape that IS a complete fix, and its two variants.

    Removing a name from an import list is the only mechanical edit available when a
    type is deleted: there is no substitute value for an object, so a signature, a
    construction or a base class all need a human. A stale import is the case where
    nothing else has to change -- and it is real, not contrived.
    """
    from app.py_codemod import remove_type

    # a) name removed from the list, neighbours preserved verbatim
    src = ('from src.models import User, Address\n'
           '\n'
           '\n'
           'def g(a: Address) -> str:\n'
           '    return a.city\n')
    r = remove_type(src, "User")
    assert r.complete, (r.refusals, r.edits)
    assert r.code.splitlines()[0] == "from src.models import Address"
    assert [e["shape"] for e in r.edits] == ["name in import list"]

    # b) sole name -- the whole statement goes, not just the name
    r2 = remove_type("from src.models import User\n\n\nX = 1\n", "User")
    assert r2.complete, (r2.refusals, r2.edits)
    assert "import" not in r2.code, r2.code
    assert "X = 1" in r2.code

    # c) every other shape refuses, with a reason specific enough to act on
    for src_, expect in (
        ("def f(u: User) -> str:\n    return u.email\n", "function signature"),
        ("class Admin(User):\n    pass\n", "base class"),
        ("x = User(id='1')\n", "construction"),
    ):
        rr = remove_type(src_, "User")
        assert rr.refusals, src_
        assert not rr.complete, src_
        assert any(expect in r for r in rr.refusals), (expect, rr.refusals)


def test_symbol_fallback_refuses_instead_of_destroying_the_file():
    """The six languages with no codemod and no pattern table.

    `_remove_type_reference` fell through to `_generic_remove`, which deletes every
    line matching any CASE VARIANT of the name. `name_variants("User")` yields snake
    `user` and camel `user` -- the conventional VARIABLE name -- so it deleted the
    code and kept the reference. Measured through apply_fix_template:

        dart    import 'models.dart'; String label(User u){return u.email;}
                ->  import 'models.dart';\\n\\n}     the STALE IMPORT survived
        scala   3 lines -> one blank line          entire file destroyed
        shell   echo "looking up user $user_id"    deleted; shell has no types
        yaml    the $ref AND its parent `user:` key gone

    None of these languages has a wired validator, so nothing downstream would have
    caught any of it.
    """
    from app.fix_templates import apply_fix_template

    LIVE = {
        "dart": "import 'models.dart';\n\nString label(User user) {\n"
                "  return user.email;\n}\n",
        "php": "<?php\nuse App\\Models\\User;\n\nfunction label(User $user) {\n"
               "    return $user->email;\n}\n",
        "scala": "import models.User\n\ndef label(user: User): String = user.email\n",
        "swift": "import Models\n\nfunc label(user: User) -> String {\n"
                 "    return user.email\n}\n",
        "shell": '#!/bin/sh\nuser_id="$1"\necho "looking up user $user_id"\n',
        "yaml": "properties:\n  user:\n    $ref: '#/components/schemas/User'\n",
    }
    for lang, src in LIVE.items():
        fixed, explanation = apply_fix_template(src, lang, "type_removed", "User")
        assert fixed == src, (
            f"{lang}: a live reference remains, so the only safe answer is to change "
            f"nothing. Got:\n{fixed}")
        assert "Could NOT remove" in explanation, (lang, explanation)
        assert "lines affected" not in explanation, (
            f"{lang}: claimed an edit count for a file it did not touch: "
            f"{explanation}")

    # `to_upper_snake("User")` is `USER`, which collides with a bare upper-case list
    # entry. Removing a TYPE must not touch a list of required environment variables:
    # after the symbol is removed the line is structural-only, nothing else in the
    # file references it, so the removal would look COMPLETE and ship.
    #
    # NOTE ON THIS ASSERTION: it originally used `echo "$USER"`, which does NOT
    # manifest -- the word `echo` survives, so the structural-only rule refuses that
    # line anyway. Mutation-testing caught the weak assertion: enabling upper-snake
    # for types left the test green. This is the case that actually fires.
    env_list = "required:\n  - USER\n  - HOME\n"
    fixed, _ = apply_fix_template(env_list, "yaml", "type_removed", "User")
    assert fixed == env_list, (
        f"a list of required environment variables is not a reference to a type "
        f"called User:\n{fixed}")

    # And upper-snake must still work where it is meant to: an ENUM VALUE.
    enum_yaml = "status:\n  enum:\n    - ACTIVE\n    - PENDING\n"
    fixed, _ = apply_fix_template(enum_yaml, "yaml", "removed_enum_value", "pending")
    assert "- PENDING" not in fixed, fixed
    assert "- ACTIVE" in fixed, fixed


def test_symbol_fallback_completes_only_for_a_stale_import():
    """The one case that IS mechanical, and it must keep its neighbours.

    Same conclusion py_codemod and ts_codemod reached: for a deleted symbol the only
    safe edit is dropping an import that nothing uses. A PARTIAL removal is not a
    smaller success -- the first draft of this function dropped scala's
    `import models.User` while leaving `User` in a live signature, producing a file
    where the type was both undefined AND unimported, which is strictly worse than
    not touching it. Hence the all-or-nothing check.
    """
    from app.fix_templates import apply_fix_template

    src = ("import models.User\n"
           "import models.Order\n"
           "\n"
           "def n(o: Order): Int = 1\n")
    fixed, explanation = apply_fix_template(src, "scala", "type_removed", "User")

    assert "import models.User" not in fixed, fixed
    assert "import models.Order" in fixed, (
        f"the neighbouring import was taken with it:\n{fixed}")
    assert "def n(o: Order): Int = 1" in fixed, fixed
    assert "Could NOT" not in explanation, explanation

    # A comma-separated import list is refused in BOTH directions: a neighbour before
    # the symbol would be lost, and text after it means the symbol is not the import.
    for listed in ("import models.{Address, User}\n\ndef n(a: Address): Int = 1\n",
                   "import models.{User, Address}\n\ndef n(a: Address): Int = 1\n"):
        out, _ = apply_fix_template(listed, "scala", "type_removed", "User")
        assert out == listed, (
            f"a braced import list is not a shape this fallback can edit:\n{out}")


def test_enum_value_explanation_stops_fabricating_a_shape_list():
    """Third occurrence of the fabricated-shape-list defect, after remove_field and
    remove_type.

    The explanation asserted "Removed references to deleted enum value 'X'
    (N lines affected): switch/case arms, match arms and constant declarations for
    {lang}" on EVERY outcome. Measured with a target that appears nowhere:

        apply_fix_template("const x = 1;\\n", "typescript",
                           "removed_enum_value", "NOPE")
        -> unchanged, and it still named three shapes it had not cleaned.
    """
    from app.fix_templates import apply_fix_template

    untouched = "const x = 1;\n"
    fixed, explanation = apply_fix_template(
        untouched, "typescript", "removed_enum_value", "NOPE")
    assert fixed == untouched
    assert "Could NOT remove" in explanation, explanation
    assert "switch/case arms" not in explanation, (
        f"named shapes it did not clean: {explanation}")
    assert "0 lines affected" not in explanation, explanation

    # And the truthful branch still fires when something really was removed.
    real = ("enum Status {\n  ACTIVE = 1,\n  PENDING = 2,\n}\n")
    fixed, explanation = apply_fix_template(
        real, "dart", "removed_enum_value", "PENDING")
    assert "PENDING" not in fixed, fixed
    assert "ACTIVE = 1," in fixed, fixed
    assert "lines affected" in explanation, explanation

    # A lowercase spec literal must still match -- `literal` exists in name_variants
    # precisely because pascal/upper-snake would miss an OpenAPI enum like `pending`.
    yml = "status:\n  enum:\n    - active\n    - pending\n"
    fixed, _ = apply_fix_template(yml, "yaml", "removed_enum_value", "pending")
    assert "- pending" not in fixed, fixed
    assert "- active" in fixed, fixed


def test_capability_tables_match_the_dispatch():
    """`_OP_TABLE` must describe the code it claims to describe.

    app/capabilities.py cited this test by name as the guard against drift, and the
    test did not exist -- a comment asserting a guarantee nothing enforced, the same
    shape as a gate whose PASS message names a property it never exercises.

    It catches the drift that motivated it: `remove_type` used to read
    `_TYPE_REF_PATTERNS`, then typescript/javascript were wired to a syntax-aware
    codemod instead. javascript has NO pattern entry, so the registry reported
    `generate_fix("javascript", "remove_type") == False` while a real codemod was
    wired -- an UNDER-claim, safe in direction but still a lie about the code.

    Two checks:

    1. every attribute `_OP_TABLE` names exists in fix_templates and is a dict, so
       `_table_languages` -- which silently returns the empty set for a non-dict --
       cannot make an operation look unsupported in every language;
    2. every language named in an explicit `lang ==` / `lang in (...)` branch of the
       dispatch appears in at least one of the tables, so adding a delegation
       without registering it fails here.
    """
    import ast
    import pathlib

    from app import capabilities as cap
    from app import fix_templates as ft

    # 1. Each named attribute must resolve to a dict keyed by language.
    for op, attr in cap._OP_TABLE.items():
        table = getattr(ft, attr, None)
        assert table is not None, (
            f"_OP_TABLE maps {op!r} to fix_templates.{attr}, which does not exist")
        assert isinstance(table, dict), (
            f"_OP_TABLE maps {op!r} to fix_templates.{attr}, which is a "
            f"{type(table).__name__}. _table_languages returns the EMPTY SET for a "
            f"non-dict, so every language would silently report unsupported")
        assert table, f"fix_templates.{attr} is empty, so {op!r} claims no languages"

    known = set()
    for attr in set(cap._OP_TABLE.values()):
        known |= set(getattr(ft, attr, {}))

    # 2. Any language the dispatch branches on explicitly must be registered.
    src = pathlib.Path(ft.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    all_langs = set(cap.languages())
    branched = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        if not (isinstance(left, ast.Name) and left.id in ("lang", "language")):
            continue
        for comparator in node.comparators:
            for lit in ast.walk(comparator):
                value = _ast_str(lit)
                if value in all_langs:
                    branched.add(value)

    assert branched, (
        "found no `lang == '...'` branch in fix_templates, so this check would pass "
        "vacuously. Refusing a pass over an empty set")

    missing = sorted(branched - known)
    assert not missing, (
        f"the dispatch branches on {missing} but no _OP_TABLE table lists "
        f"{'them' if len(missing) > 1 else 'it'}, so generate_fix() reports False "
        f"for a language that HAS a handler. Register the delegation in the table "
        f"_OP_TABLE names for that operation")

    # The specific claim that was wrong, asserted directly so the reason survives.
    assert cap.generate_fix("javascript", "remove_type"), (
        "javascript delegates remove_type to ts_codemod, so the registry must say so")
    assert cap.generate_fix("typescript", "remove_type")
    assert cap.generate_fix("python", "remove_type")


def _ast_str(node):
    """The string value of a literal node, or None. 3.7 uses ast.Str."""
    from app.ast_compat import str_literal
    return str_literal(node)


def test_typescript_remove_type_no_longer_destroys_a_live_import():
    """The exact input the shared regex corrupted while reporting success.

    `_TYPE_REF_PATTERNS['typescript']` opens with

        r'^\\s*import\\s+.*\\b{name}\\b.*$'

    which deletes the ENTIRE import statement when the removed name appears anywhere
    on it -- the identical defect already fixed in py_codemod, in a language that
    carries TWO production cells. Measured:

        import { User, Address } from './models';   ->  (line deleted)
        export function label(a: Address) { ... }   ->  UNCHANGED

    so `Address` became undefined while it claimed "Removed references to deleted
    type 'User' (1 lines affected)". `tsc` would catch the fallout eventually, which
    is the only thing that made this less severe than the javascript case -- the fix
    path still returned a broken file and called it success.
    """
    from app.fix_templates import apply_fix_template

    src = ("import { User, Address } from './models';\n"
           "\n"
           "export function label(a: Address): string {\n"
           "  return a.city;\n"
           "}\n")

    fixed, explanation = apply_fix_template(src, "typescript", "type_removed", "User")

    assert "Address" in fixed, (
        f"the still-used neighbour was destroyed with the import:\n{fixed}")
    assert "import { Address } from './models';" in fixed, fixed
    assert "User" not in fixed, fixed
    assert "return a.city;" in fixed
    assert "name in import list" in explanation, explanation


def test_javascript_remove_type_no_longer_deletes_every_line_naming_a_variable():
    """The most severe corruption found in the audit: a module reduced to `}`.

    javascript had NO `_TYPE_REF_PATTERNS` entry, so `_remove_type_reference` fell
    through to `_generic_remove`, which deletes every line matching any CASE VARIANT
    of the name. For a type called `User`:

        name_variants("User") -> snake 'user', camel 'user'

    which is the conventional variable name for a User, i.e. essentially every line
    of any real consumer. Measured:

        export function formatContact(user) {      ->   }
          return `${user.email}`;
        }

    reported as "Removed references to deleted type 'User' (2 lines affected)". And
    javascript has NO wired validator, so nothing downstream would have caught it.

    The correct answer for this input is to change NOTHING: the file never mentions
    `User` at all. `user` is a parameter.
    """
    from app.fix_templates import apply_fix_template

    src = ("export function formatContact(user) {\n"
           "  return `${user.email} ${user.phoneNumber}`;\n"
           "}\n"
           "\n"
           "export function toPayload(user) {\n"
           "  return {\n"
           "    id: user.id,\n"
           "    phone: user.phoneNumber,\n"
           "  };\n"
           "}\n")

    fixed, explanation = apply_fix_template(src, "javascript", "type_removed", "User")

    assert fixed == src, (
        f"nothing in this file references the type `User` -- only a variable named "
        f"`user` -- so the correct edit is none:\n{fixed}")
    assert "Could NOT remove" in explanation, explanation
    assert "lines affected" not in explanation, (
        f"claimed an edit count for a file it did not touch: {explanation}")

    # And the shape javascript CAN fix: a CommonJS binding list, neighbour kept.
    cjs = ("const { User, Address } = require('./models');\n"
           "\n"
           "module.exports = { Address };\n")
    out, expl = apply_fix_template(cjs, "javascript", "type_removed", "User")
    assert "const { Address } = require('./models');" in out, out
    assert "User" not in out, out
    assert "require binding list" in expl, expl


def test_ts_remove_type_refuses_every_shape_that_needs_a_decision():
    """A deleted type has no substitute value, so only a binding list is mechanical.

    Each refusal must carry a reason SPECIFIC to the shape -- a reviewer reading the
    PR body needs to know which decision is being asked of them, not "a human must
    decide". An alias is included because its local name is used elsewhere: dropping
    the import would orphan every use of `U`.
    """
    from app.ts_codemod import remove_type

    for src, expect in (
        ("export function label(u: User): string {\n  return u.city;\n}\n",
         "function signature"),
        ("let current: User | null = null;\n", "type annotation"),
        ("const u = new User({ id: '1' });\n", "construction"),
        ("class Admin extends User {\n  role = 'a';\n}\n", "base class"),
        ("class Admin implements User {\n  id = '';\n}\n", "implemented interface"),
        ("if (x instanceof User) { go(); }\n", "runtime type test"),
        ("const u = payload as User;\n", "type assertion"),
        ("import { User as U } from './m';\n\nexport function f(u: U) { return u; }\n",
         "aliased import"),
    ):
        r = remove_type(src, "User")
        assert r.refusals, f"should refuse: {src!r}"
        assert not r.complete, f"refused but reported complete: {src!r}"
        assert not r.changed, (
            f"a refusal must not edit the file, or the caller returns corrupted "
            f"output: {src!r} -> {r.code!r}")
        assert any(expect in x for x in r.refusals), (expect, r.refusals)

    # A comment or a string mention is a NOTE, never a refusal -- it cannot break a
    # build, and blocking on it would block nearly every real consumer.
    for src in ("// User was removed from the spec\nexport const X = 1;\n",
                "console.log('User');\nexport const X = 1;\n"):
        r = remove_type(src, "User")
        assert r.notes, src
        assert not r.refusals, (src, r.refusals)


def test_ruby_remove_field_no_longer_welds_the_next_line_on():
    """The exact input the old regex corrupted while reporting success.

    `_remove_field_ruby` removed a list element with

        re.sub(rf':{snake}\\s*,?\\s*', '', code)

    whose trailing `\\s*` matches NEWLINES. Removing the LAST element of
    `attr_accessor :id, :email, :phone_number` therefore consumed the line break
    -- and the blank line after it -- welding the following line on:

        attr_accessor :id, :email, def initialize(id: nil, email: nil)

    Two things make this the worst case in the codebase:

    1. It is SYNTACTICALLY VALID. `def` is an expression in Ruby, so `ruby -c`
       reports "Syntax OK" on the corrupted file. Measured with real ruby 2.0.
       A syntax check cannot catch this class; the damage only appears when the
       class is loaded, as `attr_accessor(:id, :email, nil)` ->
       "TypeError: nil is not a symbol".
    2. Ruby has NO wired validator, so nothing downstream would have caught it
       either. The fix path returned a file that raises on import and reported
       "Removed references to field 'phone_number' (1 lines affected)".

    The one-character fix (`\\s*` -> `[^\\S\\n]*`) is NOT sufficient: it leaves
    `attr_accessor :id, :email,` whose trailing comma is a line continuation in
    Ruby, so `end` still gets swallowed. The separator has to go with the
    element -- see `_drop_list_element`.
    """
    from app.fix_templates import apply_fix_template

    src = ('class User\n'
           '  attr_accessor :id, :email, :phone_number\n'
           '\n'
           '  def initialize(id: nil, email: nil, phone_number: nil)\n'
           '    @id = id\n'
           '    @email = email\n'
           '    @phone_number = phone_number\n'
           '  end\n'
           'end\n')

    fixed, explanation = apply_fix_template(
        src, "ruby", "removed_field", "phone_number")

    lines = fixed.split("\n")

    # The attribute list keeps its neighbours and stays a list of symbols only.
    attr_line = next(l for l in lines if "attr_accessor" in l)
    assert attr_line.strip() == "attr_accessor :id, :email", repr(attr_line)
    assert "def " not in attr_line, (
        f"the next line was welded onto the attribute list: {attr_line!r}")

    # `def initialize` is still a definition on its own line.
    assert any(l.strip().startswith("def initialize(") for l in lines), fixed

    # The field is genuinely gone from every shape, and the neighbours survive.
    assert "phone_number" not in fixed, fixed
    assert ":id" in fixed and ":email" in fixed
    assert "@id = id" in fixed and "@email = email" in fixed

    # No line count may collapse: the old bug merged two logical lines.
    assert len([l for l in lines if l.strip()]) == 7, (
        f"expected 7 non-blank lines, got "
        f"{len([l for l in lines if l.strip()])}: {fixed!r}")

    assert "1 lines affected" not in explanation or "phone_number" not in fixed

    # If a real ruby is on this machine, make it the judge rather than the
    # assertions above. `-c` is only a syntax check and is blind to the weld, so
    # LOAD the class: the old output raised TypeError here, the new one runs.
    import shutil
    import subprocess
    import tempfile

    ruby = shutil.which("ruby")
    if ruby:
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "user.rb")
            with open(path, "w") as fh:
                fh.write(fixed + "\nu = User.new(id: 'a', email: 'b')\n"
                                 "raise 'lost id' unless u.id == 'a'\n"
                                 "raise 'lost email' unless u.email == 'b'\n"
                                 "raise 'field survived' if u.respond_to?(:phone_number)\n")
            proc = subprocess.run([ruby, path], capture_output=True, text=True,
                                  timeout=60)
            assert proc.returncode == 0, (
                f"real ruby refused the fixed output:\n{proc.stderr}\n{fixed}")


def test_python_remove_field_no_longer_lies_about_success():
    """The exact input the old regex claimed to fix, and did not touch.

    Measured before app/py_codemod.py existed: every reference survived, one blank
    line was collapsed, and the explanation read "Removed references to field
    'phone_number' (2 lines affected). Cleaned: struct/class declarations, accessor
    methods, function params, object literals, and direct field access patterns for
    python." Two false claims in one string -- nothing was removed, and the five
    named categories were a fixed list appended to every success.

    `changed` being True on a whitespace-only edit is the dangerous part: the
    pipeline believes a fix exists and only the validator stands between that and a
    PR.
    """
    from app.fix_templates import apply_fix_template

    src = ('from src.models import User\n'
           '\n'
           '\n'
           'def format_contact(user: User) -> str:\n'
           '    return f"{user.full_name} <{user.email}> {user.phone_number}"\n'
           '\n'
           '\n'
           'def to_crm_payload(user: User) -> dict:\n'
           '    return {\n'
           '        "id": user.id,\n'
           '        "phone": user.phone_number,\n'
           '    }\n')

    fixed, explanation = apply_fix_template(
        code=src, language="python", change_type="removed_field",
        field_name="phone_number")

    # 1. the references are actually GONE, which was the whole failure
    assert "phone_number" not in fixed, fixed
    assert fixed != src

    # 2. the surviving code is still valid Python. A removal that leaves
    #    `f"{user.full_name} <{user.email}> "` is fine; one that leaves `f"{}"` or a
    #    dangling comma is not, and only parsing catches it.
    import ast as _ast
    _ast.parse(fixed)

    # 3. the fields that should NOT be touched survive
    assert "user.full_name" in fixed and "user.email" in fixed
    assert '"id": user.id,' in fixed

    # 4. PEP 8's two blank lines between top-level defs survive. The normaliser used
    #    to collapse every gap in the file, turning a one-field removal into a diff
    #    across every function boundary.
    assert fixed.count("\n\n\n") == src.count("\n\n\n"), \
        "blank-line runs changed -- the diff is no longer minimal"

    # 5. the explanation names the shapes ACTUALLY removed, not a canned list
    assert "f-string interpolation" in explanation, explanation
    assert "dict-literal entry" in explanation, explanation
    assert "accessor methods" not in explanation, \
        "the fabricated category list is back"


def test_python_remove_field_refuses_what_it_cannot_do_safely():
    """Four shapes that must abstain, each with exactly one reason.

    Two of these were actively CORRUPTED by the old regexes rather than refused:
    `\\b{f}\\s*:\\s*[^,=)]+...` stripped a function parameter, and
    `^\\s*.*\\[["\\']?{f}...` deleted the entire line containing a subscript.
    """
    from app.fix_templates import apply_fix_template

    cases = {
        "parameter": 'def send(phone_number: str) -> None:\n    print(phone_number)\n',
        "aliased": 'def f(u: object) -> object:\n    phone = u.phone_number\n    return phone\n',
        "subscript": 'def f(d: dict) -> str:\n    return d["phone_number"]\n',
        "side_effect_default": ('from dataclasses import dataclass\n\n\n'
                                '@dataclass\nclass U:\n'
                                '    phone_number: str = fetch()\n'),
    }
    for name, src in cases.items():
        fixed, explanation = apply_fix_template(
            code=src, language="python", change_type="removed_field",
            field_name="phone_number")
        assert fixed == src, f"{name}: the codemod edited a shape it cannot handle"
        assert "Could NOT remove" in explanation, f"{name}: {explanation}"
        assert "NEEDS A HUMAN" in explanation, f"{name}: {explanation}"

    # ONE reason per reference. The side-effecting default was refused twice -- once
    # specifically by the declaration pass and once generically by the final
    # classification pass -- so the PR body restated it worse and the count doubled.
    _, explanation = apply_fix_template(
        code=cases["side_effect_default"], language="python",
        change_type="removed_field", field_name="phone_number")
    refusals = [l for l in explanation.split("\n") if l.startswith("NEEDS A HUMAN")]
    assert len(refusals) == 1, refusals
    assert "the default contains" in refusals[0], refusals[0]


def test_python_remove_field_notes_mentions_without_refusing_them():
    """A comment or string naming the field is reported, never edited.

    ts_codemod learned this the expensive way: refusing benign mentions set
    complete=False, and nearly every real consumer has a log line or comment naming
    the field it uses, so the one real repository came back BLOCKED. A safety rule
    that blocks the safe cases is not conservative, it is broken.

    The subscript case is the deliberate exception -- see the codemod's docstring.
    """
    from app.fix_templates import apply_fix_template
    from app.py_codemod import remove_field

    src = '# phone_number was removed upstream\nX = "phone_number"\n'
    result = remove_field(src, "phone_number")
    assert result.code == src, "a comment or string was edited"
    assert not result.refusals, result.refusals
    assert len(result.notes) == 2, result.notes
    assert any("comment" in n for n in result.notes), result.notes
    assert any("string" in n for n in result.notes), result.notes

    _, explanation = apply_fix_template(
        code=src, language="python", change_type="removed_field",
        field_name="phone_number")
    assert "NOTE:" in explanation and "NEEDS A HUMAN" not in explanation, explanation


def test_blank_line_normaliser_does_what_its_docstring_says():
    """`\\n{3,}` -> `\\n\\n` collapsed to ONE blank line while claiming to leave two.

    The docstring and the regex disagreed and the regex won. Python felt it worst:
    PEP 8 requires two blank lines between top-level definitions, so every Python fix
    reformatted every gap in the file.
    """
    from app.fix_templates import _clean_blank_lines

    # two blank lines are PRESERVED
    assert _clean_blank_lines("a\n\n\nb\n") == "a\n\n\nb\n"
    # one blank line is preserved
    assert _clean_blank_lines("a\n\nb\n") == "a\n\nb\n"
    # three or more collapse to exactly two, as documented
    assert _clean_blank_lines("a\n\n\n\nb\n") == "a\n\n\nb\n"
    assert _clean_blank_lines("a\n\n\n\n\n\nb\n") == "a\n\n\nb\n"


def test_codemod_result_has_exactly_one_definition():
    """`complete` is a rule, and a rule with two copies drifts.

    CodemodResult lived in ts_codemod.py until Python needed the same contract.
    Duplicating it would have put two definitions of `complete` in the tree, which is
    the defect already removed twice -- from the capability registry and from the
    production predicate.
    """
    from app import codemod_result, ts_codemod, py_codemod

    assert ts_codemod.CodemodResult is codemod_result.CodemodResult
    assert py_codemod.CodemodResult is codemod_result.CodemodResult

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    defining = []
    for name in sorted(os.listdir(os.path.join(root, "app"))):
        if not name.endswith(".py"):
            continue
        body = open(os.path.join(root, "app", name)).read()
        if "class CodemodResult" in body:
            defining.append(name)
    assert defining == ["codemod_result.py"], defining


def test_e2e_go_openapi_change_field_type():
    """The FIFTH golden path, and the first for the Go validator.

    detect -> canonical op -> fix -> APPLY -> go build -> minimal diff

    Go was the last wired validator with nothing proven, which is a specific kind of
    gap: validate_ok counted 63 more cells than before, and not one of them had been
    shown to work. A wired validator with no cell is a claim about a toolchain, not
    about the product.

    WHY change_field_type AND NOT remove_field
    Measured before this fixture was written. The Go remove_field codemod edits the
    STRUCT DECLARATION and does nothing at all to a usage file:

        _remove_field_go(usage_file,  "PhoneNumber")  ->  unchanged
        _remove_field_go(struct_file, "PhoneNumber")  ->  field removed

    For a removal the declaration is regenerated correct and the USAGES are stale, so
    the one file it can edit is the one file that does not need editing. go/openapi/
    remove_field is therefore not production-ready, and a fixture for it would fail
    at step 4. Only change_field_type and remove_field are even detectable for
    go/openapi, so change_field_type is the only viable Go cell today.

    THE TYPE-NAME HAZARD IS SHARPER IN GO
    `native_type("go", "integer")` is `int32`, not `int`. A struct declaring `int`
    does not match, and the codemod correctly ANNOTATES instead of editing --
    "Ripple did NOT transform the code: it lacks the information to do so correctly".
    The fixture declares the sized type, which is also what OpenAPI codegen produces
    for an integer with a 32-bit format, so the spec and the struct agree.

    GO IS THE STRICTEST VALIDATOR
    Unused imports are compile errors here. This cell does not trip that -- the
    retype keeps `strings` in use -- but a remove_field cell would have to drop the
    now-unused import or fail the build, which is the validator earning its keep.
    """
    import filecmp
    import shutil as _sh
    import tempfile
    from app.capability_claims import ValidationState
    from app.change_types import canonical_op, category, MECHANICAL
    from app.fix_templates import apply_fix_template
    from app.validation import validate, choose_backend

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    base = os.path.join(root, "fixtures", "go-openapi", "change-field-type")
    consumer = os.path.join(base, "consumer")

    backend, note = choose_backend()
    if backend != "docker":
        print(f"      SKIP: go validation needs docker, got {backend or 'none'} "
              f"({note}) -- no e2e evidence written")
        return

    # 1. the operation is mechanical, so a transformation is legitimate at all
    assert canonical_op("field_type_changed") == "change_field_type"
    assert category("field_type_changed") == MECHANICAL

    work_root = tempfile.mkdtemp(prefix="ripple-e2e-go-")
    work = os.path.join(work_root, "consumer")
    try:
        _sh.copytree(consumer, work,
                     ignore=_sh.ignore_patterns(".ripple-gomodcache", ".git"))
        target = os.path.join(work, "models", "user.go")

        # 2. the consumer genuinely does not build first. Two errors, both in
        #    contact.go, both the sized integer being passed where a string is wanted.
        before = validate("go", work)
        assert before.state is ValidationState.INVALID, \
            f"{before.state.value}: {before.reason}"
        assert len(before.errors) == 2, before.errors
        assert all("contact.go" in e for e in before.errors), before.errors

        # 3. generate and APPLY the fix (read before write)
        with open(target) as fh:
            original = fh.read()
        fixed, explanation = apply_fix_template(
            code=original, language="go", change_type="field_type_changed",
            field_name="PhoneNumber", old_type="integer", new_type="string")
        assert fixed != original, "the transformation did nothing"
        # It must EDIT, not annotate. The annotate path is correct behaviour for an
        # unmapped type but means this cell is not ready, so it has to be excluded
        # explicitly rather than passing by accident.
        assert "RIPPLE-ACTION-REQUIRED" not in fixed, \
            "the codemod annotated instead of editing -- the declared type did not " \
            "match native_type('go', 'integer')"
        assert "PhoneNumber string" in fixed, fixed
        assert "int32" not in fixed, fixed
        # exactly ONE line differs. The first draft of this fixture quoted the type
        # name in its doc comment and got four extra edits inside the comment.
        assert (len(fixed.splitlines()) == len(original.splitlines())
                and sum(1 for a, b in zip(original.splitlines(), fixed.splitlines())
                        if a != b) == 1), "more than one line changed"

        with open(target, "w") as fh:
            fh.write(fixed)
        assert open(target).read() == fixed

        # 4. the real compiler accepts it
        after = validate("go", work)
        assert after.state is ValidationState.VALID, \
            f"{after.reason} :: {after.errors[:3]}"
        assert after.evidence["typecheck_exit"] == 0

        # 5. the diff is MINIMAL. contact.go is listed because it is the file that
        #    ERRORED: the fix must repair it without editing it.
        for untouched in ("contact.go", "orders.go", "go.mod"):
            assert filecmp.cmp(os.path.join(consumer, untouched),
                               os.path.join(work, untouched), shallow=False), \
                f"{untouched} was modified -- the PR would not be reviewable"

        # 6. proof written only after a real build
        _record_e2e_evidence(("go", "openapi", "change_field_type"), after)
    finally:
        _sh.rmtree(work_root, ignore_errors=True)


def test_e2e_python_openapi_remove_field():
    """The FOURTH golden path -- and the one that was impossible this morning.

    detect -> canonical op -> fix -> APPLY -> mypy -> minimal diff

    This cell could not exist until app/py_codemod.py replaced five context-free
    regexes. Measured on this fixture's own consumer, the old handler returned every
    reference intact while reporting "Removed references to field 'phone_number'
    (2 lines affected)", so step 4 would have failed and the cell would correctly
    have refused to become production-ready. Two of those regexes were worse than
    useless -- one stripped a function parameter, one deleted whole lines around a
    subscript -- and no test noticed, because there was no Python fixture.

    THE INVERSION, AGAIN, IN THE OTHER DIRECTION
    The change-field-type cells edit the DECLARATION because a retype leaves the
    usages correct. A removal is the opposite: models.py is regenerated from the new
    spec and is already right, so the USAGES are stale and checkout.py is the fix
    target. models.py must come out byte-identical, which this asserts -- the same
    relationship as the TypeScript remove-field cell.

    Both edited shapes are ones the codemod can remove with no behavioural change:
    an f-string interpolation and a dict-literal entry. A parameter or an aliased
    local in this file would be refused, correctly, and the cell would not qualify.
    """
    import filecmp
    import shutil as _sh
    import tempfile
    from app.capability_claims import ValidationState
    from app.change_types import canonical_op, category, MECHANICAL
    from app.fix_templates import apply_fix_template
    from app.validation import validate, choose_backend

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    base = os.path.join(root, "fixtures", "python-openapi", "remove-field")
    consumer = os.path.join(base, "consumer")

    backend, note = choose_backend()
    if backend != "docker":
        print(f"      SKIP: python validation needs docker, got {backend or 'none'} "
              f"({note}) -- no e2e evidence written")
        return

    # 1. the operation is mechanical, so a transformation is legitimate at all
    assert canonical_op("removed_field") == "remove_field"
    assert category("removed_field") == MECHANICAL

    work_root = tempfile.mkdtemp(prefix="ripple-e2e-pyrm-")
    work = os.path.join(work_root, "consumer")
    try:
        _sh.copytree(consumer, work,
                     ignore=_sh.ignore_patterns("__pycache__", ".mypy_cache",
                                                ".ripple-venv", ".git"))
        target = os.path.join(work, "src", "checkout.py")

        # 2. the consumer genuinely does not typecheck first. Two attribute errors,
        #    one per stale reference, and NO annotation gaps -- a gap would make the
        #    verdict UNABLE_TO_VALIDATE and a later pass would prove nothing.
        before = validate("python", work)
        assert before.state is ValidationState.INVALID, \
            f"{before.state.value}: {before.reason}"
        assert len(before.errors) == 2, before.errors
        assert before.evidence["mypy_annotation_gaps"] == 0, before.evidence

        # 3. generate and APPLY the fix (read before write)
        with open(target) as fh:
            original = fh.read()
        fixed, explanation = apply_fix_template(
            code=original, language="python", change_type="removed_field",
            field_name="phone_number")
        assert fixed != original, "the transformation did nothing"

        # The failure this cell exists to prevent: a no-op that reports success.
        assert "user.phone_number" not in fixed, fixed
        # ...and the explanation must name the shapes it really removed, not the
        # fabricated five-category list the old success path appended to everything.
        assert "f-string interpolation" in explanation, explanation
        assert "dict-literal entry" in explanation, explanation
        assert "accessor methods" not in explanation, explanation
        # The neighbours survive: only the removed field's references go.
        assert "user.full_name" in fixed and "user.email" in fixed, fixed
        assert '"id": user.id,' in fixed, fixed
        # PEP 8's two blank lines between top-level defs survive
        assert fixed.count("\n\n\n") == original.count("\n\n\n"), \
            "blank-line runs changed -- the diff is no longer minimal"
        # and it is still parseable Python, which a dangling comma or `f"{}"` is not
        import ast as _ast
        _ast.parse(fixed)

        with open(target, "w") as fh:
            fh.write(fixed)
        assert open(target).read() == fixed

        # 4. the real typechecker accepts it
        after = validate("python", work)
        assert after.state is ValidationState.VALID, \
            f"{after.reason} :: {after.errors[:3]}"
        assert after.evidence["typecheck_exit"] == 0

        # 5. the diff is MINIMAL. models.py is listed first because it is the file
        #    the SIBLING cell edits -- here it is already correct and touching it
        #    would mean the codemod went after the declaration instead of the usages.
        for untouched in ("src/models.py", "src/orders.py", "src/__init__.py",
                          "mypy.ini", "requirements-dev.txt"):
            assert filecmp.cmp(os.path.join(consumer, untouched),
                               os.path.join(work, untouched), shallow=False), \
                f"{untouched} was modified -- the PR would not be reviewable"

        # 6. proof written only after a real typecheck
        _record_e2e_evidence(("python", "openapi", "remove_field"), after)
    finally:
        _sh.rmtree(work_root, ignore_errors=True)


def test_e2e_python_openapi_change_field_type():
    """The THIRD golden path -- and the first in a language other than TypeScript.

    detect -> canonical op -> fix -> APPLY -> mypy -> minimal diff

    This cell proves the NEW thing, which is the Python validator, rather than a
    third fixture for an already-proven toolchain. The operation is the same as the
    TypeScript cell deliberately: holding the codemod constant means a failure here
    is attributable to validation, not to a transformation nobody had exercised.

    Why the op is change_field_type and not remove_field, which would have mirrored
    the first cell: the Python remove_field codemod DOES NOT WORK. Measured before
    this fixture was written -- given a consumer reading `user.phone_number`, it
    returns the file with every reference still present while reporting "Removed
    references to field 'phone_number' (2 lines affected)". A fixture for that cell
    would fail at step 4, correctly, and the cell is not production-ready. It is
    recorded in expected.json rather than quietly avoided.

    Three hazards, all measured:

    1. mypy's reach depends on the CONSUMER's annotations, not on the project config
       the way tsc's does. The same broken file passes mypy at exit 0 once the
       parameter annotation is removed. Every def in this fixture is annotated, and
       the runner passes --disallow-untyped-defs so a gap yields
       UNABLE_TO_VALIDATE rather than a pass it could not have earned.
    2. _change_type_python is field-blind -- it rewrites every occurrence of the old
       annotation and never reads field_name. models.py has exactly one.
    3. The codemod does not respect comments. The first draft of this fixture said
       the pattern out literally in its docstring and got "3 type annotations
       updated" for a one-field change, two of them inside the comment. The prose
       now avoids the pattern.

    SKIPS rather than passes with no docker, for the same reason as the other cells.
    """
    import filecmp
    import shutil as _sh
    import tempfile
    from app.capability_claims import ValidationState
    from app.change_types import canonical_op, category, MECHANICAL
    from app.fix_templates import apply_fix_template
    from app.validation import validate, choose_backend

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    base = os.path.join(root, "fixtures", "python-openapi", "change-field-type")
    consumer = os.path.join(base, "consumer")

    backend, note = choose_backend()
    if backend != "docker":
        # Python validation is docker-only by design: choose_backend()'s host path
        # probes for NODE, which is no evidence that python or mypy exist.
        print(f"      SKIP: python validation needs docker, got {backend or 'none'} "
              f"({note}) -- no e2e evidence written")
        return

    # 1. the operation is mechanical, so a transformation is legitimate at all
    assert canonical_op("field_type_changed") == "change_field_type"
    assert category("field_type_changed") == MECHANICAL

    work_root = tempfile.mkdtemp(prefix="ripple-e2e-py-")
    work = os.path.join(work_root, "consumer")
    try:
        _sh.copytree(consumer, work,
                     ignore=_sh.ignore_patterns("__pycache__", ".mypy_cache",
                                                ".ripple-venv", ".git"))
        target = os.path.join(work, "src", "models.py")

        # 2. the consumer genuinely does not typecheck first. Two errors, both in
        #    contact.py: .strip() and .startswith() on an int.
        before = validate("python", work)
        assert before.state is ValidationState.INVALID, \
            f"{before.state.value}: {before.reason}"
        assert len(before.errors) == 2, before.errors
        assert before.evidence["mypy_annotation_gaps"] == 0, \
            "the fixture must be fully annotated, or a pass would prove nothing"

        # 3. generate and APPLY the fix (read before write)
        with open(target) as fh:
            original = fh.read()
        fixed, explanation = apply_fix_template(
            code=original, language="python", change_type="field_type_changed",
            field_name="phone_number", old_type="integer", new_type="string")
        assert fixed != original, "the transformation did nothing"
        assert "phone_number: str" in fixed, fixed
        # sibling str fields survive verbatim -- proof the field-blind regex was
        # pointed at the int annotation and not at the str ones
        assert fixed.count(": str") == 4, fixed
        # Word boundary, not substring: the docstring contains "(type: integer)",
        # which a plain `": int" not in fixed` test matches. The codemod uses
        # `:\s*int\b`, which correctly does NOT match inside "integer" -- so the
        # assertion has to use the same semantics or it fails on prose the codemod
        # never touched. (It did, on the first run.)
        assert re.search(r":\s*int\b", fixed) is None, fixed
        # and exactly ONE line changed: if the comment gets rewritten too, this
        # fixture is measuring a three-line diff and calling it one
        assert (len(fixed.splitlines()) == len(original.splitlines())
                and sum(1 for a, b in zip(original.splitlines(), fixed.splitlines())
                        if a != b) == 1), "more than one line changed"
        with open(target, "w") as fh:
            fh.write(fixed)
        assert open(target).read() == fixed

        # 4. the real typechecker accepts it
        after = validate("python", work)
        assert after.state is ValidationState.VALID, \
            f"{after.reason} :: {after.errors[:3]}"
        assert after.evidence["typecheck_exit"] == 0

        # 5. the diff is MINIMAL. contact.py is listed because it is the file that
        #    ERRORED: the fix must repair it without editing it.
        for untouched in ("src/contact.py", "src/orders.py", "src/__init__.py",
                          "mypy.ini", "requirements-dev.txt"):
            assert filecmp.cmp(os.path.join(consumer, untouched),
                               os.path.join(work, untouched), shallow=False), \
                f"{untouched} was modified -- the PR would not be reviewable"

        # 6. proof written only after a real typecheck
        _record_e2e_evidence(("python", "openapi", "change_field_type"), after)
    finally:
        _sh.rmtree(work_root, ignore_errors=True)


def test_python_validator_refuses_to_pass_an_unannotated_consumer():
    """A mypy pass on unannotated code must be UNABLE_TO_VALIDATE, never VALID.

    This is the load-bearing property of the Python validator and the reason it is
    not simply `mypy`. Measured: the change-field-type fixture, broken, passes mypy
    at exit 0 once the parameter annotation is removed -- so an exit-0-means-valid
    runner would have granted AUTO to a consumer it could not read.

    Runs on the SAME broken fixture with one annotation stripped, so it cannot drift
    from the cell it protects.
    """
    import shutil as _sh
    import tempfile
    from app.capability_claims import ValidationState
    from app.validation import validate, choose_backend

    backend, note = choose_backend()
    if backend != "docker":
        print(f"      SKIP: python validation needs docker ({note})")
        return

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    consumer = os.path.join(root, "fixtures", "python-openapi",
                            "change-field-type", "consumer")
    work_root = tempfile.mkdtemp(prefix="ripple-py-unann-")
    work = os.path.join(work_root, "consumer")
    try:
        _sh.copytree(consumer, work,
                     ignore=_sh.ignore_patterns("__pycache__", ".mypy_cache",
                                                ".ripple-venv", ".git"))
        path = os.path.join(work, "src", "contact.py")
        body = open(path).read()
        # Strip the annotations from BOTH defs. The first version of this test
        # stripped only one and got INVALID -- correctly, because the OTHER def was
        # still annotated and still reported attr-defined, and a real type error
        # outranks an annotation gap. To isolate the property under test, no real
        # error may remain: mypy must then have nothing to say except that it cannot
        # see the types.
        stripped = body.replace("def normalise_phone(user: User) -> str:",
                                "def normalise_phone(user):")
        stripped = stripped.replace("def is_international(user: User) -> bool:",
                                    "def is_international(user):")
        assert stripped.count("(user):") == 2, \
            "the fixture signatures changed -- update this test"
        open(path, "w").write(stripped)

        v = validate("python", work)
        assert v.state is ValidationState.UNABLE_TO_VALIDATE, (
            f"an unannotated consumer produced {v.state.value}, not "
            f"UNABLE_TO_VALIDATE -- mypy cannot see the types here, so this would "
            f"be a pass earned by absence of evidence")
        assert v.evidence["mypy_annotation_gaps"] >= 2, v.evidence
        assert v.evidence["mypy_errors"] == 0, \
            f"a real type error survived, so this is not testing the gap path: {v.errors}"
        assert "could not see the types" in v.reason, v.reason
    finally:
        _sh.rmtree(work_root, ignore_errors=True)


def test_auto_is_real_only_for_proven_cells_and_unreachable_otherwise():
    """AUTO exists. It must remain impossible to obtain without earning it.

    Before Stage 6 this was easy to guarantee, because AUTO was unreachable for all
    1800 cells -- nothing validated. Now a small set of cells reach it, which is the
    first time the mechanism has had to distinguish rather than simply refuse. That
    makes this the load-bearing test of the whole plan.

    The expected AUTO set is DERIVED from E2E_FIXTURES, not written as a literal.
    It was a literal while there was one cell, and adding the second broke this
    test -- which is the right outcome for a hardcoded expectation but the wrong
    place to spend the correction. Deriving it means the invariant under test is
    "AUTO iff proven", which is the actual rule, and a third fixture needs no edit
    here. A literal would also have let someone add a fixture and silently keep
    this assertion true by editing the list to match.
    """
    from app import capability_claims as cc
    from app.capabilities import CONTRACT_ENGINES
    from app.change_types import CANONICAL_OPS, MECHANICAL
    from app.routing import pr_level, Level
    from app import languages

    autos, counts = [], {"AUTO": 0, "REVIEW": 0, "BLOCKED": 0}
    for lang in sorted(languages.languages()):
        for contract in sorted(CONTRACT_ENGINES):
            for op in sorted(CANONICAL_OPS):
                # validated=True: this sweep asks "which cells COULD be AUTO once a
                # patch compiles", which is the registry question. Whether a given
                # patch compiled is a separate fact, asserted immediately below.
                d = pr_level(lang, contract, op, confidence=0.99, min_confidence=0.5,
                             validated=True)
                counts[d.level.value] += 1
                if d.level is Level.AUTO:
                    autos.append((lang, contract, op))

    # 1. exactly the proven cells, and nothing else
    expected = sorted(cell for cell in cc.E2E_FIXTURES
                      if cc.production_ready(*cell))
    assert expected, "no cell is production-ready -- this test would be vacuous"
    assert sorted(autos) == expected, (sorted(autos), expected)
    total = sum(counts.values())
    assert counts["AUTO"] == len(expected), counts
    assert counts["REVIEW"] == total - len(expected), counts

    # 1b. AUTO REQUIRES A LIVE VALIDATION OF THIS PATCH. Registry evidence proves the
    #     CELL works -- that this combination has an end-to-end fixture which
    #     compiles. It says nothing about whether THIS patch, on THIS repository,
    #     compiles, and conflating the two is what AUTO used to rest on. Checked for
    #     EVERY proven cell: a per-cell rule verified on one cell is a rule verified
    #     nowhere in particular.
    for cell in expected:
        for validated, want in ((True, Level.AUTO),
                                (False, Level.REVIEW),
                                (None, Level.REVIEW)):
            d = pr_level(*cell, confidence=0.99, min_confidence=0.5,
                         validated=validated)
            assert d.level is want, (
                f"{'/'.join(cell)} validated={validated!r} produced {d.level.value}, "
                f"wanted {want.value} -- 'we could not check' must never read as "
                f"'it is fine'")
        assert any("not validated" in r
                   for r in pr_level(*cell, confidence=0.99, min_confidence=0.5,
                                     validated=None).reasons), \
            f"{'/'.join(cell)}: an unvalidated fix is downgraded without saying why"
        assert any("did not typecheck" in r
                   for r in pr_level(*cell, confidence=0.99, min_confidence=0.5,
                                     validated=False).reasons), \
            f"{'/'.join(cell)}: a failed compile is downgraded without saying why"

    # 2. every AUTO must satisfy the registry AND be mechanical
    for lang, contract, op in autos:
        assert cc.production_ready(lang, contract, op), (lang, contract, op)
        assert not cc.blocking_reasons(lang, contract, op)
        assert CANONICAL_OPS[op][0] == MECHANICAL, \
            f"{op} is not mechanical -- a judgment operation must never be AUTO"

    # 3. NO judgment / wire_only / non_breaking operation is AUTO, in any language
    for lang, contract, op in [(l, c, o) for l in languages.languages()
                               for c in CONTRACT_ENGINES for o in CANONICAL_OPS
                               if CANONICAL_OPS[o][0] != MECHANICAL]:
        assert pr_level(lang, contract, op, 0.99, 0.5, validated=True).level is not Level.AUTO, \
            f"{op} ({CANONICAL_OPS[op][0]}) reached AUTO"

    # 4. Removing the e2e evidence must take AUTO away. If it does not, the level is
    #    decoration rather than a computation over facts.
    saved = dict(cc.E2E_FIXTURES)
    cc.E2E_FIXTURES.clear()
    try:
        d = pr_level("typescript", "openapi", "remove_field", 0.99, 0.5,
                validated=True)
        assert d.level is Level.REVIEW, d
        assert any("end-to-end" in r for r in d.reasons), d.reasons
    finally:
        cc.E2E_FIXTURES.update(saved)
    assert pr_level("typescript", "openapi", "remove_field", 0.99, 0.5,
                validated=True).level is Level.AUTO

    # 5. Same for validation.
    ts = cc.VALIDATORS["typescript"]
    cc.VALIDATORS["typescript"] = cc.ValidatorSpec("typescript", ts.toolchain,
                                                   implemented_by="", note=ts.note)
    try:
        d = pr_level("typescript", "openapi", "remove_field", 0.99, 0.5,
                validated=True)
        assert d.level is Level.REVIEW, d
        assert any("UNABLE_TO_VALIDATE" in r for r in d.reasons), d.reasons
    finally:
        cc.VALIDATORS["typescript"] = ts
    assert pr_level("typescript", "openapi", "remove_field", 0.99, 0.5,
                validated=True).level is Level.AUTO

    # 6. Confidence still gates independently -- AUTO is not a bypass.
    low = pr_level("typescript", "openapi", "remove_field", 0.10, 0.5)
    assert low.level is Level.BLOCKED and not low.opens_pr

    # 7. The PR body for AUTO must SHOW the evidence, not merely assert it.
    from app.confidence import format_pr_body
    body = format_pr_body("Field removed", "acme/api", 0.95, ["grep"], ["ref"],
                          decision=pr_level("typescript", "openapi",
                                            "remove_field", 0.95, 0.5,
                                            validated=True))
    first = body.split("\n")[0]
    assert "Automated fix, validation passed" in first, first
    assert "tsc --noEmit" in body and "byte-compared" in body
    assert "audit_capabilities" in body, "the claim must point at what recomputes it"

    # and a REVIEW body must never make that claim
    review = format_pr_body("Field removed", "acme/api", 0.95, ["grep"], ["ref"],
                            decision=pr_level("swift", "proto", "removed_field",
                                              0.95, 0.5, validated=True))
    assert "validation passed" not in review

    # nor may a body claim it when the patch itself was never compiled -- the
    # heading is derived from the decision, so an unvalidated fix must read as REVIEW
    unvalidated = format_pr_body("Field removed", "acme/api", 0.95, ["grep"], ["ref"],
                                 decision=pr_level("typescript", "openapi",
                                                   "remove_field", 0.95, 0.5,
                                                   validated=None))
    assert "validation passed" not in unvalidated, \
        "a PR body claimed validation passed for a fix that was never compiled"
    assert "human review required" in review.split("\n")[0]


def test_codemod_reports_every_reference_it_cannot_handle():
    """A reference the codemod cannot see is worse than one it refuses.

    Stage 7 measured the first version against a REAL repository -- the billing-api
    demo consumer -- and it returned changed=False, edits=0, REFUSALS=0 while the
    file contained FOUR references: two interface property declarations, a function
    parameter, and a shorthand object property. Detection searched only for
    `.field`, so none of those four were member accesses and none were seen.

    "Nothing to do" and "four things I cannot do" are different answers. Reporting
    the first for the second is the silent-gap defect this project keeps finding, and
    it is worse here than elsewhere: `complete` would have been False with no reason
    attached, so the PR body could not say why.

    Detection is now by word boundary. Three shapes are transformed; everything else
    is named individually with a line number.
    """
    from app.ts_codemod import remove_field

    # 1. THE REAL-REPOSITORY SHAPE. Two declarations are removed (a mirror of a
    #    field that no longer exists upstream is dead), and the parameter and
    #    shorthand are REFUSED -- removing a parameter breaks every caller, which is
    #    a change Ripple is not making in this PR.
    real = (
        "export interface User {\n"
        "  id: string;\n"
        "  phoneNumber: string;\n"
        "}\n"
        "export interface CreateUserRequest {\n"
        "  phoneNumber: string;\n"
        "}\n"
        "async function createUser(email: string, phoneNumber: string) {\n"
        "  const request: CreateUserRequest = { email, phoneNumber };\n"
        "  return request;\n"
        "}\n"
    )
    r = remove_field(real, "phoneNumber")
    assert r.changed and not r.complete
    assert len([e for e in r.edits
                if e["shape"] == "keyed property (inert value)"]) == 2, r.edits
    assert len(r.refusals) == 2, r.refusals
    assert all("line " in x for x in r.refusals), r.refusals
    assert any("createUser" in x for x in r.refusals)
    assert any("{ email, phoneNumber }" in x for x in r.refusals)

    # 2. Every refusal carries a REASON, not just a location.
    for x in r.refusals:
        assert "human must decide" in x, x

    # 3. A file with no reference at all is not a refusal.
    clean = remove_field("export const x = 1;\n", "phoneNumber")
    assert not clean.changed and not clean.refusals

    # 4. A reference in a COMMENT or STRING is a NOTE, not a refusal. Refusing it
    #    set complete=False, so a stale comment vetoed the whole file -- and nearly
    #    every real consumer has one, which is why the one real repository tested
    #    came back BLOCKED. Reported, never edited, never blocking.
    only_comment = remove_field("// phoneNumber was removed upstream\nexport const y = 2;\n",
                                "phoneNumber")
    assert not only_comment.changed
    assert not only_comment.refusals, "a comment must not block"
    assert len(only_comment.notes) == 1, only_comment.notes

    only_string = remove_field('console.log("phoneNumber");\n', "phoneNumber")
    assert not only_string.refusals and len(only_string.notes) == 1

    # An edit ALONGSIDE a comment and a string must still complete -- this is the
    # case that unblocked real consumers.
    mixed = remove_field(
        "// phoneNumber removed upstream\n"
        "const p = {\n  a: user?.phoneNumber,\n};\n"
        'console.log("phoneNumber gone");\n', "phoneNumber")
    assert mixed.complete, (mixed.refusals, mixed.notes)
    assert len(mixed.edits) == 1 and len(mixed.notes) == 2
    assert "user?.phoneNumber" not in mixed.code
    assert "// phoneNumber removed upstream" in mixed.code, "the comment must survive"
    assert 'console.log("phoneNumber gone")' in mixed.code, "the string must survive"

    # 4b. Optional chaining is a handled shape -- `?.` changes nothing about whether
    #     the reference is removable, and treating it as unhandled was an oversight.
    for src in ('const p = {\n  a: user?.phoneNumber,\n};\n',
                'const s = `${user?.phoneNumber}`;\n'):
        r_opt = remove_field(src, "phoneNumber")
        assert r_opt.complete, (src, r_opt.refusals)

    # 5. The golden fixture is UNAFFECTED by the broadened detection -- the two
    #    mechanical shapes still resolve completely, which is what keeps AUTO earned.
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fixture = os.path.join(root, "fixtures", "typescript-openapi", "remove-field",
                           "consumer", "src", "checkout.ts")
    g = remove_field(open(fixture).read(), "phoneNumber")
    assert g.complete and len(g.edits) == 2 and not g.refusals, (g.edits, g.refusals)
    assert "phoneNumber" not in g.code
    assert "user.}" not in g.code


def test_codemod_coverage_does_not_regress():
    """Coverage is a number that must not fall, and correctness must not break.

    The coverage audit exits 0 on a coverage gap deliberately -- a gap is a task, and
    failing the build on it would pressure someone into reclassifying a judgment call
    as an edit to make the number go up. That is the one outcome that must never
    happen, so the ratchet lives here instead.

    Measured per REFERENCE, not per case: a file with four references and one bad
    shape is three automatable references plus one that needs a human, and the ratio
    is what predicts whether a design partner ever sees an automated fix. The AUTO
    flag was already true while the one real repository tested came back BLOCKED.
    """
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
    import audit_codemod_coverage as cov
    from app.ts_codemod import remove_field

    handled = missed = judgment = notes = 0
    for case_id, src, expected in cov.CORPUS:
        r = remove_field(src, cov.FIELD)
        h, m, j, n, problems = cov._classify(r, expected)
        assert not problems, f"{case_id}: {problems}"
        handled += h; missed += m; judgment += j; notes += n

    total = handled + missed

    # COUNTS, not a percentage. The audit printed 84.6% as "85%" and a floor set from
    # that display then failed against the real value. Integers cannot round.
    # 17 after the JSX attribute shape landed: React consumers dominate real
    # TypeScript, so an attribute is plausibly more common than the object literal
    # that was already handled.
    assert handled >= 17, f"handled dropped to {handled} of {total}, was 17"
    assert missed <= 1, f"{missed} unimplemented shapes, was 1"

    # Judgment references must stay refused. If this count ever DROPS, a judgment
    # call was silently transformed -- which would raise coverage while making the
    # product less safe, so it is the assertion that matters most here.
    # 7 after default-parameter-object-value was added: it is the case that fails if
    # _inside_jsx_tag is ever deleted, which was measured to destroy a signature.
    assert judgment == 7, f"judgment references changed to {judgment}, was 7"
    # 5 after the same-line template case was added: its static text is a note while
    # its ${...} contents are an edit. If this DROPS, the position classifier stopped
    # distinguishing prose from code -- which would either rewrite a customer's
    # string or leave a reference that cannot compile.
    assert notes == 5, notes

    # And the gate itself is green on the real corpus.
    assert cov.main([]) == 0


def test_diff_contract_catches_what_the_compiler_cannot():
    """A green compiler means well-typed, never correct. MEASURED, not argued.

    Five corrupting mutations were applied to a fix `tsc --noEmit` had accepted:

        delete an unrelated field       VALID   <- compiler blind
        change the wrong property       VALID   <- compiler blind
        delete an unrelated function    VALID   <- compiler blind
        introduce a syntax error        INVALID
        no-op                           INVALID

    Three of five passed. `{ email: user.email }` -> `{ email: user.fullName }`
    typechecks perfectly because both are `string`. That is why the diff contract is
    mandatory rather than nice to have, and why it was designed against these
    measured failures instead of imagined ones.
    """
    from app.diff_contract import check
    from app.ts_codemod import remove_field

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    orig = open(os.path.join(root, "fixtures", "typescript-openapi", "remove-field",
                             "consumer", "src", "checkout.ts")).read()
    good = remove_field(orig, "phoneNumber").code

    # The correct fix passes, and only deletions happened.
    v = check(orig, good, "phoneNumber")
    assert v.ok, v.violations
    assert v.summary["added_lines"] == 0

    # The three the compiler could not see.
    unrelated_field = good.replace("    email: user.email,\n", "")
    wrong_property = good.replace("email: user.email", "email: user.fullName")
    dropped_function = good[:good.index("export function toCrmPayload")]
    for label, after, expect in [
        ("deleted an unrelated field", unrelated_field, "collateral damage"),
        ("changed the wrong property", wrong_property, "INSERTED text"),
        ("dropped a whole function", dropped_function, "collateral damage"),
        ("no-op", orig, "still present in CODE"),
        ("syntax error", good.replace("return {", "return {{"), "INSERTED text"),
        ("rewrote a comment", good.replace("A display string.", "Whatever."),
         "INSERTED text"),
    ]:
        d = check(orig, after, "phoneNumber")
        assert not d.ok, f"{label} was accepted by the diff contract"
        assert any(expect in x for x in d.violations), (label, d.violations)

    # An ADDITION is forbidden outright -- a removal never adds, and a patch that
    # adds a line produces a diff a reviewer cannot scan.
    with_addition = good.replace("export function formatContact",
                                 "// injected\nexport function formatContact")
    assert not check(orig, with_addition, "phoneNumber").ok


def test_keyed_property_with_a_side_effect_is_refused():
    """`phoneNumber: getPhone(),` must NOT be silently removed.

    The first version of the keyed-property rule deleted it, removing a CALL. Nothing
    else would have caught that: the compiler is happy, and the diff contract is
    satisfied because the deleted line DOES reference the field. Whether dropping the
    call is correct depends on what it does, which makes it judgment, not removal.
    """
    from app.ts_codemod import remove_field

    for src in ('const p = {\n  phoneNumber: getPhone(),\n};\n',
                'const p = {\n  phoneNumber: await fetchPhone(),\n};\n',
                'const p = {\n  phoneNumber: new Phone(),\n};\n',
                'const p = {\n  phoneNumber: () => 1,\n};\n'):
        r = remove_field(src, "phoneNumber")
        assert not r.edits, f"removed a side-effecting value: {src!r}"
        assert len(r.refusals) == 1, (src, r.refusals)   # exactly one reason
        assert r.code == src, "the code must be left alone"

    # Inert values stay removable -- the guard must not over-refuse.
    for src in ('const p = {\n  phoneNumber: "555",\n};\n',
                'interface U {\n  phoneNumber: string;\n}\n',
                'interface U {\n  phoneNumber?: string;\n}\n',
                'interface U {\n  phoneNumber: "a" | "b";\n}\n'):
        r = remove_field(src, "phoneNumber")
        assert len(r.edits) == 1 and not r.refusals, (src, r.refusals)


def test_every_historical_false_valid_stays_blocked():
    """The six fixes that were once called VALID must never be called VALID again.

    Two assertions, and the second is the one that stops this decaying into
    decoration:

      1. every case is blocked by the current stack, by the layer it declares;
      2. every case is GENUINELY historical -- replayed against a frozen copy of
         the deleted validator's logic, which must accept it.

    Without (2) the corpus can be padded with inputs that were never a problem, and
    the count grows while the safety boundary does not. Without the size floor the
    corpus can be emptied and still pass, which is the same defect one level up.

    Note what this does NOT assert: that `tsc` rejects all six. It does not.
    `known_bad_fix_003` keeps an unused function parameter, which is legal
    TypeScript, so the compiler returns VALID and the diff contract is the only
    thing standing between it and an automatically opened PR. A gate written around
    the compiler would ship it.
    """
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
    import audit_negative_corpus as neg

    assert len(neg.CORPUS) >= 7, \
        f"the negative corpus shrank to {len(neg.CORPUS)} -- entries are permanent"

    ids = [c["id"] for c in neg.CORPUS]
    assert len(set(ids)) == len(ids), f"duplicate case ids: {ids}"

    layers = {}
    for case in neg.CORPUS:
        provenance = case.get("provenance", neg.HISTORICAL)
        was_valid, err = neg._historical_validate(case["after"], case["language"])
        if provenance == neg.HISTORICAL:
            assert was_valid, (
                f"{case['id']}: the deleted validator REJECTED this ({err}), so it is "
                f"not one of the false VALIDs and does not belong in the corpus")
        else:
            # An OBSERVED entry has no old validator to replay against, so its
            # anti-padding evidence is that what the CURRENT toolchain says about it
            # is written down. A model failure tsc already rejects needs no memory
            # here -- production catches it.
            assert case.get("compiler_note"), \
                f"{case['id']} is OBSERVED but records no compiler_note"

        result = neg._run_stack(case)
        assert result["blocked"], f"{case['id']} ESCAPED: {result['detail']}"
        assert result["layer"] == case["blocked_by"], (
            f"{case['id']}: declared blocked_by={case['blocked_by']!r} but "
            f"{result['layer']!r} stopped it -- a layer changed behaviour and "
            f"another covered for it")
        layers[result["layer"]] = layers.get(result["layer"], 0) + 1

    # At least two DIFFERENT layers must be doing work. If every case collapsed onto
    # one layer, the corpus would stop being evidence that the stack has depth.
    assert len(layers) >= 2, f"all cases blocked by one layer only: {layers}"

    # The half-fix is the reason the diff layer exists. Losing it would leave the
    # corpus unable to show the compiler is insufficient.
    half = [c for c in neg.CORPUS if "half_fix" in c["id"]]
    assert half, "the half-fix case was removed -- it is the one tsc lets through"
    assert half[0]["blocked_by"] == "diff", half[0]["blocked_by"]

    # And at least one entry must be a REAL model failure rather than a replay.
    # Synthetic cases prove the layers work; an observed one proves they are needed.
    observed = [c for c in neg.CORPUS
                if c.get("provenance") == neg.OBSERVED]
    assert observed, \
        "no OBSERVED entry -- the corpus is entirely synthetic, so nothing in it " \
        "shows a real model producing a fix the compiler accepts"
    assert all(c["blocked_by"] == "diff" for c in observed), \
        "an observed model failure is blocked by something other than the diff " \
        "contract; if that is now true, say so deliberately"


def test_deployed_capability_is_reported_not_assumed():
    """The running image must state whether it can validate, and never overstate it.

    The gap this closes: the registry derives AUTO=1 from the code, while the
    deployed image is `python:3.11-slim` with no node, no npm and no docker daemon.
    Measured in that base image -- backend "", verdict UNABLE_TO_VALIDATE. So AUTO
    was simultaneously true in the repository and unreachable in production, and
    pushing the pending commits would not have changed it: an image problem wearing
    a deployment problem's clothes.

    Neither side could see it. The repo's audits run on a host that HAS docker; the
    deployed service was never asked. This is the same defect shape as a matcher
    that is built, tested and CI-gated but unreachable from production.
    """
    import asyncio

    from app import validation as val
    from app.webhook import health_capability

    original = val.choose_backend
    try:
        # No toolchain -- the production condition.
        val.choose_backend = lambda: ("", "no usable node and no docker")
        val._BACKEND_DESCRIPTION = None
        body = asyncio.run(health_capability())
        v = body["validation"]
        assert v["backend"] is None, v
        assert v["can_validate"] is False, \
            "an image with no node claimed it could validate"
        assert v["hint"], "a blocked image must say what is wrong, not just report False"

        # Toolchain present -- the hint must disappear rather than linger and mislead.
        val.choose_backend = lambda: ("docker", "container, pinned image")
        val._BACKEND_DESCRIPTION = None
        body = asyncio.run(health_capability())
        v = body["validation"]
        assert v["backend"] == "docker" and v["can_validate"] is True, v
        assert v["hint"] is None, "a working image still showed the failure hint"

        # The description is CACHED (choose_backend shells out with a 25s timeout,
        # which has no business on a health endpoint). Cached state that ignores the
        # reset is how a stale answer outlives the thing it described.
        val.choose_backend = lambda: ("", "changed after caching")
        body = asyncio.run(health_capability())
        assert body["validation"]["backend"] == "docker", \
            "the cache did not hold, so every health check pays a 25s docker probe"
        val._BACKEND_DESCRIPTION = None
        assert asyncio.run(health_capability())["validation"]["backend"] is None, \
            "the cache could not be reset, so the answer can never be corrected"
    finally:
        val.choose_backend = original
        val._BACKEND_DESCRIPTION = None


def test_safety_layers_are_reachable_or_declared_unreachable():
    """A safety layer that production cannot reach is not a safety layer.

    Found in Stage 6, in Stage 3's and Stage 4's own work. Stage 3 reported wiring
    the diff contract "into the pipeline so it gates AUTO"; Stage 4 built a corpus
    asserting no historical bad fix can reach AUTO. Both were true of a harness.
    `app/diff_contract.py` was imported by tests/ and tools/ and by nothing in app/.

    The exact cost, not a vague one: five of the six corpus cases are independently
    rejected by tsc, so production would catch them regardless. The sixth --
    known_bad_fix_003, the half-fix tsc accepts -- is blocked ONLY by the diff
    contract. The one case that justified building the layer is the one case the
    layer cannot catch where it matters.

    A REPORTING import must not count as wiring. app/webhook.py imports
    validation.describe_backend for the /health/capability endpoint, which can gate
    nothing; without that exemption this gate would have reported "2 of 3 layers
    wired" and hidden the gap it exists to expose.
    """
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
    import audit_safety_reachability as reach

    reachable = reach._reachable_from_entry()

    for name, spec in reach.LAYERS.items():
        assert (name in reachable) == spec["reachable"], (
            f"app/{name}.py is {'reachable' if name in reachable else 'unreachable'} "
            f"but declared {spec['reachable']} -- reachability and its declaration "
            f"diverged, in whichever direction")
        if not spec["reachable"]:
            assert spec["consequence"], \
                f"{name} is declared unreachable without stating what that costs"

    # The transformation must always be wired. If this ever fails, fixes are being
    # generated by something other than the codemod that refuses unsafe shapes.
    assert "ts_codemod" in reachable, \
        "the codemod is unreachable from production -- fixes come from elsewhere"

    # The reporting-only exemption is load-bearing, asserted rather than trusted.
    assert ("validation", "describe_backend") in reach.REPORTING_ONLY, \
        "the reporting-only exemption was removed; a health endpoint would now " \
        "make the validator look wired"

    # ALL FOUR layers are now in the request path. This assertion previously read
    # `"validation" not in reachable` with the note "if it is now genuinely wired,
    # update LAYERS and this assertion together, deliberately" -- and it fired when
    # the wiring landed, which is the gate working. Flipped deliberately, together
    # with LAYERS, not worked around.
    for name in ("ts_codemod", "diff_contract", "validation", "repo_workspace"):
        assert name in reachable, \
            f"{name} is NOT reachable from production -- a safety layer was " \
            f"disconnected, which is the regression this gate exists for"
    assert all(spec["reachable"] for spec in reach.LAYERS.values()), \
        "a layer is declared unreachable while all four are wired"

    # EVERY import form must be visible to the scanner. `from . import x` is an
    # ImportFrom whose node.module is None, and reading node.module skipped the
    # statement entirely -- so that form was INVISIBLE, in a scanner whose own entry
    # point (app/webhook.py) uses it. Found by mutation: wiring repo_workspace that
    # way did not fail the gate. A gate that cannot see a real import is worse than
    # no gate, because it reports "unreachable" with confidence.
    import ast
    import tempfile

    probe = tempfile.mkdtemp(prefix="ripple-imp-")
    try:
        for form in ("from . import ts_codemod",
                     "from . import ts_codemod as _t",
                     "from .ts_codemod import remove_field",
                     "from app.ts_codemod import remove_field",
                     "import app.ts_codemod"):
            path = os.path.join(probe, "probe.py")
            with open(path, "w") as fh:
                fh.write(form + "\n")
            ast.parse(form)                      # the form must be valid Python
            assert "ts_codemod" in reach._module_imports(path), \
                f"the scanner cannot see this import form: {form!r}"
    finally:
        import shutil as _sh
        _sh.rmtree(probe, ignore_errors=True)

    # repo_workspace must be REGISTERED even while unwired. It was built, tested and
    # CI-gated on the same day, imported by nothing -- the exact state diff_contract
    # sat in for three stages while a commit message claimed it was wired. Being
    # able to SEE the gap is the point.
    assert "repo_workspace" in reach.LAYERS, \
        "repo_workspace is not registered, so nothing reports that the tree fetch " \
        "is unreachable from production"


def test_a_partial_removal_returns_the_original_not_broken_code():
    """The diff contract now runs in the request path, so half-fixes never ship.

    Behaviour before this: a file with two removable references and two judgment
    references returned CHANGED code with the judgment references still in it. That
    compiles nowhere -- the type declaration is gone and the parameter still demands
    the field -- so it opened as REVIEW carrying a compile error for a human to
    discover. The residue was flagged in Stage 2 and belonged here.

    Now the diff contract sees `field still present in CODE`, the patch is refused
    outright, and the ORIGINAL is returned. A patch that changed something it should
    not have is worse than no patch, and unchanged code is already what
    apply_fix_template turns into a truthful "could not remove" and what the outcome
    derivation turns into BLOCKED.

    This is the real billing-api shape, which is why it is this shape.
    """
    from app.fix_templates import apply_fix_template, _LAST_CODEMOD_RESULT

    partial = (
        "interface CreateUserRequest {\n"
        "  name: string;\n"
        "  phoneNumber: string;\n"
        "}\n"
        "async function createUser(name: string, phoneNumber: string) {\n"
        "  const request: CreateUserRequest = { name, phoneNumber };\n"
        "  return request;\n"
        "}\n"
    )
    out, _explanation = apply_fix_template(
        code=partial, language="typescript", change_type="removed_field",
        field_name="phoneNumber")

    assert out == partial, \
        "a partial removal returned CHANGED code -- it would open a PR that cannot " \
        "compile, which is what wiring the diff contract was meant to stop"
    assert not (_LAST_CODEMOD_RESULT.get("edits") or []), \
        "edits were reported for a patch that was refused"
    assert any("diff contract" in str(r) for r in _LAST_CODEMOD_RESULT.get("refusals") or []), \
        "the refusal does not say the diff contract rejected it, so the PR body " \
        "could not explain why nothing happened"

    # And the complete case must be UNAFFECTED -- if this breaks, AUTO is lost.
    complete = (
        "interface U {\n  phoneNumber: string;\n}\n"
        "const p = { a: 1 };\n"
    )
    out2, _ = apply_fix_template(
        code=complete, language="typescript", change_type="removed_field",
        field_name="phoneNumber")
    assert out2 != complete and "phoneNumber" not in out2, \
        "the diff contract rejected a CORRECT complete removal -- it is now " \
        "over-refusing, which silently costs every fix"


def test_the_llm_branch_is_subject_to_the_diff_contract():
    """The diff contract must gate EVERY generator path, not just the template.

    It was wired inside fix_templates._remove_field_typescript, which the LLM branch
    never reaches -- _generate_with_llm returns its output directly and only falls
    back to a template on exception. So the deterministic generator was checked and
    the probabilistic one was not, which is backwards.

    The bad output below is the REAL thing, captured from a live gemini-flash-latest
    call asked to REMOVE phoneNumber: it added the field as a function parameter
    instead, which breaks every caller. `tsc --noEmit` returns VALID on it -- adding
    a parameter and using it is well-typed -- so the compiler cannot save us here and
    the diff contract is the only layer that objects. Preserved as
    known_bad_fix_007 in the negative corpus.

    Monkeypatched rather than calling a model, so this is deterministic and needs no
    network -- but the payload is not invented.
    """
    import inspect
    import tempfile

    from app import fix_generator as fg
    from app.consumer_finder import ConsumerMatch
    from app.diff_engine import BreakingChange

    def _mk(cls, **over):
        kw = {}
        for name, p in inspect.signature(cls).parameters.items():
            if name in over:
                kw[name] = over[name]
                continue
            if p.default is not inspect.Parameter.empty:
                continue
            ann = str(p.annotation)
            kw[name] = (0.9 if "float" in ann else
                        1 if "int" in ann else
                        [] if "list" in ann else "")
        return cls(**kw)

    original = (
        'import { User } from "./types";\n'
        "\n"
        "export function formatContact(user: User): string {\n"
        "  return `${user.fullName} <${user.email}> ${user.phoneNumber}`;\n"
        "}\n"
    )
    llm_bad = original.replace(
        "export function formatContact(user: User): string {",
        "export function formatContact(user: User, phoneNumber: string): string {"
    ).replace("> ${user.phoneNumber}`;", "> ${phoneNumber}`;")
    assert "phoneNumber: string" in llm_bad and llm_bad != original

    tmp = tempfile.mkdtemp(prefix="ripple-llmgate-")
    path = os.path.join(tmp, "checkout.ts")
    with open(path, "w") as fh:
        fh.write(original)

    consumer = _mk(ConsumerMatch, file_path=path, repo="billing-api",
                   language="typescript", confidence="high")
    change = _mk(BreakingChange, change_type="removed_field",
                 field_name="phoneNumber", severity="breaking",
                 description="phoneNumber removed from User")

    saved_llm = fg._generate_with_llm
    saved_key = None
    try:
        from app import llm_config
        saved_key = llm_config.api_key
        llm_config.api_key = lambda: "DUMMY"          # take the LLM branch
        fg._generate_with_llm = lambda *_a, **_k: (llm_bad, "llm said so")

        assert fg.generate_fix(consumer, change, use_llm=True) is None, (
            "the LLM branch produced a fix that ADDS a parameter while claiming to "
            "remove a field, and it was accepted -- the diff contract is not gating "
            "this path")

        # A CORRECT llm output must still pass, or the gate is simply refusing
        # everything and proves nothing.
        good = original.replace(" ${user.phoneNumber}", "")
        fg._generate_with_llm = lambda *_a, **_k: (good, "llm said so")
        ok = fg.generate_fix(consumer, change, use_llm=True)
        assert ok is not None and "user.phoneNumber" not in ok.fixed_code, \
            "a correct LLM removal was rejected -- the gate over-refuses"
    finally:
        fg._generate_with_llm = saved_llm
        if saved_key is not None:
            from app import llm_config as _lc
            _lc.api_key = saved_key
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)


def test_jsx_attribute_is_removed_but_a_parameter_list_is_never_touched():
    """React consumers dominate real TypeScript, so the attribute shape matters most.

    `<Row phone={user.phoneNumber} />` -> `<Row />`. Same reasoning as the
    object-literal property: the field no longer exists upstream, so passing it
    conveys nothing. If the prop is REQUIRED, `tsc` reports it and the validator
    blocks the fix -- that decision belongs to the compiler, not a regex.

    THE PART THAT NEEDED MEASURING, not assuming. Two guards protect this: the
    pattern requires the braces to hold exactly a member chain, and
    _inside_jsx_tag() scans back for the opening `<`. I assumed the pattern was
    load-bearing. It is not -- loosening it alone changes nothing, because the scan
    rejects a default parameter on hitting `(`. Removing the SCAN is what does
    damage:

        function f(opts={user: user.phoneNumber}) { return opts; }
          ->  function f() { return opts; }

    a destroyed signature with the body still using the parameter. So this test
    pins the scan, and the corpus case default-parameter-object-value fails the
    coverage gate if it is ever deleted.
    """
    from app.ts_codemod import remove_field

    # Shapes that must be removed, including the two the first implementation
    # REFUSED because the backward scan treated a preceding attribute's `}` as the
    # end of the tag.
    for label, src, expected in (
        ("single line", 'const el = <Row phone={user.phoneNumber} />;\n',
         "const el = <Row />;\n"),
        ("optional chain", 'const el = <Row phone={user?.phoneNumber} />;\n',
         "const el = <Row />;\n"),
        ("among siblings", 'const el = <Row a={x} phone={user.phoneNumber} b={y} />;\n',
         "const el = <Row a={x} b={y} />;\n"),
        # `>` inside a sibling's arrow function is not the end of the tag.
        ("after an arrow sibling",
         'const el = <Row onClick={() => f()} phone={user.phoneNumber} />;\n',
         "const el = <Row onClick={() => f()} />;\n"),
        # A quoted value may contain `>` too.
        ("after a quoted sibling",
         'const el = <Row title="a>b" phone={user.phoneNumber} />;\n',
         'const el = <Row title="a>b" />;\n'),
    ):
        r = remove_field(src, "phoneNumber")
        assert len(r.edits) == 1 and not r.refusals, (label, r.refusals)
        assert r.edits[0]["shape"] == "JSX attribute", (label, r.edits)
        assert r.code == expected, (label, repr(r.code))

    # Alone on its line: the LINE goes, not just the attribute, or a blank line is
    # left behind and the diff stops being scannable.
    multi = ('const el = (\n  <Row\n    name={user.fullName}\n'
             '    phone={user.phoneNumber}\n  />\n);\n')
    r = remove_field(multi, "phoneNumber")
    assert len(r.edits) == 1 and not r.refusals, r.refusals
    assert r.code == ('const el = (\n  <Row\n    name={user.fullName}\n  />\n);\n'), \
        repr(r.code)
    assert "\n\n" not in r.code, "a blank line was left where the attribute was"

    # A PARAMETER LIST IS NOT AN ATTRIBUTE LIST. These must never be edited.
    for src in ('function f(opts={user: user.phoneNumber}) {\n  return opts;\n}\n',
                'function f(phone=user.phoneNumber) {\n  return phone;\n}\n',
                'const o = { phone: user.phoneNumber };\n'):
        r = remove_field(src, "phoneNumber")
        assert not any(e["shape"] == "JSX attribute" for e in r.edits), \
            f"the JSX rule matched outside a tag: {src!r} -> {r.code!r}"

    # And the output must satisfy the diff contract, which now runs in production.
    from app.diff_contract import check
    for src in ('const el = <Row phone={user.phoneNumber} />;\n', multi,
                'const el = <Row a={x} phone={user.phoneNumber} b={y} />;\n'):
        r = remove_field(src, "phoneNumber")
        verdict = check(src, r.code, "phoneNumber")
        assert verdict.ok, (src, verdict.violations)


def test_python_regions_and_a_language_aware_diff_contract():
    """The diff contract now covers Python, and the language parameter is load-bearing.

    It was TS/JS-only because the scanner knew `//` and `/* */` but not `#`. Scanning
    Python with those rules means a stale `# phone_number is gone` comment is not a
    comment at all -- it reads as CODE, the "field still present in CODE" rule fires,
    and a CORRECT fix is REJECTED. That is asserted below in both directions, because
    a language parameter nothing depends on is decoration.

    F-STRINGS ARE THE HARD PART, and they are the exact analogue of TS template
    literals: the text is string content, `{...}` holds real code.

        f"phone_number={user.phone_number}"
          ^^^^^^^^^^^^ string (a NOTE)     ^^^^^^^^^^^^^^^^^ code (must be fixed)

    Getting that backwards fails silently in one direction (the fix never happens)
    and destructively in the other (a customer's log message is rewritten).
    """
    import re

    from app.diff_contract import check
    from app.source_regions import SCANNED, regions

    def kinds(src, field="phone_number"):
        spans = regions(src, "python")
        return [next((k for s, e, k in spans if s <= m.start() < e), "CODE")
                for m in re.finditer(rf"\b{field}\b", src)]

    for label, src, expected in (
        ("hash comment", "# phone_number is gone\nx = 1\n", ["comment"]),
        ("member access", "p = user.phone_number\n", ["CODE"]),
        ("plain string", 'log("phone_number gone")\n', ["string"]),
        ("docstring", 'def f():\n    """phone_number removed."""\n    return 1\n',
         ["string"]),
        ("triple single", "x = '''phone_number'''\n", ["string"]),
        ("f-string text", 'msg = f"phone_number missing"\n', ["string"]),
        ("f-string interpolation", 'msg = f"{user.phone_number}"\n', ["CODE"]),
        # One line, BOTH position classes -- the case that cannot be expressed by a
        # rule as coarse as `if field in line`.
        ("f-string both", 'msg = f"phone_number={user.phone_number}"\n',
         ["string", "CODE"]),
        # `{{` is a literal brace, not an interpolation. Reading it as one would put
        # the following text in a code span.
        ("escaped braces", 'msg = f"{{phone_number}} {user.phone_number}"\n',
         ["string", "CODE"]),
        ("raw string", "p = r'phone_number\\d'\n", ["string"]),
        ("rf-string", 'm = rf"a{user.phone_number}"\n', ["CODE"]),
        # `format_f` must not be read as an `f` prefix on the following quote.
        ("not a prefix", "format_f = user.phone_number\n", ["CODE"]),
    ):
        assert kinds(src) == expected, (label, kinds(src), expected)

    # THE LOAD-BEARING ASSERTION. A correct Python fix that leaves a stale comment
    # passes as Python and is WRONGLY REJECTED as TypeScript.
    before = ("# phone_number was removed upstream\n"
              "class User:\n    name: str\n    phone_number: str\n")
    after = "# phone_number was removed upstream\nclass User:\n    name: str\n"
    assert check(before, after, "phone_number", language="python").ok, \
        "a correct Python removal was rejected with the Python scanner"
    assert not check(before, after, "phone_number", language="typescript").ok, \
        "the language parameter changes nothing -- scanning Python as TypeScript " \
        "should misread the `#` comment as code, so either the scanner regressed " \
        "or this check is not consulting it"

    # And it must have TEETH on Python, not merely accept everything.
    src = ('# keep this note\n'
           'def build(user):\n'
           '    payload = {}\n'
           '    payload["email"] = user.email\n'
           '    payload["phone"] = user.phone_number\n'
           '    return payload\n')
    good = src.replace('    payload["phone"] = user.phone_number\n', "")
    assert check(src, good, "phone_number", language="python").ok, "correct fix"
    for label, bad in (
        ("collateral deletion",
         good.replace('    payload["email"] = user.email\n', "")),
        ("partial removal",
         src.replace('    payload["phone"] = user.phone_number\n',
                     "    phone = user.phone_number\n")),
        ("rewrote the comment",
         good.replace("# keep this note", "# phone_number gone")),
        ("no-op", src),
    ):
        assert not check(src, bad, "phone_number", language="python").ok, \
            f"the Python diff contract rubber-stamped: {label}"

    # The production gate keys off SCANNED, so a language can never be admitted
    # without a scanner -- adding one is the single edit that widens coverage.
    assert "python" in SCANNED and "typescript" in SCANNED, SCANNED
    src_gate = open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "app", "fix_generator.py")).read()
    assert "lang in _SCANNED" in src_gate, \
        "fix_generator no longer gates on source_regions.SCANNED, so a language " \
        "with no scanner can reach the diff contract and be misjudged"


def test_llm_is_briefed_per_operation_and_never_given_contradictory_orders():
    """The prompt must describe the operation being performed.

    There was ONE hardcoded instruction block, used for every change_type:

        1. Add the new required field "{field}" to the API call.
        2. Add it as a parameter/argument that callers must provide.

    with no branching. So a REMOVAL asked the model to ADD the field. Measured on a
    live gemini-flash-latest call: asked to remove `phoneNumber`, it added
    `phoneNumber: string` to two signatures and explained itself as "Added required
    field" -- doing exactly what it was told. Only the diff contract stopped it, and
    it read as model unreliability for a day. It was the prompt.

    The table is an ALLOWLIST. An unlisted operation gets NO LLM attempt and returns
    to the deterministic path, because the previous shape was the "unknown enum falls
    through to the weakest path" defect: every unrecognised operation silently
    inherited add-a-required-field.
    """
    from app.diff_engine import BreakingChange
    from app.fix_generator import _llm_explanation, _llm_instructions

    def change(**kw):
        base = dict(change_type="", path="/users", method="get", field_name="phone",
                    field_type="string", location="request_body", severity="breaking",
                    description="")
        base.update(kw)
        return BreakingChange(**base)

    # Each briefed operation must be told to do THAT operation.
    for change_type, must_contain, must_not in (
        ("removed_field", "REMOVE every reference", "Add it as a parameter"),
        ("added_required_field", "now REQUIRED", "REMOVE every reference"),
        ("renamed_field", "was RENAMED", "REMOVE every reference"),
        ("field_type_changed", "changed type", "REMOVE every reference"),
    ):
        kw = {"change_type": change_type}
        if change_type == "renamed_field":
            kw["new_name"] = "phone_no"
        if change_type == "field_type_changed":
            kw["new_type"] = "number"
        text = _llm_instructions(change(**kw))
        assert text, change_type
        assert must_contain in text, (change_type, text[:120])
        assert must_not not in text, \
            f"{change_type} inherited instructions for a different operation"

    # JUDGMENT operations are absent by design -- REVIEW is the correct answer for
    # them, not a prompt. The first four are the dialects engines actually emit; the
    # last two exercise the suffix FALLBACK, where `removed_package` used to degrade
    # to `remove_field` and would have been briefed as a field removal.
    for change_type in ("removed_operation", "package_removed", "spec_removed",
                        "directory_removed", "removed_package", "restrict_schema"):
        assert _llm_instructions(change(change_type=change_type)) == "", \
            f"{change_type} is being briefed to the LLM; it is a judgment call"

    # An unknown operation must get NOTHING rather than the nearest match.
    assert _llm_instructions(change(change_type="%%nonsense%%")) == "", \
        "an unrecognised change_type received instructions -- the allowlist leaks"

    # Instructions that interpolate a field must refuse when it is empty, or the
    # model is asked to rename something to "" and will invent a plausible answer.
    assert _llm_instructions(change(change_type="renamed_field")) == "", \
        "a rename with no new_name was briefed anyway"
    assert _llm_instructions(change(change_type="field_type_changed")) == "", \
        "a type change with no new_type was briefed anyway"

    # The EXPLANATION is what a customer reads in the PR body, and it was hardcoded
    # to "Added required field" for every operation -- so a removal PR announced
    # itself as an addition.
    assert "Removed references" in _llm_explanation(change(change_type="removed_field"))
    assert "Added" in _llm_explanation(change(change_type="added_required_field"))
    assert "Renamed" in _llm_explanation(
        change(change_type="renamed_field", new_name="phone_no"))
    assert "Added" not in _llm_explanation(change(change_type="removed_field")), \
        "a removal is still described as an addition"


def test_llm_output_keeps_the_files_trailing_newline():
    """`.strip()` on the response dropped the final newline, and the contract noticed.

    An otherwise CORRECT live fix was rejected with "REMOVED text that does not
    reference 'phoneNumber': '\\n'". The contract was right -- losing the trailing
    newline puts "\\ No newline at end of file" in the diff, which is an unrelated
    change. The loss was ours, not the model's, and I attributed it to the model
    first.

    Normalising the generator's OUTPUT is the fix. Relaxing the verifier to ignore
    trailing whitespace would have hidden a real class of unrelated change.
    """
    import inspect
    import tempfile

    from app import fix_generator as fg
    from app.consumer_finder import ConsumerMatch
    from app.diff_engine import BreakingChange
    from app.diff_contract import check

    def _mk(cls, **over):
        kw = {}
        for name, p in inspect.signature(cls).parameters.items():
            if name in over:
                kw[name] = over[name]
                continue
            if p.default is not inspect.Parameter.empty:
                continue
            ann = str(p.annotation)
            kw[name] = (0.9 if "float" in ann else 1 if "int" in ann
                        else [] if "list" in ann else "")
        return cls(**kw)

    original = ('import { User } from "./types";\n'
                "\n"
                "export function f(user: User): string {\n"
                "  return `${user.fullName} ${user.phoneNumber}`;\n"
                "}\n")
    # A correct removal that has LOST the trailing newline, which is what .strip()
    # used to produce.
    stripped = original.replace(" ${user.phoneNumber}", "").rstrip("\n")

    tmp = tempfile.mkdtemp(prefix="ripple-nl-")
    path = os.path.join(tmp, "f.ts")
    with open(path, "w") as fh:
        fh.write(original)

    consumer = _mk(ConsumerMatch, file_path=path, repo="r", language="typescript",
                   confidence="high")
    ch = _mk(BreakingChange, change_type="removed_field", field_name="phoneNumber",
             method="get", path="/users", field_type="string", severity="breaking")

    # Without restoration the contract rejects it -- proving the rule has teeth and
    # that the restoration below is doing real work rather than being cosmetic.
    assert not check(original, stripped, "phoneNumber",
                     language="typescript").ok, \
        "losing the trailing newline no longer violates the contract, so this " \
        "normalisation is untested"

    saved = fg._generate_with_llm
    try:
        from app import llm_config
        saved_key = llm_config.api_key
        llm_config.api_key = lambda: "DUMMY"
        # The generator hands back the stripped form; generate_fix must still produce
        # a patch the contract accepts.
        fg._generate_with_llm = lambda *_a, **_k: (stripped, "removed")
        got = fg.generate_fix(consumer, ch, use_llm=True)
        assert got is not None, \
            "a correct fix was refused only because the trailing newline was lost"
        assert got.fixed_code.endswith("\n"), got.fixed_code[-20:]
    finally:
        fg._generate_with_llm = saved
        from app import llm_config as _lc
        _lc.api_key = saved_key
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)


def test_repo_archive_extraction_is_contained_and_capped():
    """Extraction takes an archive built by whoever owns the repo. Untrusted input.

    Stage 1 replaced per-file `contents/` fetches with a whole-tree fetch, because a
    compiler needs a PROJECT and a file in isolation typechecks nothing. The cost of
    that unlock is that we now extract someone else's archive, and the classic
    attacks are not theoretical.

    THE INVARIANT IS CONTAINMENT, NOT REFUSAL. Two hostile shapes are ACCEPTED and
    still safe, which is why asserting "it refused" would assert the wrong thing:

        absolute member path   data_filter STRIPS the leading slash, so `/tmp/x`
                               lands inside the tree as `tmp/x`
        symlink member         _extract skips every non-regular member, so the link
                               is never created and there is nothing to escape through

    Sizes and counts are ours, because a filter cannot know our budget.
    """
    import io
    import tarfile
    import tempfile

    from app.repo_workspace import Limits, RepoTooLarge, WorkspaceError, _extract

    small = Limits(download_bytes=1 << 20, extracted_bytes=2 << 20, files=50,
                   file_bytes=1 << 19, timeout_seconds=5)

    def build(path, members):
        with tarfile.open(path, "w:gz") as tar:
            for name, data, kind in members:
                info = tarfile.TarInfo(name)
                if kind == "link":
                    info.type, info.linkname = tarfile.SYMTYPE, "/tmp"
                    tar.addfile(info)
                    continue
                if kind == "fifo":
                    info.type = tarfile.FIFOTYPE
                    tar.addfile(info)
                    continue
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

    blank = b"\0" * (1 << 18)
    cases = [
        ("traversal", [("../../tmp/ripple-t", b"x", "f")], "refuse"),
        ("absolute sanitised", [("/tmp/ripple-a", b"x", "f")], "accept"),
        ("symlink skipped", [("escape", b"", "link"),
                             ("escape/ripple-l", b"x", "f")], "accept"),
        ("bomb", [(f"b/{i}.bin", blank, "f") for i in range(12)], "refuse"),
        ("too many files", [(f"m/{i}", b"", "f") for i in range(60)], "refuse"),
        ("file over cap", [("big", b"\0" * ((1 << 19) + 1), "f")], "refuse"),
        ("fifo skipped", [("a.fifo", b"", "fifo"), ("ok", b"y", "f")], "accept"),
        ("normal repo", [("r-abc/package.json", b"{}", "f")], "accept"),
    ]

    tmp = tempfile.mkdtemp(prefix="ripple-arch-")
    try:
        for label, members, expected in cases:
            arch = os.path.join(tmp, f"{label.replace(' ', '_')}.tar.gz")
            build(arch, members)
            into = tempfile.mkdtemp(dir=tmp)
            try:
                _extract(arch, into, small)
                got = "accept"
            except (RepoTooLarge, WorkspaceError):
                got = "refuse"
            except Exception:                       # noqa: BLE001
                got = "refuse"                      # the filter's own errors count
            assert got == expected, f"{label}: expected {expected}, got {got}"

            # Containment, for every case including the accepted ones.
            real_into = os.path.realpath(into)
            for base, dirs, names in os.walk(into):
                for name in names:
                    full = os.path.realpath(os.path.join(base, name))
                    assert full.startswith(real_into + os.sep), \
                        f"{label}: escaped the tree -- {full}"
                for entry in dirs + names:
                    assert not os.path.islink(os.path.join(base, entry)), \
                        f"{label}: a symlink was created -- {entry}"

            for probe in ("/tmp/ripple-t", "/tmp/ripple-a", "/tmp/ripple-l"):
                assert not os.path.exists(probe), \
                    f"{label}: wrote outside the tree -- {probe}"
    finally:
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)


def test_an_extracted_tree_is_a_project_a_compiler_can_read():
    """The reason cloning exists: a tree typechecks, a single file does not.

    Asserts the GitHub archive SHAPE is handled -- one `{owner}-{repo}-{sha}` wrapper
    directory. Returning the temp root instead would put every relative path one
    level off, and a tsconfig lookup would silently find nothing, which reads as
    "this repo has no TypeScript project" rather than as a bug here.

    The compiler half needs a validation backend and SKIPS without one, but the shape
    assertions always run.
    """
    import tarfile
    import tempfile

    from app.repo_workspace import Limits, _extract, _single_root
    from app.validation import choose_backend

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fixture = os.path.join(root, "fixtures", "typescript-openapi", "remove-field",
                           "consumer")
    tmp = tempfile.mkdtemp(prefix="ripple-tree-")
    try:
        arch = os.path.join(tmp, "r.tar.gz")
        with tarfile.open(arch, "w:gz") as tar:
            for base, dirs, names in os.walk(fixture):
                dirs[:] = [d for d in dirs if d not in ("node_modules", ".git")]
                for name in names:
                    full = os.path.join(base, name)
                    tar.add(full, arcname=os.path.join(
                        "acme-billing-abc1234", os.path.relpath(full, fixture)))

        into = os.path.join(tmp, "tree")
        os.makedirs(into)
        files, _written = _extract(arch, into, Limits())
        assert files >= 3, files

        tree = _single_root(into)
        assert os.path.basename(tree) == "acme-billing-abc1234", tree
        for required in ("tsconfig.json", "package.json"):
            assert os.path.exists(os.path.join(tree, required)), \
                f"{required} missing from the extracted tree, so tsc cannot resolve " \
                f"the project -- the single-root unwrap is wrong"

        backend, _note = choose_backend()
        if not backend:
            print("      SKIP: no validation backend for the compiler half")
            return

        from app.fix_templates import apply_fix_template
        from app.validation import validate

        # Unfixed, the tree must be INVALID -- proof the compiler is really seeing
        # the project rather than an empty directory.
        assert validate("typescript", tree).state.value == "INVALID", \
            "the unfixed tree typechecked, so the compiler is not seeing the project"

        target = os.path.join(tree, "src", "checkout.ts")
        with open(target) as fh:
            before = fh.read()
        fixed, _expl = apply_fix_template(
            code=before, language="typescript", change_type="removed_field",
            field_name="phoneNumber")
        assert fixed != before
        with open(target, "w") as fh:
            fh.write(fixed)

        assert validate("typescript", tree).state.value == "VALID", \
            "the fix did not typecheck against the extracted tree"
    finally:
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)


def test_project_resolution_never_falls_back_to_the_repo_root():
    """A monorepo is a repo where "which project owns this file" is not "the root".

    Getting that answer right IS the monorepo feature; workspace manifests and project
    references are refinements on top of it.

    WHY THE ROOT IS ALWAYS THE WRONG FALLBACK. `tsc` at a monorepo root either
    EXCLUDES the changed file -- so a broken fix validates clean -- or INCLUDES
    thousands of unrelated ones and reports errors the fix never caused. Both are
    confident verdicts about the wrong thing, which is worse than admitting we cannot
    validate. So an unowned file resolves to None and the caller degrades to REVIEW.

    THREE SHAPES THAT BREAK "nearest manifest wins", all real:

        hoisted workspace   tsconfig in the package, package.json at the root.
                            app/validation.py wants both in ONE directory, so this
                            silently becomes UNABLE_TO_VALIDATE unless it is reported
        polyglot repo       a .ts file under a go.mod -- resolving by nearest manifest
                            alone hands a TypeScript file to a Go project
        no owning project   a loose file with no config above it
    """
    import tempfile

    from app.project_resolution import group, resolve

    tree = tempfile.mkdtemp(prefix="ripple-resolve-")

    def w(rel, body="{}"):
        path = os.path.join(tree, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(body)

    try:
        w("flat/package.json"); w("flat/tsconfig.json"); w("flat/src/a.ts", "x")
        w("mono/package.json"); w("mono/tsconfig.base.json")
        w("mono/packages/api/package.json"); w("mono/packages/api/tsconfig.json")
        w("mono/packages/api/src/user.ts", "x")
        w("mono/packages/web/package.json"); w("mono/packages/web/tsconfig.json")
        w("mono/packages/web/src/page.tsx", "x")
        # A REAL workspace root declares `workspaces`. Without it this is just a
        # package.json, and the install root is found by the fallback path -- which
        # is a different code path and a weaker assertion.
        w("hoist/package.json", '{"workspaces": ["packages/*"]}')
        w("hoist/packages/api/tsconfig.json")
        w("hoist/packages/api/src/user.ts", "x")
        w("poly/go.mod", "module x\n"); w("poly/scripts/tool.ts", "x")
        w("loose/src/orphan.ts", "x")
        w("nested/package.json"); w("nested/tsconfig.json")
        w("nested/inner/package.json"); w("nested/inner/tsconfig.json")
        w("nested/inner/src/deep.ts", "x")
        w("py/pyproject.toml", "[project]\n"); w("py/pkg/mod.py", "x = 1\n")

        for label, rel, want in (
            ("flat", "flat/src/a.ts", "flat"),
            ("monorepo package", "mono/packages/api/src/user.ts",
             "mono/packages/api"),
            ("sibling not chosen", "mono/packages/web/src/page.tsx",
             "mono/packages/web"),
            ("hoisted", "hoist/packages/api/src/user.ts", "hoist/packages/api"),
            ("nested inner wins", "nested/inner/src/deep.ts", "nested/inner"),
            ("python", "py/pkg/mod.py", "py"),
        ):
            project = resolve(tree, rel)
            assert project is not None, f"{label}: resolved to nothing"
            assert project.rel_root == want, (label, project.rel_root, want)

        # A .ts file under a go.mod must NOT become a Go project.
        assert resolve(tree, "poly/scripts/tool.ts") is None, \
            "a TypeScript file resolved against a Go module -- resolution is not " \
            "language-driven"
        # And an unowned file must NOT fall back to the tree root.
        assert resolve(tree, "loose/src/orphan.ts") is None, \
            "an unowned file fell back to a project; the root is never the answer"

        # The hoisted split must be VISIBLE. app/validation.py requires package.json
        # and tsconfig.json in one directory, so a caller that cannot see the split
        # meets it later as an unexplained UNABLE_TO_VALIDATE.
        hoisted = resolve(tree, "hoist/packages/api/src/user.ts")
        assert hoisted.deps_root and \
            os.path.normpath(hoisted.deps_root) != os.path.normpath(hoisted.root), \
            "the hoisted workspace was not reported as hoisted"
        # Assert the PROPERTY, not the prose. The reason text names which mechanism
        # found the install root (workspace marker vs nearest manifest) and changed
        # when workspace detection was added -- an assertion on wording fails for
        # the wrong reason.
        assert "dependencies resolve from" in hoisted.reason, hoisted.reason
        assert "workspaces" in hoisted.reason or "workspace root" in hoisted.reason, \
            f"the install root was not identified as a workspace: {hoisted.reason}"
        assert hoisted.as_detail()["deps_root_differs"] is True

        # A self-contained package must NOT be flagged as hoisted.
        contained = resolve(tree, "mono/packages/api/src/user.ts")
        assert contained.as_detail()["deps_root_differs"] is False, contained.reason

        # Grouping keeps packages APART -- one change touching two packages must be
        # validated twice, in two projects. Collapsing them is the bug.
        grouped, unresolved = group(tree, [
            "mono/packages/api/src/user.ts",
            "mono/packages/web/src/page.tsx",
            "loose/src/orphan.ts",
        ])
        assert len(grouped) == 2, f"two packages collapsed into {len(grouped)}"
        assert unresolved == ["loose/src/orphan.ts"], unresolved

        # Resolution must never read above the tree, whatever the path claims.
        assert resolve(tree, "../../../etc/passwd") is None
    finally:
        import shutil as _sh
        _sh.rmtree(tree, ignore_errors=True)


def test_a_hoisted_workspace_validates_at_the_package_not_the_root():
    """pnpm/yarn workspaces put tsconfig in the package and node_modules at the root.

    The validator required both manifests in ONE directory, so Stage 2 measured this:
    resolution correctly returned packages/api, and validate() then answered
    "package.json is missing" -- the most common real monorepo layout was
    UNABLE_TO_VALIDATE.

    `workspace` is now where dependencies install and `project_subdir` is the path to
    the compiler config. That is all it takes, because node's own resolution walks UP
    from a file looking for node_modules.

    THE SECOND ASSERTION IS THE IMPORTANT ONE. A SIBLING package contains a
    deliberate type error. If the correct target came back INVALID, we would be
    typechecking the whole workspace rather than the changed project -- which is the
    failure the root tsconfig causes, and it would report errors from code the fix
    never touched.

    Needs a validation backend and SKIPS without one.
    """
    import json as _json
    import tempfile

    from app.project_resolution import resolve
    from app.validation import choose_backend, validate

    backend, note = choose_backend()
    if not backend:
        print(f"      SKIP: no validation backend ({note})")
        return

    def build(body):
        tree = tempfile.mkdtemp(prefix="ripple-hoisted-")

        def w(rel, text):
            path = os.path.join(tree, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(text)

        w("package.json", _json.dumps({
            "name": "ws", "private": True, "workspaces": ["packages/*"],
            "devDependencies": {"typescript": "5.3.3"}}))
        w("packages/api/tsconfig.json", _json.dumps({
            "compilerOptions": {"strict": True, "noEmit": True},
            "include": ["src"]}))
        w("packages/api/src/user.ts", body)
        # A sibling with a deliberate error. It must NOT affect the target's verdict.
        w("packages/web/tsconfig.json", _json.dumps({
            "compilerOptions": {"strict": True}, "include": ["src"]}))
        w("packages/web/src/broken.ts", "export const b: string = 42;\n")
        return tree

    for label, body, want in (
        ("broken target", "export const u: string = 1;\n", "INVALID"),
        ("correct target", 'export const u: string = "ok";\n', "VALID"),
    ):
        tree = build(body)
        try:
            project = resolve(tree, "packages/api/src/user.ts")
            assert project is not None and project.deps_root, project
            subdir = os.path.relpath(project.root, project.deps_root)
            assert subdir == os.path.join("packages", "api"), subdir

            verdict = validate("typescript", project.deps_root,
                               project_subdir=subdir)
            assert verdict.state.value == want, (
                f"{label}: got {verdict.state.value}, wanted {want} -- "
                f"{verdict.reason[:120]}")
            if want == "INVALID":
                assert any("packages/api" in e for e in verdict.errors), \
                    f"errors do not name the target project: {verdict.errors[:2]}"
                assert not any("packages/web" in e for e in verdict.errors), \
                    "the sibling package appeared in the errors -- the whole " \
                    "workspace is being typechecked, not the changed project"
        finally:
            import shutil as _sh
            _sh.rmtree(tree, ignore_errors=True)


def test_the_production_image_does_not_pay_for_a_gpu_it_does_not_have():
    """requirements.txt is the production image, and it was 94% CUDA.

    Measured in the real image rather than estimated:

        with    sentence-transformers + chromadb   site-packages 5.4 GB
                nvidia 2.7G  torch 1.2G  triton 691M     = 4.6 GB of GPU stack
        without                                    site-packages 348 MB

    On a container with no GPU, to serve a RAG store holding zero patterns. That
    layer is rebuilt on every Railway deploy and it is the only build step large
    enough to fail on time or disk.

    `chromadb` was pinned and imported NOWHERE -- the sole occurrence in the tree
    is a comment.

    THE SECOND ASSERTION IS THE ONE THAT MATTERS. Dropping sentence-transformers
    alone would fall through TWO tiers of Embedder.__init__ to bag-of-words, and
    the guarded `except (ImportError, Exception)` would make that invisible --
    exactly the fail-silent shape this repo keeps rediscovering. scikit-learn must
    be present so the degradation is one honest step, not a silent collapse.

    This is a gate, not a comment. A verbal decision not to reinstall a 5 GB
    dependency lasts about four days.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "requirements.txt")) as fh:
        pinned = {
            line.split("=")[0].split(">")[0].split("<")[0].strip().lower()
            for line in fh
            if line.strip() and not line.lstrip().startswith("#")
        }

    # 1. the GPU stack and the phantom dependency stay out
    for banned, why in (
        ("sentence-transformers", "drags torch + nvidia + triton = 4.6 GB"),
        ("chromadb", "imported nowhere in the tree"),
        ("torch", "no GPU in the container, and nothing imports it directly"),
    ):
        assert banned not in pinned, (
            f"{banned} is back in requirements.txt ({why}). If this is "
            f"deliberate, measure the image first: it was 5.4 GB with it and "
            f"348 MB without, and Railway rebuilds that layer every deploy."
        )

    # 2. the fallback tier is real, so removing tier 1 costs ONE step not two
    assert "scikit-learn" in pinned, (
        "scikit-learn is missing, so Embedder falls past the TF-IDF tier to "
        "bag-of-words -- and the guarded except makes that silent."
    )

    # 3. and the tiers themselves still exist, so this test keeps meaning something
    rag = os.path.join(root, "app", "rag_engine.py")
    with open(rag) as fh:
        body = fh.read()
    for tier in ("sentence_transformers", "TfidfVectorizer", "_bow_embed"):
        assert tier in body, (
            f"Embedder no longer references {tier} -- the three-tier fallback "
            f"this test protects has changed shape, so re-derive it."
        )


def test_a_consumer_tree_is_never_fetched_at_the_spec_repos_sha():
    """The first live end-to-end run failed here, and the log accused the wrong thing.

        tree_unavailable  billing-api
          "HTTP 404 fetching the archive -- the token cannot read this repository"

    webhook.py passed `after_sha` -- a commit in the SPEC repository -- as the git
    ref for the CONSUMER repository's tarball. Measured against the real API:

        ref=HEAD        200
        ref=main        200
        ref=8b7c869     404      <- the spec repo's commit
        ref=deadbeef    404      <- indistinguishable from a nonexistent ref

    So validation could not run in production for ANY repository, whatever the
    registry derived -- and the contents API had read the same file seconds earlier
    with the same token, which is what makes the "token" wording a false lead.

    Asserted structurally because the alternative is a live network call. The call
    site must not pass a spec-repo SHA; the consumer's own default-branch HEAD is
    the only ref that means anything here, because that is the tree the PR targets.
    """
    import ast as _ast
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "app", "webhook.py")) as fh:
        tree = _ast.parse(fh.read())

    calls = [
        node for node in _ast.walk(tree)
        if isinstance(node, _ast.Call)
        and isinstance(node.func, _ast.Name)
        and node.func.id == "_fetch_consumer_tree"
    ]
    assert calls, "_fetch_consumer_tree is no longer called -- re-derive this test"

    banned = {"after_sha", "before_sha", "base_sha", "commit_sha", "sha"}
    for call in calls:
        passed = [a.id for a in call.args if isinstance(a, _ast.Name)]
        leaked = banned.intersection(passed)
        assert not leaked, (
            f"_fetch_consumer_tree is being passed {sorted(leaked)} -- that is a "
            f"commit in the SPEC repository and does not exist in the consumer's, "
            f"so GitHub 404s the archive and nothing can be validated."
        )


def test_a_404_on_the_archive_is_not_reported_as_an_auth_failure():
    """One string covered 401, 403 and 404, and it sent me to the wrong place.

    A 404 on an archive means the repository or the REF is absent. 401/403 mean
    auth. Collapsing them produced "the token cannot read this repository" for a
    ref that simply did not exist -- so the first thing I checked was the App's
    repository permissions, which were fine.

    Same family as the 404-vs-403 caching bug this repo has now hit five times:
    distinct HTTP codes carry distinct meanings and must not be merged.
    """
    from app.repo_workspace import _http_reason

    not_found = _http_reason(404)
    denied = _http_reason(401)
    forbidden = _http_reason(403)

    assert not_found != denied, "404 and 401 still produce the same explanation"
    assert not_found != forbidden, "404 and 403 still produce the same explanation"
    assert "token" not in not_found.lower(), (
        f"a 404 is still blamed on the token: {not_found!r} -- it means the repo "
        f"or the ref is absent, which is what actually happened in production."
    )
    assert "ref" in not_found.lower(), (
        f"the 404 explanation does not mention the ref: {not_found!r}. Naming the "
        f"likely cause is the whole point -- the wording is the diagnostic."
    )
    assert "token" in denied.lower() and "token" in forbidden.lower(), (
        "401/403 should still name the token; they really are auth failures"
    )


def test_the_line_based_fallback_cannot_bypass_the_diff_contract():
    """The first live run opened a PR containing code that cannot compile.

    `generate_fix()` applies the diff contract at fix_generator.py:132. But
    webhook.py:72 imports the PRIVATE `_generate_with_template` and calls it
    directly at line 2188, reaching around that guard -- so the weakest generator
    in the codebase ran unverified, and it fires EXACTLY WHEN the hardened
    template refuses. A deliberate refusal became a silent downgrade.

    What it produced on a real file, in a real repository:

        -    email: string,
        -    phoneNumber: string,          removed the PARAMETER
        +    email: string
             const request: CreateUserRequest = { name, email, phoneNumber };
                                                                ^^^ still there

        -    body: JSON.stringify(body),   an unrelated function
        +    body: JSON.stringify(body)

    The stray comma comes from a whole-file `re.sub(r',\\s*(\\n\\s*[}\\])])', ...)`.
    The contract catches all of it -- two orphan commas and a destroyed doc
    comment -- so the guard belongs INSIDE the generator, not in one caller.

    THE SECOND HALF IS THE IMPORTANT PART. The hardened path already refuses this
    file. What is asserted is that the refusal SURVIVES: the weak fallback must
    not be able to overturn it.
    """
    from app.fix_generator import _remove_field_references
    from app import diff_contract

    original = (
        '/**\n'
        ' * Every reference here is a JUDGMENT call.\n'
        ' */\n'
        'export interface CreateUserRequest {\n'
        '  name: string;\n'
        '  email: string;\n'
        '  phoneNumber: string;\n'
        '}\n'
        '\n'
        'async function post<T>(url: string, body: unknown): Promise<T> {\n'
        '  const response = await fetch(url, {\n'
        '    method: "POST",\n'
        '    body: JSON.stringify(body),\n'
        '  });\n'
        '  return (await response.json()) as T;\n'
        '}\n'
        '\n'
        'export class UserClient {\n'
        '  async createUser(\n'
        '    name: string,\n'
        '    email: string,\n'
        '    phoneNumber: string,\n'
        '  ): Promise<User> {\n'
        '    const request: CreateUserRequest = { name, email, phoneNumber };\n'
        '    return post<User>("/users", request);\n'
        '  }\n'
        '}\n'
    )

    fixed, note = _remove_field_references(original, "phoneNumber", "typescript")

    # 1. the refusal survives -- the weak generator does not get to overturn it
    assert fixed == original, (
        "the line-based fallback still returns a patch the diff contract "
        f"rejects. note={note!r}\n"
        "Every reference in this file is a judgment shape (parameter, shorthand), "
        "so the only correct answer is to change nothing."
    )

    # 2. and it says WHY, rather than going quiet
    assert note, "the fallback returned no explanation at all"
    assert any(w in note.lower() for w in ("refus", "contract", "unsafe")), (
        f"the explanation does not say the patch was rejected: {note!r}"
    )

    # 3. the contract really does reject that patch -- so test 1 is not vacuous
    #    (if the fallback ever stops producing it, this pins WHY it was banned)
    from app.fix_generator import _remove_field_references_unchecked as _raw
    raw, _ = _raw(original, "phoneNumber", "typescript")
    assert raw != original, (
        "the unchecked generator no longer changes this file, so test 1 passes "
        "for a different reason than intended -- re-derive it."
    )
    verdict = diff_contract.check(original, raw, "phoneNumber", "typescript")
    assert not verdict.ok, (
        "the diff contract now ACCEPTS the line-based patch. Either the contract "
        "weakened or the generator improved; find out which before relaxing this."
    )


def test_the_governed_decision_is_platform_neutral_and_denies_auto_without_a_tree():
    """Every platform must reach the SAME decision function, not a copy of it.

    The governance audit measured the gap: in the GitLab/Bitbucket region of
    webhook.py, `pr_level`, `_fetch_consumer_tree`, `_validate_fix_against_tree`
    and `ChangeRun` each appeared ZERO times. Those pipelines fetched a consumer,
    generated a fix and opened a merge request directly -- no routing decision, no
    validation, and no recorded terminal state, so a breaking change could
    terminate in silence on a customer's repository.

    Duplicating the decision per platform is what produced that gap, and it is the
    same shape as the eight disagreeing language maps and the 154-line inline
    pipelines. So the decision moves into ONE helper that every platform calls.

    THE SECOND ASSERTION IS THE LOAD-BEARING ONE. repo_workspace fetches GITHUB
    tarballs, so a GitLab tree cannot be fetched at all today -- `tree` is None
    there. That must yield REVIEW, permanently and by construction, never AUTO:
    "we could not compile it" must not read as "it is fine". GitLab and Bitbucket
    can be governed and contract-checked before they can be compiled, and claiming
    otherwise would recreate the gap being closed here.
    """
    from app.webhook import _govern_consumer_fix
    from app.run_outcome import ChangeRun
    from app import activity as _activity

    _acts = _activity.all_events()
    _before = len(_acts)

    # A GitLab-shaped call: a fix was generated, but no tree exists to compile it.
    run = ChangeRun(change_type="removed_field", spec="user.proto",
                    repo="acme/billing")
    decision, validated = _govern_consumer_fix(
        platform="gitlab",
        repo="acme/billing",
        consumer_file="src/client.ts",
        fixed_code="const x = 1;\n",
        tree=None,
        language="typescript",
        contract="proto",
        change_type="removed_field",
        confidence=0.99,          # deliberately maximal -- confidence must not buy AUTO
        min_confidence=0.5,
        run=run,
    )

    assert validated is None, (
        f"validated={validated!r} with no tree. None is the only honest answer: "
        f"nothing was compiled."
    )
    assert decision.level.value != "AUTO", (
        f"a platform with no tree reached {decision.level.value} at confidence 0.99. "
        f"Confidence is not verification -- this is exactly the conflation "
        f"pr_level() was changed to prevent."
    )
    joined = " ".join(decision.reasons).lower()
    assert "validat" in joined, (
        f"the decision does not say it was unvalidated: {decision.reasons}. The "
        f"reason is what a human reads in the PR body."
    )

    # And the refusal is RECORDED, so nothing terminates in silence.
    # tools/audit_pipeline_governance.py declares the caller's bare `continue` as an
    # allowed silent exit BECAUSE of this assertion. If the helper stops recording,
    # this fails and that allowance stops being true -- which is the whole point of
    # pinning it here rather than trusting a comment.
    if not decision.opens_pr:
        assert run.detail().get("refused"), (
            "the decision refused to open a PR but the ChangeRun recorded nothing "
            "-- a silent terminal state is the defect this closes"
        )
        assert any("pr_skipped" in str(e.get("action", ""))
                   for e in _activity.all_events()[_before:]), (
            f"no pr_skipped activity was logged for the refusal. The governance "
            f"audit's SILENT_EXIT_OK entry depends on this signal existing; new "
            f"actions were {[e.get('action') for e in _activity.all_events()[_before:]]}"
        )


def test_a_validated_fix_can_still_reach_auto_through_the_shared_helper():
    """The helper must not become a blanket downgrade.

    Routing everything through one function is only correct if the GitHub path
    keeps its behaviour: a fix that really compiled must still earn AUTO. If this
    ever fails, the shared helper has traded one bug (no governance off GitHub)
    for a worse one (AUTO unreachable everywhere), and the live run this morning
    already showed how easy it is to make AUTO unreachable by accident.

    Validation is stubbed rather than run: this pins the DECISION, and a real
    compile is covered by test_a_hoisted_workspace_validates_at_the_package_not_the_root.
    """
    import app.webhook as w
    from app.run_outcome import ChangeRun

    original = w._validate_fix_against_tree
    w._validate_fix_against_tree = lambda tree, f, code: (True, {"validation": "VALID"})
    try:
        run = ChangeRun(change_type="removed_field", spec="api.yaml",
                        repo="acme/api")
        decision, validated = w._govern_consumer_fix(
            platform="github",
            repo="acme/billing",
            consumer_file="src/user.ts",
            fixed_code="const x = 1;\n",
            tree="/tmp/does-not-matter",
            language="typescript",
            contract="openapi",
            change_type="removed_field",
            confidence=0.95,
            min_confidence=0.5,
            run=run,
        )
    finally:
        w._validate_fix_against_tree = original

    assert validated is True, f"validated={validated!r} -- the stub returned True"
    assert decision.level.value == "AUTO", (
        f"a compiled fix in a proven cell reached {decision.level.value}, not AUTO: "
        f"{decision.reasons}. The shared helper must not downgrade GitHub."
    )


def test_no_platform_can_open_a_pr_outside_the_governed_path():
    """GitLab and Bitbucket used to fetch, fix and open a PR with nothing between.

    Measured before this change, in both regions of webhook.py:

        pr_level                       0 occurrences
        _fetch_consumer_tree           0
        _validate_fix_against_tree     0
        ChangeRun                      0

    So a breaking change on either platform could terminate in SILENCE on a
    customer's repository -- no routing decision, no validation, no recorded
    outcome -- and the PR was opened the moment the generator returned anything
    different from the input.

    ONE TABLE, NOT ONE TEST PER PLATFORM. A copied assertion drifts exactly the way
    the copied pipelines did: whichever copy nobody updates is the one that rots.
    Adding a platform means adding a row here, and the row fails until that
    platform is governed.

    THE DOMINANCE CHECK IS THE LOAD-BEARING ONE. `create_fix_mr` appearing in the
    same function as `_govern_consumer_fix` proves nothing if the PR call can still
    run when the decision refused -- that would be the original defect wearing a
    helper call. So every PR-creating call must sit INSIDE a branch testing the
    decision.
    """
    import ast as _ast
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "app", "webhook.py")) as fh:
        tree = _ast.parse(fh.read())

    def _name(call):
        f = call.func
        return f.id if isinstance(f, _ast.Name) else getattr(f, "attr", "")

    platforms = (
        ("gitlab_webhook", "create_fix_mr"),
        ("bitbucket_webhook", "bb_create_fix_pr"),
    )

    for fn_name, pr_fn in platforms:
        fn = next((n for n in _ast.walk(tree)
                   if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                   and n.name == fn_name), None)
        assert fn is not None, f"{fn_name} is gone -- re-derive this test"

        called = {_name(c) for c in _ast.walk(fn) if isinstance(c, _ast.Call)}
        assert "_govern_consumer_fix" in called, (
            f"{fn_name} does not call _govern_consumer_fix, so it still decides "
            f"for itself whether to open a PR -- the gap this closes."
        )
        assert "ChangeRun" in called, (
            f"{fn_name} does not open a ChangeRun, so a breaking change on that "
            f"platform can still terminate with no stated outcome."
        )

        pr_calls = [c for c in _ast.walk(fn)
                    if isinstance(c, _ast.Call) and _name(c) == pr_fn]
        assert pr_calls, f"{pr_fn} is gone from {fn_name} -- re-derive this test"

        guarded = []
        for node in _ast.walk(fn):
            if not isinstance(node, _ast.If):
                continue
            test_src = " ".join(
                getattr(n, "attr", "") or getattr(n, "id", "")
                for n in _ast.walk(node.test)
            )
            if "opens_pr" not in test_src:
                continue
            guarded += [c for c in _ast.walk(node)
                        if isinstance(c, _ast.Call) and _name(c) == pr_fn]

        assert len(guarded) == len(pr_calls), (
            f"{fn_name}: {len(pr_calls) - len(guarded)} {pr_fn} call(s) are NOT "
            f"inside a branch testing the decision. A PR that can be opened when "
            f"the decision refused is the original defect wearing a helper call."
        )

        # And no platform may hardcode a change-type verb into its title. Third
        # occurrence of that shape: the LLM prompt, the PR explanation, the MR title.
        src_seg = _ast.unparse(fn)
        assert "Add required field '" not in src_seg, (
            f"{fn_name} still hardcodes \"Add required field\" as a title, so a "
            f"REMOVAL opens a PR announcing an addition. Use _fix_title()."
        )


def test_no_title_ever_announces_the_wrong_operation():
    """A title that names the wrong operation is worse than a vague one.

    FOURTH occurrence of one shape. Each time, something was hardcoded for
    `add_required_field` and then applied to all twelve operations:

        app/fix_generator.py   the LLM PROMPT said "add the new required field",
                               so Gemini dutifully ADDED a parameter when asked to
                               remove one -- the diff contract was the only save
        app/fix_generator.py   the EXPLANATION said "Added required field", which
                               would have appeared in a removal PR in a stranger's
                               repository
        app/webhook.py         the GitLab/Bitbucket MR TITLE, fixed in this plan
        app/pr_engine.py       still live at line 81 when this test was written

    So the mapping is exhaustive and CI-gated rather than best-effort. EVERY
    canonical op must be named explicitly: a thirteenth op added to
    change_types.CANONICAL_OPS fails here instead of silently inheriting a neutral
    phrase, which is the mechanism that let "add required field" spread four times.

    The verb assertions are the point. "Remove references to deleted field 'x'"
    and "Add required field 'x'" are opposite instructions to a human reader, and
    the diff sits right below the title -- a reader who trusts the title misreads
    the change.
    """
    from app.change_types import fix_title, CANONICAL_OPS, canonical_op
    from app.diff_engine import BreakingChange

    def mk(change_type):
        return BreakingChange(
            change_type=change_type, path="/users", method="GET",
            field_name="phoneNumber", field_type="string", location="body",
            severity="breaking", description="x")

    # 1. every canonical op is named EXPLICITLY -- none falls through
    for op in CANONICAL_OPS:
        assert canonical_op(op) == op, (
            f"canonical_op({op!r}) is not idempotent, so this test cannot address "
            f"ops by name -- re-derive it")
        title = fix_title(mk(op))
        assert title, f"{op}: empty title"
        assert "references to '" not in title, (
            f"{op} fell through to the NEUTRAL fallback: {title!r}. Every op in "
            f"CANONICAL_OPS must be named explicitly -- inheriting a default is "
            f"exactly how 'add required field' spread to four call sites."
        )

    # 2. a removal must never say "add", and vice versa
    removals = [op for op in CANONICAL_OPS if op.startswith("remove")]
    assert removals, "no removal ops found -- re-derive this test"
    for op in removals:
        title = fix_title(mk(op)).lower()
        assert "add" not in title, (
            f"{op} produced a title containing 'add': {title!r}. This is the exact "
            f"defect: a removal announcing an addition."
        )
    for op in ("add_required", "add_optional"):
        assert "add" in fix_title(mk(op)).lower(), (
            f"{op} does not say 'add': {fix_title(mk(op))!r}")
    for op in ("rename_field", "rename_type"):
        assert "renam" in fix_title(mk(op)).lower(), (
            f"{op} does not say 'rename': {fix_title(mk(op))!r}")

    # 3. an UNKNOWN string still gets something neutral rather than raising --
    #    a webhook must not 500 because a diff engine emitted a new dialect
    assert fix_title(mk("some_dialect_nobody_mapped")), "unknown op produced nothing"

    # 4. and no module builds a title by hardcoding the operation. pr_engine.py:81
    #    was the survivor: the CLI path the governance audit lists as EXEMPT, so
    #    nothing else was watching it.
    import ast as _ast
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for module in ("pr_engine.py", "webhook.py"):
        with open(os.path.join(root, "app", module)) as fh:
            mod = _ast.parse(fh.read())
        for node in _ast.walk(mod):
            if not isinstance(node, _ast.Assign):
                continue
            targets = [t.id for t in node.targets if isinstance(t, _ast.Name)]
            if not any(t in ("title", "commit_msg", "mr_title") for t in targets):
                continue
            rendered = _ast.unparse(node.value)
            assert "Add required field" not in rendered, (
                f"app/{module} assigns {targets} a hardcoded "
                f"\"Add required field\" title: {rendered[:110]} -- use fix_title()."
            )


def test_all_three_platforms_are_governed_and_none_is_merely_disabled():
    """The audit's expectations are the invariant; this pins the new one.

    Before this plan the audit read:

        1 governed, 2 disabled, 2 exempt, 5 total

    and its DISABLED table asserted that gitlab_webhook and bitbucket_webhook
    "must stay off" -- because each inlined ~154 lines of pipeline that bypassed
    both the routing decision and the outcome funnel. Switching them off was the
    right call at the time: an exemption tolerates an ungoverned path, and a
    breaking change on those paths could terminate in silence.

    They are now governed instead, which is a strictly stronger position than
    disabled. GOVERNED IS NOT THE SAME AS ENABLED: the experimental_enabled()
    guard stays, so both remain off by default. What changed is that turning them
    on is now a deployment decision rather than a safety risk.

    THE POINT OF ASSERTING THIS IN A TEST is that "disabled" was load-bearing. If
    someone re-inlines a pipeline or drops the pr_level call, the audit must fail
    rather than quietly returning to two ungoverned platforms with the env var
    already set in production.
    """
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.run(
        [sys.executable, os.path.join(root, "tools", "audit_pipeline_governance.py")],
        capture_output=True, text=True, cwd=root)

    assert out.returncode == 0, (
        f"the governance audit FAILS:\n{out.stdout[-1500:]}\n{out.stderr[-500:]}")

    text = out.stdout
    assert "3 governed" in text, (
        f"the audit does not report 3 governed entry points. It said:\n"
        f"{[l for l in text.splitlines() if 'governed' in l]}\n"
        f"All three platforms must reach pr_level and the outcome funnel."
    )
    assert "0 disabled" in text, (
        f"the audit still reports disabled entry points:\n"
        f"{[l for l in text.splitlines() if 'disabled' in l]}\n"
        f"A governed platform does not need to be switched off to be safe."
    )
    for platform in ("gitlab_webhook", "bitbucket_webhook"):
        assert platform in text, f"{platform} vanished from the audit entirely"


def test_the_disabled_notice_does_not_claim_something_untrue():
    """The 501 body is customer-facing, and it made a claim about the code.

    app/experimental.py returned, in the response body:

        "the {platform} path additionally bypasses the routing decision and the
         outcome funnel -- so a breaking change there could terminate in silence"

    That was accurate when written and is now false: both paths call
    _govern_consumer_fix and open a ChangeRun. A stale reason string is worse than
    a vague one here, because it is served to whoever tried to connect and it tells
    them the integration is unsafe rather than merely switched off.

    Same class as the docstring that promised tokens.json survived redeploys while
    nothing wrote to it: a comment describing an intention rather than the code.
    """
    from app.experimental import experimental_disabled

    body = experimental_disabled("gitlab", "webhook").body.decode()
    for stale in ("bypasses the routing decision", "could terminate in silence"):
        assert stale not in body, (
            f"the 501 body still claims {stale!r}, which stopped being true when "
            f"gitlab_webhook was routed through _govern_consumer_fix."
        )
    # it must still say WHY it is off and how to reverse it -- that was the point
    assert "RIPPLE_ENABLE_EXPERIMENTAL_PLATFORMS" in body, (
        "the notice no longer says how to re-enable the platform")


def test_the_llm_is_reachable_from_the_webhook_but_only_with_a_key():
    """The LLM path existed, was correct, and production could not reach it.

    Measured before this change:

        app/webhook.py   generate_fix imported: True   referenced anywhere: False
                         calls _generate_fix_with_rag_fallback instead
                           -> generate_fix_rag()        no LLM gate
                           -> _generate_with_template() no LLM gate
        app/cli.py       calls generate_fixes -> generate_fix   the ONLY live route

    The gate `if use_llm and _llm_key()` lives inside generate_fix(), so setting a
    key in production changed nothing. Confirmed by a real webhook run: every fix
    source was [template] or [RAG/template], never [llm]. Sixth appearance of
    built-tested-CI-gated-unreachable in this repo, and the reachability gate could
    not see it because fix_generator is imported for other reasons -- module
    granularity is structurally blind to a branch inside a reachable module.

    THE ROOT CAUSE WAS NOT AN OVERSIGHT. generate_fix() read the consumer file from
    DISK, which is CLI-shaped; the webhook holds content fetched from an API and has
    no file to open, so the call would have raised IOError and returned None anyway.

    BYO-KEY IS THE DEFAULT-OFF POSITION. With no key, nothing is attempted and no
    source leaves the machine -- so "production fixes are deterministic and your
    code never reaches a model" stays literally true unless a customer opts in.
    """
    import inspect
    import app.webhook as w
    from app.fix_generator import generate_fix

    # 1. the webhook must actually REACH the guarded function
    src = inspect.getsource(w._generate_fix_with_rag_fallback)
    assert "generate_fix(" in src, (
        "_generate_fix_with_rag_fallback does not call generate_fix, so the LLM "
        "branch and the diff contract that guards it are both unreachable from "
        "every platform. Its own docstring already claimed 'Claude LLM (ONLY if "
        "1-3 all fail)' -- a docstring describing an intention, not the code."
    )

    # 2. it must pass content, not rely on a file existing on disk
    params = inspect.signature(generate_fix).parameters
    assert "original_code" in params, (
        "generate_fix still only reads from disk. The webhook has no file to open, "
        "so the call raises IOError and returns None -- unreachable in practice "
        "even once it is called.")
    assert params["original_code"].default is None, (
        "original_code must default to None so the CLI keeps reading from disk")

    # 3. NO BACKEND -> NO ATTEMPT, and a KEYLESS LOCAL backend DOES count.
    #    is_configured() is key-OR-self-hosted, because a locally run model
    #    authenticates nothing; gating on a token alone made a self-hosted
    #    deployment fall silently through to the template.
    import os as _os
    _keys = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL")
    saved = {k: _os.environ.get(k) for k in _keys}

    def _attempts() -> bool:
        called = []
        import app.fix_generator as fg
        real = fg._generate_with_llm
        fg._generate_with_llm = lambda *a, **k: called.append(1) or ("", "")
        try:
            class _C:
                file_path = "x.ts"
                language = "typescript"
            from app.diff_engine import BreakingChange
            ch = BreakingChange(change_type="removed_field", path="/users",
                                method="GET", field_name="phoneNumber",
                                field_type="string", location="body",
                                severity="breaking", description="x")
            # CONTENT THE DETERMINISTIC PATH REFUSES. `const a = user.phoneNumber;`
            # would be fixed by the template and the LLM would never be reached --
            # which is correct behaviour and would make this test assert nothing.
            # A constructor parameter plus a shorthand property is the shape
            # ts_codemod declines, so this exercises the LAST RESORT specifically.
            refused = (
                "export class C {\n"
                "  constructor(private phoneNumber: string) {}\n"
                "  build() { return { phoneNumber }; }\n"
                "}\n"
            )
            w._generate_fix_with_rag_fallback(refused, _C(), ch, "")
        finally:
            fg._generate_with_llm = real
        return bool(called)

    try:
        from app.llm_config import is_configured, is_self_hosted

        for k in _keys:
            _os.environ.pop(k, None)
        assert not is_configured(), "is_configured() is true with nothing set"
        assert not _attempts(), (
            "the LLM was invoked with NO backend configured. Default-off is the "
            "whole position: without it, customer source can reach a model that "
            "nobody chose.")

        # a self-hosted endpoint, NO key -- this is the local-model deployment
        _os.environ["ANTHROPIC_BASE_URL"] = "http://ripple-llm.railway.internal:11434"
        assert is_self_hosted() and is_configured(), (
            "a keyless self-hosted base_url is not recognised as configured, so a "
            "local model would silently never be used")
        assert _attempts(), (
            "with a self-hosted backend configured the LLM was still not attempted "
            "-- the gate and the deployment disagree")

        # the real Anthropic API with NO key must remain OFF: source must never
        # reach a third party by accident
        _os.environ["ANTHROPIC_BASE_URL"] = "https://api.anthropic.com"
        assert not is_configured(), (
            "a keyless configuration pointing at api.anthropic.com counts as "
            "configured -- that would send source to a third party with no "
            "credential and no decision")
    finally:
        for k, v in saved.items():
            _os.environ.pop(k, None)
            if v is not None:
                _os.environ[k] = v

    # 4. the diff contract still guards the LLM branch -- wiring must not bypass it
    gsrc = inspect.getsource(generate_fix)
    assert "_diff_check" in gsrc or "diff_contract" in gsrc, (
        "generate_fix no longer applies the diff contract, so an LLM patch could "
        "reach a PR unverified -- the defect the line-based fallback had.")


def test_the_llm_path_is_declared_in_the_reachability_gate():
    """Module-level reachability is structurally blind to this, so it is declared.

    app/fix_generator.py is imported by app/webhook.py for other reasons, so the
    existing LAYERS table reports it reachable and always would have -- including
    while the LLM branch inside it was dead. That coarseness is exactly why this
    went unnoticed, and the fix is a FUNCTION-level declaration rather than a
    finer-grained guess.

    The gate fails in BOTH directions, as the module-level one does: a declared-
    reachable function becoming unreachable is a regression, and a declared-
    unreachable one becoming reachable forces someone to delete the consequence
    text and state what is now true.
    """
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
    import audit_safety_reachability as R

    assert hasattr(R, "FUNCTION_LAYERS"), (
        "the reachability gate has no FUNCTION_LAYERS table, so a safety-relevant "
        "branch inside an imported module cannot be declared at all")

    # DERIVED FROM THE REGISTRY, not pinned to one name. Every wire format that
    # claims a client must name a function, and every such function is on the path
    # where customer source leaves the machine -- so every one must be declared.
    #
    # The original asserted the single literal ("fix_generator",
    # "_generate_with_llm"). Measured consequence: adding a second client
    # (_generate_with_openai for Groq/Cerebras, both OpenAI-format) would leave this
    # gate GREEN while the new path went undeclared -- one of four gates that all
    # keyed on that one name and would all have passed without asserting anything
    # about it. Deriving from FORMAT_CLIENTS means declaring a format without
    # declaring its function fails here.
    from app.llm_providers import FORMAT_CLIENTS

    assert FORMAT_CLIENTS, (
        "no wire format claims a client, so this check would pass over an empty set")

    for fmt, key in sorted(FORMAT_CLIENTS.items()):
        assert key in R.FUNCTION_LAYERS, (
            f"wire format {fmt!r} claims client {key}, which is not declared in the "
            f"reachability gate. The LLM path is safety-relevant: it is the only "
            f"path on which customer source leaves the machine.")
        entry = R.FUNCTION_LAYERS[key]
        assert entry.get("role"), f"{key} is declared with no stated role"
        if entry.get("reachable"):
            assert not entry.get("consequence"), (
                f"{key} is a reachable layer but still carries a consequence for "
                f"being unreachable -- delete it and say what is true now")
        else:
            assert entry.get("consequence"), (
                f"{key} is declared unreachable and must name its cost, or the "
                f"declaration is just a note")


def test_a_keyless_self_hosted_backend_can_actually_construct_a_client():
    """is_configured() opened the gate and the call site then refused to call.

    Found by running it against a real local model (Ollama in Docker, serving
    native Anthropic /v1/messages at localhost:11434). The gate said yes:

        api_key()        ''
        is_self_hosted() True
        is_configured()  True

    and then the request failed:

        LLM error: "Could not resolve authentication method. Expected one of
        api_key, auth_token, or credentials to be set. Or for one of the
        `X-Api-Key` or `Authorization` headers to be explicitly omitted"

    The Anthropic SDK refuses to construct with an empty api_key even when
    base_url points at a server that authenticates nothing. So yesterday's fix
    moved the disagreement one layer down rather than removing it: the GATE
    accepts keyless self-hosted, the CLIENT cannot do keyless.

    That is the third time this exact shape has appeared in this module's area --
    llm_config.py exists BECAUSE three call sites each decided independently how to
    reach the model, and its own comment warns that a gate reading one thing while
    the call site reads another sends a real configuration silently to the
    template. Hence one resolution point: client_api_key().

    THE THIRD ASSERTION IS THE SAFETY ONE. A placeholder must NEVER be handed out
    when nothing is configured, or an unconfigured deployment would start sending
    source code to api.anthropic.com with a fake credential instead of doing
    nothing.
    """
    import os as _os
    from app import llm_config as c

    keys = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL")
    saved = {k: _os.environ.get(k) for k in keys}
    try:
        # 1. keyless self-hosted -> a NON-EMPTY value, so the SDK can construct
        for k in keys:
            _os.environ.pop(k, None)
        _os.environ["ANTHROPIC_BASE_URL"] = "http://localhost:11434"
        assert c.is_self_hosted() and c.is_configured(), "precondition broken"
        got = c.client_api_key()
        assert got, (
            "client_api_key() is empty for a keyless self-hosted backend, so "
            "anthropic.Anthropic(api_key=...) raises 'Could not resolve "
            "authentication method' and every self-hosted fix silently falls "
            "through to the template."
        )

        # 2. a real key always wins -- the placeholder must not shadow it
        _os.environ["ANTHROPIC_AUTH_TOKEN"] = "sk-real-token"
        assert c.client_api_key() == "sk-real-token", (
            f"a configured token was replaced by {c.client_api_key()!r}")

        # 3. NOTHING configured -> NO placeholder. This is the safety property:
        #    a fake credential must never let an unconfigured deployment reach a
        #    third-party API.
        for k in keys:
            _os.environ.pop(k, None)
        assert not c.is_configured(), "precondition broken"
        assert not c.client_api_key(), (
            f"client_api_key() handed out {c.client_api_key()!r} with nothing "
            f"configured. That would let an unconfigured install talk to "
            f"api.anthropic.com with a placeholder instead of doing nothing."
        )
    finally:
        for k, v in saved.items():
            _os.environ.pop(k, None)
            if v is not None:
                _os.environ[k] = v

    # 4. and no call site may construct a client from api_key() directly -- that
    #    is the bug. They must go through client_api_key().
    import ast as _ast
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for module in ("fix_generator.py", "natural_language.py"):
        path = os.path.join(root, "app", module)
        if not os.path.isfile(path):
            continue
        src = open(path).read()
        tree = _ast.parse(src)
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Call):
                continue
            for kw in node.keywords or []:
                if kw.arg not in ("api_key", "x-api-key"):
                    continue
                rendered = _ast.unparse(kw.value)
                assert "client_api_key" in rendered or "api_key()" not in rendered, (
                    f"app/{module} passes {rendered} as api_key -- use "
                    f"llm_config.client_api_key() so a keyless self-hosted "
                    f"backend can construct."
                )


def test_every_learning_call_matches_the_function_it_calls():
    """The learning loop failed for a reason no test could have caught by reading.

    `_handle_pr_merged` called:

        learn_from_merged_pr(trigger_diff=..., fix_diff=..., language=...,
                             field_name=..., change_type=..., store=...)

    against a function whose whole signature is `(pattern_id: str)`. Every merged
    Ripple PR therefore raised

        TypeError: learn_from_merged_pr() got an unexpected keyword argument
                   'trigger_diff'

    swallowed by `except Exception: return {"status": "learn_error"}` — and no
    _log_activity fired, because the log line sat AFTER the failing call inside the
    same try. So the RAG store held 0 patterns, the dashboard showed nothing, and
    the only trace was an HTTP response body nobody reads.

    THIS IS A KNOWN CLASS IN THIS FILE, NOT A ONE-OFF. `generate_fix_rag`'s own
    docstring records the identical defect — webhook called it with a keyword shape
    that "did not match the positional signature at all" and "would have raised
    TypeError on the first real invocation." That one was fixed with keyword-only
    aliases; these two were not.

    So this test guards the CLASS: it binds each call site's actual keywords against
    the callee's real signature with inspect.Signature.bind, which fails for any
    future rename or added required parameter. Asserting only that the store gains a
    row would pass a call that happens to work today and break on the next rename.
    """
    import ast as _ast
    import inspect as _inspect
    import app.rag_retriever as _rr

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "app", "webhook.py")) as fh:
        tree = _ast.parse(fh.read())

    targets = {
        "learn_from_merged_pr": _rr.learn_from_merged_pr,
        "learn_from_rejected_pr": _rr.learn_from_rejected_pr,
    }
    seen = {name: 0 for name in targets}

    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Call):
            continue
        fname = (node.func.id if isinstance(node.func, _ast.Name)
                 else getattr(node.func, "attr", ""))
        if fname not in targets:
            continue
        seen[fname] += 1
        sig = _inspect.signature(targets[fname])
        args = [_ast.unparse(a) for a in node.args]
        kwargs = {kw.arg: _ast.unparse(kw.value)
                  for kw in (node.keywords or []) if kw.arg}
        try:
            sig.bind(*args, **kwargs)
        except TypeError as exc:
            raise AssertionError(
                f"webhook.py calls {fname}({', '.join(args)}"
                f"{', ' if args and kwargs else ''}"
                f"{', '.join(f'{k}=...' for k in kwargs)}) but its signature is "
                f"{fname}{sig} -- {exc}. This is the defect that kept the RAG "
                f"store at 0 patterns: the call raised TypeError on every merged "
                f"PR and the except swallowed it."
            ) from None

    for name, count in seen.items():
        assert count, (
            f"webhook.py no longer calls {name} at all, so a merged or closed PR "
            f"teaches Ripple nothing. The learning loop is the difference between "
            f"'self-maintaining' and 'runs the same way forever'."
        )


def test_an_unattributable_pr_teaches_nothing():
    """No provenance row means no attribution, and a guess is worse than nothing.

    The old identity check was `"Generated by" not in body and "Ripple" not in
    body` — a substring test on text a human can write, which matches any PR that
    merely MENTIONS Ripple. Attributing a stranger's merge to one of Ripple's
    patterns would raise that pattern's confidence on evidence that has nothing to
    do with it.

    The provenance ledger replaces the heuristic: a PR Ripple opened has a row
    naming the pattern, the change type, and the file. No row means not ours, or
    ours from before the ledger existed — either way the honest action is to record
    nothing, the same rule as `validated=None -> REVIEW`.
    """
    from app import pr_ledger

    assert pr_ledger.lookup("https://github.com/someone/else/pull/999") is None, (
        "the ledger claims provenance for a PR it never recorded")

    import app.webhook as w
    payload = {"repository": {"full_name": "someone/else"}}
    pr = {"number": 999, "merged": True,
          "html_url": "https://github.com/someone/else/pull/999",
          "body": "Fixes the thing. Thanks Ripple for the idea!"}

    before = len(pr_ledger.all_outcomes())
    result = w._handle_pr_merged(payload, pr)
    after = len(pr_ledger.all_outcomes())

    assert after == before, (
        f"an unattributable PR wrote {after - before} outcome(s). A merge Ripple "
        f"cannot attribute must teach it nothing -- otherwise a stranger's PR that "
        f"mentions Ripple raises a real pattern's confidence."
    )
    assert result.get("status") in ("ignored", "unattributed"), (
        f"expected the handler to decline, got {result!r}")


def test_all_three_platforms_record_pr_provenance():
    """One ledger writer, three call sites — the shape that stopped the drift.

    The outcome handler can only attribute a merge if the PR-creation path recorded
    which pattern produced it. That write has to happen on all three platforms or
    GitLab and Bitbucket merges are permanently unattributable, which is how the
    governed-path gap looked before it was closed.

    Asserted as a TABLE for the same reason as the governed-decision test: a copied
    assertion drifts, and whichever copy nobody updates is the one that rots.
    """
    import ast as _ast
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "app", "webhook.py")) as fh:
        tree = _ast.parse(fh.read())

    for fn_name in ("_process_spec_change_inner", "gitlab_webhook",
                    "bitbucket_webhook"):
        fn = next((n for n in _ast.walk(tree)
                   if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                   and n.name == fn_name), None)
        assert fn is not None, f"{fn_name} is gone -- re-derive this test"
        called = {
            (c.func.id if isinstance(c.func, _ast.Name)
             else getattr(c.func, "attr", ""))
            for c in _ast.walk(fn) if isinstance(c, _ast.Call)
        }
        assert "_record_pr_provenance" in called, (
            f"{fn_name} opens PRs without recording provenance, so any merge on "
            f"that platform is unattributable and teaches Ripple nothing."
        )


def test_an_inferred_pattern_cannot_overwrite_a_human_correction():
    """The ladder, and the split between evidence and opinion.

    `add_pattern` merged by identity and only ever set `source_file` when empty, so
    `strategy` — the prescriptive content — was decided by whichever write happened
    FIRST and never revisited. That is arbitrary, not a precedence rule: a guess
    folded in from the PropBench corpus could permanently define the approach for a
    field a reviewer had already corrected by hand.

    Two things are deliberately treated differently:

        COUNTERS are observations of the world. merge_count / reject_count /
        example_count accumulate from ANY source, including a lower-ranked one,
        because a rejection is a fact regardless of who noticed it.

        STRATEGY is an opinion. Only an equal-or-higher provenance may replace it.

    Conflating them would mean either losing real outcome evidence or letting a
    guess redefine the fix. Rank order, highest first: human_edit, merged_clean,
    rejected, inferred.
    """
    from app.rag_store import PatternStore, FixPattern, PROVENANCE_RANK

    assert PROVENANCE_RANK["human_edit"] > PROVENANCE_RANK["merged_clean"] \
        > PROVENANCE_RANK["rejected"] > PROVENANCE_RANK["inferred"], PROVENANCE_RANK

    store = PatternStore("test_ladder")
    store.patterns = []
    pid = store.make_pattern_id("removed_field", "typescript", "phoneNumber")

    store.add_pattern(FixPattern(
        pattern_id=pid, change_type="removed_field", language="typescript",
        field_name="phoneNumber", strategy="THE HUMAN CORRECTION",
        provenance="human_edit", merge_count=1))

    # an inferred write arrives later with a different opinion and one new observation
    store.add_pattern(FixPattern(
        pattern_id=pid, change_type="removed_field", language="typescript",
        field_name="phoneNumber", strategy="a guess from the corpus",
        provenance="inferred", reject_count=1, example_count=1))

    p = next(x for x in store.patterns if x.pattern_id == pid)
    assert p.strategy == "THE HUMAN CORRECTION", (
        f"an inferred write overwrote a human correction: {p.strategy!r}. This is "
        f"the failure that makes a learning loop get WORSE with more data.")
    assert p.provenance == "human_edit", (
        f"provenance was downgraded to {p.provenance!r} by a lower-ranked write")
    assert p.reject_count == 1, (
        f"reject_count is {p.reject_count} -- counters must accumulate from ANY "
        f"source. A rejection is a fact, not an opinion.")
    assert p.example_count == 2, f"example_count is {p.example_count}, expected 2"

    # a HIGHER rank may overwrite
    store.add_pattern(FixPattern(
        pattern_id=pid, change_type="removed_field", language="typescript",
        field_name="phoneNumber", strategy="A LATER HUMAN CORRECTION",
        provenance="human_edit"))
    p = next(x for x in store.patterns if x.pattern_id == pid)
    assert p.strategy == "A LATER HUMAN CORRECTION", (
        "an equal-ranked human correction could not update the strategy")

    # EQUAL rank, clearly worse ratio -> must NOT win
    store2 = PatternStore("test_ladder2")
    store2.patterns = []
    pid2 = store2.make_pattern_id("removed_field", "python", "phone")
    store2.add_pattern(FixPattern(
        pattern_id=pid2, change_type="removed_field", language="python",
        field_name="phone", strategy="PROVEN", provenance="merged_clean",
        merge_count=9, reject_count=1))
    store2.add_pattern(FixPattern(
        pattern_id=pid2, change_type="removed_field", language="python",
        field_name="phone", strategy="mostly rejected", provenance="merged_clean",
        merge_count=1, reject_count=9))
    p2 = next(x for x in store2.patterns if x.pattern_id == pid2)
    assert p2.strategy == "PROVEN", (
        f"a 0.10-ratio write replaced a 0.90-ratio one at equal rank: "
        f"{p2.strategy!r}. Within 0.1 the newer wins; beyond that the better "
        f"ratio must.")


def test_a_contaminated_outcome_is_recorded_but_teaches_nothing():
    """An outcome you cannot attribute is worse than none -- it is confident noise.

    A merge is only usable as a training example when you can tell WHICH part of the
    final state was Ripple's fix. Four situations break that, and each is recorded
    in the ledger (the audit trail stays true) while deriving no pattern:

        edits beyond the field's references   you cannot separate the fix from them
        squashed with unrelated commits       the diff is not attributable
        no provenance row                     attribution would be a guess
        the consumer file moved on            the base changed underneath

    KiroCrew's analogue is refusing to synthesise a skill from any session that
    touched credentials -- discard the sample entirely rather than partially trust
    it. The failure mode being avoided is a pattern whose confidence rests on
    evidence about something else.
    """
    import app.webhook as w

    row = {"pattern_id": "abc", "source": "pattern", "consumer_file": "src/x.ts",
           "field_name": "phoneNumber"}

    # No proposed_files on this row on purpose: it doubles as coverage that a
    # LEGACY row written before Stage 1 still falls back to a bound of 1.
    clean = {"number": 1, "commits": 1, "changed_files": 1}
    contaminated, reason = w._outcome_is_contaminated(clean, row)
    assert not contaminated, f"a clean single-commit merge was called contaminated: {reason}"

    for label, pr in (
        ("extra files touched", {"number": 2, "commits": 1, "changed_files": 7}),
        ("squash of many commits", {"number": 3, "commits": 6, "changed_files": 1}),
    ):
        contaminated, reason = w._outcome_is_contaminated(pr, row)
        assert contaminated, f"{label} was NOT flagged as contaminated"
        assert reason, f"{label} was flagged with no stated reason"

    contaminated, reason = w._outcome_is_contaminated(clean, None)
    assert contaminated and reason, "a missing provenance row must contaminate"


def test_a_clean_template_merge_derives_a_pattern():
    """The common case has to teach something, or the loop only ever learns about
    fixes it already had a pattern for.

    Stage 2 left this open on purpose: a template-generated fix carries no
    `pattern_id`, so a clean merge recorded an outcome and credited no counters.
    Since the deterministic template is what produces almost every fix today, the
    store would have stayed empty while the ledger filled up — learning about
    patterns it already had, and nothing else.

    A clean, uncontaminated merge of a template fix is exactly the evidence needed
    to CREATE the pattern: the world confirmed the approach on a real repository.
    Provenance is `merged_clean`, not `inferred`, because it was observed rather
    than guessed — and that rank is what stops a later corpus guess overwriting it.
    """
    import app.webhook as w
    from app import pr_ledger
    from app.rag_store import rag_store
    import uuid as _uuid

    pr_ledger.reset_for_tests()

    # A UNIQUE field per run. make_pattern_id is deterministic over
    # (change_type, language, field_name), so a fixed name merges into the row the
    # PREVIOUS run wrote and the count never rises -- the test passed once and
    # failed forever after. The store persists to the data dir, so isolation has to
    # come from the identity, not from hoping the file is empty.
    field = f"testField{_uuid.uuid4().hex[:10]}"
    before = len(rag_store.patterns)

    url = f"https://github.com/acme/api/pull/{_uuid.uuid4().int % 100000}"
    pr_ledger.record_open(
        url, pattern_id="", source="template", change_type="removed_field",
        language="typescript", field_name=field,
        consumer_file="src/only.ts", repo="acme/api", validated=True, level="AUTO")

    # The author check is now LIVE: _record_pr_terminal fetches the commit
    # authors from the API. Substituting the fetch supplies what the network would,
    # and keeps this test about pattern DERIVATION rather than about HTTP.
    #
    # Substituting is safe here only because a separate test --
    # test_the_commit_authors_are_actually_fetched_not_read_from_the_payload --
    # pins that production really performs this fetch at the convergence point.
    # Without that pairing this would be the trap the old commit_list key was:
    # a seam only tests ever fill, making a dead check look alive.
    _real_fetch = w._fetch_pr_commit_authors
    w._fetch_pr_commit_authors = lambda *_a, **_k: ["ripple-api[bot]"]
    try:
        result = w._handle_pr_merged(
            {"repository": {"full_name": "acme/api"}},
            {"number": 4242, "html_url": url, "commits": 1, "changed_files": 1})
    finally:
        w._fetch_pr_commit_authors = _real_fetch

    assert result["outcome"] == "merged_clean", result
    after = len(rag_store.patterns)
    assert after == before + 1, (
        f"a clean template merge derived no pattern ({before} -> {after}). The "
        f"deterministic template produces nearly every fix, so without this the "
        f"store only ever learns about patterns it already had.")

    derived = next(p for p in rag_store.patterns if p.field_name == field)
    assert derived.provenance == "merged_clean", (
        f"derived pattern has provenance {derived.provenance!r} -- it was OBSERVED, "
        f"so it must outrank a later corpus guess")
    assert derived.merge_count == 1, f"merge_count is {derived.merge_count}"

    # Leave the store as we found it: a test that grows a persisted file by one row
    # per run is a slow leak on a mounted volume.
    rag_store.patterns = [p for p in rag_store.patterns if p.field_name != field]
    rag_store.save()


def test_a_relevant_consumer_is_not_displaced_by_the_platform_search_order():
    """GitLab and Bitbucket cut to five BEFORE anything looked at the files.

        consumers = client.search_code(...)
        for consumer in consumers[:5]:

    The platform's own search relevance decided which five Ripple would even READ,
    and nothing scored them. A file that genuinely references the changed field sat
    at position 6 and was never fetched, while five weak hits above it consumed
    every slot — invisible, because Ripple never saw the file it dropped.

    KiroCrew's memory store solves the same shape by admitting on the RAW score
    BEFORE the decay ranking, the MMR pass, and the `limit` cut, precisely so "a
    highly relevant but old memory cannot be ordered past `limit` by a cluster of
    recent-but-irrelevant rows". Ordering is a preference; admission is a
    correctness property, and doing them in the wrong order loses candidates
    silently.

    So: fetch a bounded candidate window, score each with the real matcher, ADMIT on
    match strength, rank, and only then cut. Admission needs evidence and evidence
    needs a fetch, so the window carries an explicit call budget — the same
    tree_budget pattern the GitHub path already uses.
    """
    from app.webhook import _admit_consumers

    # Five weak candidates first, then the real one. Only the last file actually
    # references the field; the others merely mention the word in prose.
    real = ("src/real_consumer.ts",
            'import { User } from "./types";\n'
            'export function line(u: User) { return u.phoneNumber; }\n')
    weak = [(f"docs/note{i}.md", f"# note {i}\nWe used to have a phoneNumber here.\n")
            for i in range(5)]
    candidates = [p for p, _ in weak] + [real[0]]
    blobs = dict(weak + [real])

    fetched: list = []

    def fetch(path):
        fetched.append(path)
        return blobs.get(path, "")

    budget = {"remaining": 50}
    admitted = _admit_consumers(
        candidates, fetch, field_name="phoneNumber", language_of=lambda p: (
            "typescript" if p.endswith(".ts") else "markdown"),
        max_consumers=2, budget=budget, candidate_window=25)

    paths = [p for p, _c, _s in admitted]
    assert real[0] in paths, (
        f"the only file that actually references the field was dropped. admitted="
        f"{paths}, fetched={fetched}. It sat at position 6 of the platform's search "
        f"order, which is exactly the candidate consumers[:5] could never see."
    )
    assert len(admitted) <= 2, f"the cap was not applied: {len(admitted)} admitted"
    assert admitted[0][0] == real[0], (
        f"ranking put {admitted[0][0]!r} above the genuine reference -- admission "
        f"must be followed by strength ordering, not search order")

    # the window is real: it looked past position 5
    assert len(fetched) > 5, (
        f"only {len(fetched)} candidate(s) were fetched, so admission still cannot "
        f"see past the platform's first five")

    # and the budget is respected rather than advisory
    tight = {"remaining": 2}
    _admit_consumers(candidates, fetch, field_name="phoneNumber",
                     language_of=lambda p: "typescript", max_consumers=5,
                     budget=tight, candidate_window=25)
    assert tight["remaining"] == 0, (
        f"budget ended at {tight['remaining']} -- an unbounded fetch loop is how a "
        f"wide installation scope drops the GitHub connection mid-run")


def test_both_scored_platforms_admit_before_they_cut():
    """One admission helper, both platforms -- asserted as a table.

    The `consumers[:5]` cut existed in gitlab_webhook AND bitbucket_webhook, which
    is the duplication shape this repo keeps paying for. A copied admission step
    would drift on whichever platform nobody updates.
    """
    import ast as _ast
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "app", "webhook.py")) as fh:
        src = fh.read()
    tree = _ast.parse(src)

    for fn_name in ("gitlab_webhook", "bitbucket_webhook"):
        fn = next((n for n in _ast.walk(tree)
                   if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                   and n.name == fn_name), None)
        assert fn is not None, f"{fn_name} is gone -- re-derive this test"
        body = _ast.unparse(fn)
        assert "_admit_consumers" in body, (
            f"{fn_name} does not call _admit_consumers, so it still cuts to a fixed "
            f"count on the platform's search order without reading the files.")
        assert "consumers[:5]" not in body, (
            f"{fn_name} still contains the raw `consumers[:5]` cut -- admission "
            f"before the limit is the whole point of this change.")


def test_mmr_spends_the_cap_on_breadth_rather_than_one_package():
    """MMR changes WHICH candidates survive the cap. It does not improve recall.

    Worth stating precisely, because the two are easy to conflate. Stage 4 recovered
    candidates the platform's search order hid -- files that were never fetched, so
    genuinely lost. That was recall. MMR touches only the case where MORE candidates
    were admitted than the cap allows, and every one of them is a real consumer that
    needs fixing. Dropping one is a loss either way; MMR only decides which loss.

    The choice it makes: prefer covering two packages over five files in one. A
    reviewer in `reporting` never hearing about the break at all is worse than a
    reviewer in `checkout` getting three PRs instead of five, because Ripple opens
    one PR per file and the second, third and fourth PR in the same package land in
    front of the same person.

    Jaccard over PATH tokens, not content. Two files in one package usually share the
    same fix and the same reviewer; two files with similar CONTENT in different
    packages are two genuine consumers that both need changing, so content
    similarity would suppress exactly what should be kept.

    THE SECOND AND THIRD ASSERTIONS ARE THE GUARDS. Greedy MMR must always take the
    strongest candidate first -- a diversity pass that can displace the best match
    is a bug, not a preference. And below the cap it must be a no-op, because
    nothing is being dropped and there is nothing to trade.
    """
    from app.webhook import _mmr_rerank

    rows = [
        ("packages/checkout/src/client.ts", "c", 0.95),
        ("packages/checkout/src/cart.ts", "c", 0.94),
        ("packages/checkout/src/order.ts", "c", 0.93),
        ("packages/checkout/src/pay.ts", "c", 0.92),
        ("packages/checkout/src/tax.ts", "c", 0.91),
        ("packages/reporting/src/summary.ts", "c", 0.70),
    ]

    # 1. breadth: the lone reporting file survives despite the lowest strength
    picked = _mmr_rerank(rows, limit=3)
    paths = [p for p, _c, _s in picked]
    assert len(picked) == 3, f"cap not applied: {len(picked)}"
    assert any("reporting" in p for p in paths), (
        f"all {len(paths)} slots went to one package: {paths}. The reporting "
        f"reviewer is never told the contract changed."
    )

    # 2. the strongest candidate is never displaced
    assert paths[0] == "packages/checkout/src/client.ts", (
        f"MMR displaced the strongest match; first pick was {paths[0]!r}. A "
        f"diversity pass that can drop the best candidate is a bug.")

    # 3. below the cap it is a no-op -- nothing is dropped, so there is no trade
    few = rows[:2]
    assert _mmr_rerank(few, limit=5) == sorted(few, key=lambda r: r[2], reverse=True), (
        "MMR reordered a set smaller than the cap. With nothing being dropped it "
        "must not second-guess the strength ordering.")

    # 4. lambda=1.0 disables diversity entirely and reproduces pure strength order
    pure = _mmr_rerank(rows, limit=3, lambda_=1.0)
    assert [p for p, _c, _s in pure] == [r[0] for r in rows[:3]], (
        f"lambda=1.0 must fall back to strength order, got "
        f"{[p for p, _c, _s in pure]}")


def test_admission_applies_mmr_before_returning():
    """The rerank has to sit inside the admission helper, not beside it.

    Both platforms call `_admit_consumers` and neither should have to remember a
    second step -- that is how the diff contract ended up wired in one caller and
    bypassed by another. Placing it at the single convergence point means adding a
    platform cannot forget it.
    """
    import inspect
    from app.webhook import _admit_consumers

    src = inspect.getsource(_admit_consumers)
    assert "_mmr_rerank" in src, (
        "_admit_consumers returns a raw strength-ordered slice, so the diversity "
        "pass is something each caller must remember -- the shape that let the "
        "line-based fallback bypass the diff contract.")
    assert "scored[:max_consumers]" not in src, (
        "_admit_consumers still cuts directly, so _mmr_rerank cannot be deciding "
        "which candidates survive the cap.")


def test_every_add_pattern_caller_persists():
    """A caller that mutates the store must also write it to the volume.

    add_pattern() deliberately does NOT save: ingest_examples() folds a whole
    corpus in a loop, and persisting per row would turn one ingest into hundreds
    of writes. So persistence is the caller's job -- which makes it a step
    someone must remember, and the first new caller written after the learning
    loop landed forgot it immediately. Its in-memory count read 2 while the
    volume held 1, and the read-back assertion passed against the stale row.

    Nothing was lost in production -- all four learning call sites do save -- but
    an outcome that accumulates in memory and not on disk is exactly the failure
    the /app/data volume was mounted to fix, and it would be invisible until a
    redeploy. Same reasoning as the signature gate: prove the call WORKS, not
    merely that it exists.

    BATCH_METHODS are exempt because their own caller saves; adding a name here
    is a deliberate, reviewable act rather than a silent omission.
    """
    import ast
    import pathlib

    BATCH_METHODS = {
        # (file, function): why the save belongs to its caller
        ("app/rag_store.py", "ingest_examples"):
            "folds N examples in a loop; _resolve_store()/callers persist once",
    }

    offenders = []
    for path in ("app/rag_store.py", "app/rag_retriever.py", "app/webhook.py",
                 "tools/verify_learning_loop.py"):
        p = pathlib.Path(__file__).parent.parent / path
        if not p.exists():
            continue
        tree = ast.parse(p.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)]
            names = {n.func.attr for n in calls if isinstance(n.func, ast.Attribute)}
            if "add_pattern" not in names:
                continue
            if (path, node.name) in BATCH_METHODS:
                continue
            if "save" not in names:
                offenders.append(f"{path}:{node.name}")

    assert not offenders, (
        "these functions call add_pattern() without save(), so the mutation lives "
        "only in memory and vanishes on redeploy:\n    "
        + "\n    ".join(offenders)
        + "\n  Either call rag_store.save() in the same function, or -- if a "
          "caller persists on your behalf -- add the name to BATCH_METHODS with "
          "the reason.")


def test_a_missing_signal_scores_zero_not_a_bonus():
    """Absence of evidence must not out-rank evidence.

    The scorer gave +0.1 when a pattern had no merge/reject data and +0.05 when
    it had no last_used -- so a corpus guess that had never been tried scored
    0.90, exactly tying a pattern with a real 50% merge rate, and beat a pattern
    with a poor-but-real record. That is the same shape as KiroCrew's un-embedded
    row keeping an unweighted keyword score: a signal you do not have cannot be
    a reason to rank higher.

    Ordering must be strict: proven > mixed > untried.
    """
    import time as _t
    from app.rag_retriever import _multi_signal_score
    from app.rag_store import FixPattern

    now = _t.time()

    def pat(**kw):
        base = dict(pattern_id="p", change_type="field_removed",
                    language="typescript", field_name="f", strategy="s",
                    last_used=now)
        base.update(kw)
        return FixPattern(**base)

    proven = _multi_signal_score(pat(merge_count=5), "field_removed",
                                 "typescript", None)
    mixed = _multi_signal_score(pat(merge_count=1, reject_count=1),
                                "field_removed", "typescript", None)
    untried = _multi_signal_score(pat(), "field_removed", "typescript", None)
    no_recency = _multi_signal_score(pat(merge_count=5, last_used=0.0),
                                     "field_removed", "typescript", None)

    # A MARGIN, not a bare >. _multi_signal_score reads time.time() itself on
    # every call, so the call made first gets marginally more recency -- when
    # this test was written with a bare `mixed > untried` it PASSED by 4e-13
    # against the un-fixed scorer, purely from evaluation order, and would have
    # flipped if the two lines were swapped. The evidence term is weighted 0.2,
    # so a real gap between a 50% record and no record is 0.1.
    MARGIN = 0.05
    assert proven - mixed >= MARGIN, (
        f"a 100% record must outrank a 50% one by more than float noise: "
        f"proven={proven:.4f} mixed={mixed:.4f}")
    assert mixed - untried >= MARGIN, (
        f"evidence must strictly order patterns, got mixed={mixed:.4f} "
        f"untried={untried:.4f} (gap {mixed - untried:.2e}) -- an untried "
        f"pattern is being paid a consolation bonus for having no record")
    assert proven - no_recency >= MARGIN, (
        f"a pattern with no last_used scored {no_recency:.4f} against "
        f"{proven:.4f} for the same pattern with a timestamp -- missing "
        f"recency must contribute 0.0, not a consolation bonus")


def test_a_pattern_that_never_worked_is_refused_not_merely_ranked_down():
    """Evidence must be able to VETO, because it cannot outweigh identity.

    change_type (0.4) + language (0.25) + the field-name boost (0.15) = 0.80,
    already above the 0.7 retrieval floor, with zero evidence and zero recency.
    The evidence term is weighted 0.2, so no track record however bad can pull an
    identity match below the floor: a pattern rejected five times and never
    merged was still retrieved and used to generate a fix.

    So applicability and trust are separate questions -- the same split Stage 4
    made for consumers, where admission is a correctness property and ranking is
    a preference. A pattern that has been tried and has never once merged is not
    a low-ranked candidate; it is a known-bad one.
    """
    import time as _t
    from app.rag_retriever import retrieve_fix_pattern
    from app.rag_store import FixPattern, PatternStore

    now = _t.time()

    def store_with(**kw):
        base = dict(pattern_id="p", change_type="field_removed",
                    language="typescript", field_name="phoneNumber",
                    strategy="s", last_used=now)
        base.update(kw)
        st = PatternStore("test_veto")
        st._loaded = True
        st.patterns = [FixPattern(**base)]
        st.structured_patterns = []
        return st

    never_worked = retrieve_fix_pattern(
        "field_removed", "typescript", "phoneNumber",
        store=store_with(merge_count=0, reject_count=5))
    assert never_worked is None, (
        "a pattern with 0 merges and 5 rejections was retrieved -- it has been "
        f"tried five times and never once worked, got {never_worked}")

    # One merge keeps it in the running: a mixed record is a ranking question.
    mixed = retrieve_fix_pattern(
        "field_removed", "typescript", "phoneNumber",
        store=store_with(merge_count=1, reject_count=5))
    assert mixed is not None, (
        "a pattern with a poor but non-zero merge record must stay retrievable "
        "-- vetoing on a ratio would invent a threshold; vetoing on 'never once "
        "worked' states a fact")


def test_an_aged_out_pattern_is_archived_not_deleted():
    """Old patterns leave retrieval but their evidence is retained.

    A pattern that worked in March against a codebase that has since changed
    should not outrank a fresh one, and the 0.15 recency weight cannot achieve
    that on its own (see the veto test -- identity alone clears the floor).
    KiroCrew runs active -> stale -> archived with pin exemptions; this is the
    archived step.

    Deleting would destroy the outcome evidence that took a real merged PR to
    earn, so archived rows stay on disk and stay in stats.
    """
    import time as _t
    from app.rag_store import FixPattern, PatternStore, ARCHIVE_AFTER_DAYS
    from app.rag_retriever import retrieve_fix_pattern

    now = _t.time()
    ancient = now - (ARCHIVE_AFTER_DAYS + 10) * 86400

    st = PatternStore("test_archive")
    st._loaded = True
    st.structured_patterns = []
    st.patterns = [FixPattern(
        pattern_id="old", change_type="field_removed", language="typescript",
        field_name="phoneNumber", strategy="s", merge_count=3,
        last_used=ancient, provenance="merged_clean")]

    got = retrieve_fix_pattern("field_removed", "typescript", "phoneNumber",
                               store=st)
    assert got is None, (
        f"a pattern last used {ARCHIVE_AFTER_DAYS + 10} days ago was retrieved: "
        f"{got}")
    assert len(st.patterns) == 1, (
        "the aged pattern was DELETED -- archiving must retain the row so the "
        "merged-PR evidence that earned it is not destroyed")


def test_a_human_correction_is_pinned_and_never_ages_out():
    """The top of the ladder does not expire.

    KiroCrew exempts pinned memories from decay and cap eviction. Ripple's
    analogue is provenance == "human_edit": a reviewer's correction is the
    highest-authority thing in the store, and letting it age out would mean the
    store forgets exactly what it was most sure of -- and, worse, would reopen
    the slot to the inferred write the ladder exists to block.
    """
    import time as _t
    from app.rag_store import FixPattern, PatternStore, ARCHIVE_AFTER_DAYS
    from app.rag_retriever import retrieve_fix_pattern

    now = _t.time()
    ancient = now - (ARCHIVE_AFTER_DAYS * 10) * 86400

    st = PatternStore("test_pin")
    st._loaded = True
    st.structured_patterns = []
    st.patterns = [FixPattern(
        pattern_id="human", change_type="field_removed", language="typescript",
        field_name="phoneNumber", strategy="a human fixed this",
        merge_count=1, last_used=ancient, provenance="human_edit")]

    got = retrieve_fix_pattern("field_removed", "typescript", "phoneNumber",
                               store=st)
    assert got is not None, (
        "a human correction aged out of retrieval -- human_edit is pinned")


def test_the_pattern_cap_archives_the_oldest_and_spares_human_edits():
    """A bounded store must evict by archiving, and must not evict a human.

    Without a cap the store grows without limit on the mounted volume -- the
    same failure pr_ledger caps at 5000 rows. Eviction takes the oldest
    last_used first, and skips human_edit rows entirely, so a busy install
    cannot silently discard the corrections it was most confident about.
    """
    import time as _t
    from app.rag_store import FixPattern, PatternStore, MAX_ACTIVE_PATTERNS

    now = _t.time()
    st = PatternStore("test_cap")
    st._loaded = True
    st.structured_patterns = []

    # One pinned human correction, deliberately the OLDEST row in the store.
    st.patterns = [FixPattern(
        pattern_id="human", change_type="field_removed", language="typescript",
        field_name="human_field", strategy="human", merge_count=1,
        last_used=now - 9_000_000, provenance="human_edit")]

    for i in range(MAX_ACTIVE_PATTERNS + 25):
        st.add_pattern(FixPattern(
            pattern_id=f"p{i}", change_type="field_removed",
            language="typescript", field_name=f"field{i}",
            strategy="s", merge_count=1, last_used=now - i,
            provenance="merged_clean"))

    assert len(st.patterns) <= MAX_ACTIVE_PATTERNS, (
        f"store holds {len(st.patterns)} active patterns, cap is "
        f"{MAX_ACTIVE_PATTERNS} -- unbounded growth on the volume")
    assert st.archived, (
        "patterns were evicted with no archive -- outcome evidence that took "
        "real merged PRs to earn was destroyed")
    assert any(p.provenance == "human_edit" for p in st.patterns), (
        "the human correction was evicted despite being pinned -- it was the "
        "oldest row, which is exactly the case the pin exists for")
    assert not any(p.provenance == "human_edit" for p in st.archived), (
        "a human_edit row was archived; pinned rows are exempt from the cap")


def test_archived_patterns_survive_a_reload():
    """Archive-not-delete is worthless if the archive is not persisted."""
    import json
    import os
    import tempfile
    import time as _t

    scratch = tempfile.mkdtemp(prefix="ripple_archive_persist_")
    old = os.environ.get("RIPPLE_DATA_DIR")
    os.environ["RIPPLE_DATA_DIR"] = scratch
    try:
        import importlib

        import app.rag_store as rs
        importlib.reload(rs)

        st = rs.PatternStore("persist_check")
        st._loaded = True
        st.patterns = []
        st.archived = [rs.FixPattern(
            pattern_id="gone", change_type="field_removed",
            language="typescript", field_name="f", strategy="s",
            merge_count=7, last_used=_t.time(), provenance="merged_clean")]
        st.save()

        raw = json.loads((st._path).read_text())
        assert "archived" in raw, (
            "save() dropped the archive, so every archived row is deleted on "
            "the next write -- archive-not-delete in name only")

        fresh = rs.PatternStore("persist_check")
        fresh.load()
        assert len(fresh.archived) == 1, (
            f"archive did not survive a reload, got {len(fresh.archived)} rows")
        assert fresh.archived[0].merge_count == 7, (
            "the archived row lost its evidence on the round trip")
    finally:
        if old is None:
            os.environ.pop("RIPPLE_DATA_DIR", None)
        else:
            os.environ["RIPPLE_DATA_DIR"] = old
        import importlib

        import app.rag_store as rs2
        importlib.reload(rs2)
        import shutil
        shutil.rmtree(scratch, ignore_errors=True)


def test_a_dormant_pattern_revives_when_a_new_merge_arrives():
    """Dormancy is derived at query time, so it must be reversible.

    The docstring on is_admissible() claims a dormant pattern revives on a new
    merge. That claim needs a test: yesterday a gate's PASS message asserted
    "ladder held" while never attempting the write the ladder blocks, and a
    mutation sailed through it. A property asserted only in prose is not held.

    Reversibility is the reason dormancy stays derived instead of physically
    moving rows: a pattern goes quiet because nothing needed it, not because it
    was ever wrong, and the evidence it carries is still valid the moment the
    same change_type/language/field comes back.
    """
    import time as _t
    from app.rag_store import (ARCHIVE_AFTER_DAYS, FixPattern, PatternStore,
                               is_admissible)
    from app.rag_retriever import retrieve_fix_pattern

    now = _t.time()
    st = PatternStore("test_revive")
    st._loaded = True
    st.structured_patterns = []
    st.patterns = [FixPattern(
        pattern_id="dormant", change_type="field_removed", language="typescript",
        field_name="phoneNumber", strategy="s", merge_count=3,
        last_used=now - (ARCHIVE_AFTER_DAYS + 30) * 86400,
        provenance="merged_clean")]

    assert retrieve_fix_pattern("field_removed", "typescript", "phoneNumber",
                                store=st) is None, "should start dormant"
    assert st.patterns, (
        "the dormant row left `patterns`, so it can never revive -- dormancy "
        "must not physically evict")

    # A new clean merge for the same identity lands via the normal write path.
    st.add_pattern(FixPattern(
        pattern_id="dormant", change_type="field_removed", language="typescript",
        field_name="phoneNumber", strategy="s", merge_count=1,
        last_used=_t.time(), provenance="merged_clean"))

    ok, why = is_admissible(st.patterns[0])
    assert ok, f"pattern did not revive after a fresh merge: {why}"
    revived = retrieve_fix_pattern("field_removed", "typescript", "phoneNumber",
                                   store=st)
    assert revived is not None, "revived pattern is still not retrievable"
    assert revived[0].merge_count == 4, (
        f"revival lost the accumulated evidence, merge_count="
        f"{revived[0].merge_count} (expected 3 dormant + 1 new)")


def test_admission_refusals_are_logged_not_silent():
    """A refused pattern must be distinguishable from one that never matched.

    `consumers[:5]` dropped real consumers for months with no log, no metric and
    no way to notice, because a candidate that was never fetched leaves no
    trace. Admission refusal has the same hazard: skipping a pattern silently
    looks exactly like having no pattern.
    """
    import time as _t
    from app.rag_store import FixPattern, PatternStore
    from app.rag_retriever import recent_refusals, retrieve_fix_pattern

    st = PatternStore("test_refusal_log")
    st._loaded = True
    st.structured_patterns = []
    st.patterns = [FixPattern(
        pattern_id="never_worked", change_type="field_removed",
        language="typescript", field_name="phoneNumber", strategy="s",
        merge_count=0, reject_count=4, last_used=_t.time())]

    before = len(recent_refusals(500))
    retrieve_fix_pattern("field_removed", "typescript", "phoneNumber", store=st)
    after = recent_refusals(500)

    assert len(after) > before, (
        "a pattern was refused admission with nothing recorded -- the refusal "
        "is invisible to the dashboard and to anyone debugging why no pattern "
        "was used")
    assert any("never merged" in (r.get("reason") or "") for r in after[-3:]), (
        f"the refusal reason does not say why, got {after[-3:]}")


def test_the_store_constants_still_match_their_recorded_derivation():
    """A documented number that drifts from the code is a stale claim.

    Both constants were guesses until they were measured, and the measurement is
    recorded in the comment block above each one. That block is prose: nothing
    stops someone restoring a round number and leaving the derivation asserting
    a different value, which is how "148 tests" survived in the README after the
    count changed.

    Two things are cross-checkable without re-running the measurement (which
    needs PropBench, absent in CI -- gating on it would be a gate that never
    runs):

      the documented p90 must equal ARCHIVE_AFTER_DAYS
      MAX_ACTIVE_PATTERNS must equal pr_ledger._MAX_ROWS, which is the stated
      reason it is 5000 rather than a number of its own
    """
    import pathlib
    import re

    from app import pr_ledger
    from app.rag_store import ARCHIVE_AFTER_DAYS, MAX_ACTIVE_PATTERNS

    src = (pathlib.Path(__file__).parent.parent / "app" / "rag_store.py").read_text()

    m = re.search(r"p90\s+(\d+)d", src)
    assert m, ("the ARCHIVE_AFTER_DAYS rationale no longer records a measured "
               "p90 -- if the measurement was dropped, the constant is a guess "
               "again and should say so")
    documented_p90 = int(m.group(1))
    assert documented_p90 == ARCHIVE_AFTER_DAYS, (
        f"ARCHIVE_AFTER_DAYS is {ARCHIVE_AFTER_DAYS} but its rationale records a "
        f"measured p90 of {documented_p90} -- one of the two is stale. Re-run "
        f"tools/measure_store_constants.py rather than editing the prose.")

    assert MAX_ACTIVE_PATTERNS == pr_ledger._MAX_ROWS, (
        f"MAX_ACTIVE_PATTERNS={MAX_ACTIVE_PATTERNS} and "
        f"pr_ledger._MAX_ROWS={pr_ledger._MAX_ROWS} have diverged. The measured "
        f"finding was that cost does not bind below ~25k rows, so the cap exists "
        f"to bound growth and is deliberately ONE number across both persisted "
        f"stores. If they should now differ, say why in both docstrings.")


def test_one_pr_carrying_two_consumers_keeps_both():
    """Ripple opens ONE PR per repo covering ALL of that repo's consumers.

    `_create_fix_pr` is called once per consumer file, but the branch name is
    derived from the FIELD, not the consumer -- so the second consumer hits the
    existing branch, appends a commit, and returns the SAME pr_url. Live proof:
    billing-api PR #5 is 2 commits / 2 files / 1 PR.

    record_open() keyed `_rows[pr_url] = {...}`, so N consumers on one PR wrote N
    rows to one key and only the LAST survived. Every other consumer was silently
    dropped, which then made contamination read the merge as "N files changed but
    Ripple proposed 1".

    Merging rather than overwriting is the fix, and it keeps the three per-consumer
    call sites untouched -- restructuring the loops to accumulate first would put
    the same requirement in three places, which is how the diff contract ended up
    wired in one caller and bypassed by another.
    """
    import os
    import shutil
    import tempfile

    scratch = tempfile.mkdtemp(prefix="ripple_ledger_multi_")
    old = os.environ.get("RIPPLE_DATA_DIR")
    os.environ["RIPPLE_DATA_DIR"] = scratch
    try:
        import importlib

        import app.pr_ledger as led
        importlib.reload(led)

        url = "https://github.com/acme/billing-api/pull/9101"
        common = dict(pattern_id="", source="template",
                      change_type="removed_field", language="typescript",
                      field_name="phoneNumber", repo="acme/billing-api",
                      validated=True, level="AUTO")

        led.record_open(url, consumer_file="packages/reporting/src/summary.ts",
                        **common)
        led.record_open(url, consumer_file="packages/checkout/src/client.ts",
                        **common)

        row = led.lookup(url)
        assert row is not None, "row vanished entirely"

        proposed = row.get("proposed_files")
        assert proposed is not None, (
            "the row records no proposed_files, so a multi-consumer PR still "
            "collapses to whichever consumer happened to be written last")
        assert set(proposed) == {
            "packages/reporting/src/summary.ts",
            "packages/checkout/src/client.ts",
        }, f"both consumers must survive, got {proposed}"

        # One row per PR, not one per consumer -- the key is the PR.
        assert len(led._rows) == 1, (
            f"expected exactly 1 ledger row for 1 PR, got {len(led._rows)}")

        # Identity fields must not drift between merges of the same PR.
        assert row["field_name"] == "phoneNumber"
        assert row["repo"] == "acme/billing-api"
        assert row["source"] == "template"

        # Back-compat: consumer_file still present for readers that expect one.
        assert row.get("consumer_file") in proposed, (
            "consumer_file must remain a real member of proposed_files")

        # And it survives a reload, since attribution happens days later.
        importlib.reload(led)
        reloaded = led.lookup(url)
        assert reloaded and set(reloaded.get("proposed_files") or []) == set(proposed), (
            "proposed_files did not survive a reload, so a PR merged tomorrow "
            "cannot be attributed")
    finally:
        if old is None:
            os.environ.pop("RIPPLE_DATA_DIR", None)
        else:
            os.environ["RIPPLE_DATA_DIR"] = old
        import importlib

        import app.pr_ledger as led2
        importlib.reload(led2)
        shutil.rmtree(scratch, ignore_errors=True)


def test_a_real_pattern_id_is_not_lost_to_a_later_template_write():
    """Merging must not let a later write erase a real pattern_id with "".

    Consumers of the same field can be generated by different paths -- one from a
    stored pattern, another falling back to the template. `_pattern_id_from_
    explanation` returns "" for template, and a plain dict update would let that
    empty string overwrite the real id, silently making the merge unattributable
    to the pattern that actually produced part of it.

    Same shape as the provenance ladder: absence must never outrank presence.
    """
    import os
    import shutil
    import tempfile

    scratch = tempfile.mkdtemp(prefix="ripple_ledger_pid_")
    old = os.environ.get("RIPPLE_DATA_DIR")
    os.environ["RIPPLE_DATA_DIR"] = scratch
    try:
        import importlib

        import app.pr_ledger as led
        importlib.reload(led)

        url = "https://github.com/acme/billing-api/pull/9102"
        common = dict(source="template", change_type="removed_field",
                      language="typescript", field_name="phoneNumber",
                      repo="acme/billing-api", validated=True, level="AUTO")

        led.record_open(url, pattern_id="abc123def4567890",
                        consumer_file="a.ts", **common)
        led.record_open(url, pattern_id="", consumer_file="b.ts", **common)

        row = led.lookup(url)
        assert row["pattern_id"] == "abc123def4567890", (
            f"a later empty pattern_id erased the real one, got "
            f"{row['pattern_id']!r} -- the merge is now unattributable")
    finally:
        if old is None:
            os.environ.pop("RIPPLE_DATA_DIR", None)
        else:
            os.environ["RIPPLE_DATA_DIR"] = old
        import importlib

        import app.pr_ledger as led2
        importlib.reload(led2)
        shutil.rmtree(scratch, ignore_errors=True)


def _s2_row(files, **over):
    """A provenance row shaped like pr_ledger writes one, for Stage 2 tests."""
    row = {
        "pattern_id": "", "source": "template", "change_type": "removed_field",
        "language": "typescript", "field_name": "phoneNumber",
        "consumer_file": files[0] if files else "",
        "proposed_files": list(files),
        "repo": "acme/billing-api", "validated": True, "level": "AUTO",
        "opened_at": 0.0,
    }
    row.update(over)
    return row


def _s2_pr(commits, changed, number=9200):
    return {
        "html_url": f"https://github.com/acme/billing-api/pull/{number}",
        "number": number, "merged": True, "state": "closed",
        "commits": commits, "changed_files": changed,
        "user": {"login": "ripple-api[bot]"},
        "base": {"repo": {"full_name": "acme/billing-api"}},
    }


def test_a_multi_consumer_clean_merge_is_not_contaminated():
    """The common case must be learnable.

    Ripple opens ONE PR per repo covering ALL its consumers, one commit per
    consumer file -- billing-api PR #5 is 2 commits / 2 files / 1 PR. The
    predicate hardcoded `changed_files > 1` and `commits > 2` against an
    assumption of one file per PR, and said so in its own message: "N files
    changed but Ripple proposed 1".

    So every real multi-consumer merge was classified contaminated and derived
    no pattern. The store could never gain a row, which is why it still holds
    zero after the whole learning loop was built and deployed.

    The bound is now the SET Ripple actually proposed.
    """
    from app.webhook import _outcome_is_contaminated

    two = ["packages/reporting/src/summary.ts", "packages/checkout/src/client.ts"]

    bad, why = _outcome_is_contaminated(_s2_pr(commits=2, changed=2),
                                        _s2_row(two))
    assert not bad, (
        f"a 2-commit / 2-file merge of a PR that proposed 2 files was called "
        f"contaminated: {why}")

    # Three consumers, three commits, three files -- still clean.
    three = two + ["packages/notify/src/sms.ts"]
    bad, why = _outcome_is_contaminated(_s2_pr(commits=3, changed=3),
                                        _s2_row(three))
    assert not bad, f"a 3-consumer clean merge was called contaminated: {why}"

    # Fewer files than proposed is fine -- one fix may have been a no-op.
    bad, why = _outcome_is_contaminated(_s2_pr(commits=2, changed=1),
                                        _s2_row(two))
    assert not bad, f"changed < proposed must not be contamination: {why}"


def test_edits_beyond_the_proposed_set_are_still_contaminated():
    """Loosening the bound must not remove the property it was protecting."""
    from app.webhook import _outcome_is_contaminated

    two = ["packages/reporting/src/summary.ts", "packages/checkout/src/client.ts"]

    bad, why = _outcome_is_contaminated(_s2_pr(commits=2, changed=7),
                                        _s2_row(two))
    assert bad and "file" in why.lower(), (
        f"7 files changed against 2 proposed must be contaminated, got {why!r}")

    bad, why = _outcome_is_contaminated(_s2_pr(commits=9, changed=2),
                                        _s2_row(two))
    assert bad and "commit" in why.lower(), (
        f"9 commits against 2 proposed must be contaminated, got {why!r}")

    # THE TWO PREDICATES HAVE DIFFERENT COMMIT TOLERANCES, on purpose.
    #
    # `_pr_had_human_edits` fires at > expected: Ripple pushes one commit per
    # consumer file, so anything beyond that is someone else. Contamination
    # tolerates ONE more, because a single reviewer follow-up touching only the
    # proposed files is still attributable -- that file's merged state IS the
    # corrected fix, which is what makes a human_edit outcome worth recording at
    # all. Collapsing the two tolerances would mean every human correction became
    # unlearnable, which is the opposite of the ladder's intent.
    one = ["packages/reporting/src/summary.ts"]
    bad, _ = _outcome_is_contaminated(_s2_pr(commits=2, changed=1),
                                      _s2_row(one))
    assert not bad, (
        "1 Ripple commit + 1 reviewer follow-up on the one proposed file must "
        "stay attributable, or provenance can never rise to human_edit")

    bad, why = _outcome_is_contaminated(_s2_pr(commits=3, changed=1),
                                        _s2_row(one))
    assert bad, (
        f"two follow-up commits on one proposed file is a pile-up we cannot "
        f"split into fix and not-fix, got {why!r}")

    # Absent data stays conservative -- "could not check" is not "it is fine".
    for pr, label in ((_s2_pr(commits=None, changed=2), "commits absent"),
                      (_s2_pr(commits=2, changed=None), "changed_files absent")):
        bad, why = _outcome_is_contaminated(pr, _s2_row(two))
        assert bad, f"{label} must read as contaminated, got {why!r}"

    bad, why = _outcome_is_contaminated(_s2_pr(commits=1, changed=1), None)
    assert bad, "a PR with no provenance row must be contaminated"

    # The bound itself, asserted directly. Both callers early-return when the row
    # is absent, so `_proposed_count(None)` is defensive-only -- and a mutation
    # making it return 999 passed every other test precisely because nothing
    # reached it. An unreachable branch with no contract is a trap for the next
    # caller, who will reach it.
    from app.webhook import _proposed_count

    assert _proposed_count(None) == 1, (
        "an absent row must yield the STRICTEST bound, not an unbounded one")
    assert _proposed_count({}) == 1, "an empty row must yield the strictest bound"
    assert _proposed_count({"proposed_files": []}) == 1, (
        "an empty proposed_files list must not mean 'unbounded'")
    assert _proposed_count({"proposed_files": ["a.ts", "b.ts"]}) == 2
    assert _proposed_count({"consumer_file": "a.ts"}) == 1, (
        "a legacy row predating proposed_files must fall back to 1")

    # A row predating proposed_files must behave like the old single-file bound,
    # not like an unbounded one.
    legacy = _s2_row(["a.ts"])
    legacy.pop("proposed_files")
    bad, why = _outcome_is_contaminated(_s2_pr(commits=1, changed=2), legacy)
    assert bad and "file" in why.lower(), (
        f"a legacy row with no proposed_files must fall back to a FILE bound of "
        f"1, not to unlimited, got {why!r}")


def test_human_edit_detection_is_bounded_by_the_proposed_set():
    """One commit per consumer file is Ripple's own shape, not a human's."""
    from app.webhook import _pr_had_human_edits

    two = ["a.ts", "b.ts"]
    app_only = ["ripple-api[bot]", "ripple-api[bot]"]

    assert not _pr_had_human_edits(_s2_pr(commits=2, changed=2),
                                   _s2_row(two), app_only), (
        "2 App commits on a PR that proposed 2 files is Ripple's normal shape")

    assert _pr_had_human_edits(_s2_pr(commits=5, changed=2),
                               _s2_row(two), app_only), (
        "5 commits against 2 proposed files means someone added commits")

    assert _pr_had_human_edits(_s2_pr(commits=2, changed=2), _s2_row(two),
                               ["ripple-api[bot]", "a-human"]), (
        "a non-App commit author must read as a human edit even when the "
        "commit COUNT looks like Ripple's own shape -- a rebase or amend keeps "
        "the count and changes the author")

    assert _pr_had_human_edits(_s2_pr(commits=2, changed=2),
                               _s2_row(two), None), (
        "unknown authors must default to 'a human touched it' -- same "
        "direction as validated=None -> REVIEW")


def test_the_commit_authors_are_actually_fetched_not_read_from_the_payload():
    """The author check must be LIVE, which it was not.

    `_pr_had_human_edits` read `pr["commit_list"]`, and grepping the repo showed
    that key is written ONLY by tests -- nothing in production populates it and
    the handler never called /pulls/{n}/commits. So the author half of the check
    was dead code, and `commits != 1` was the only guard actually running.

    That matters precisely because Stage 2 loosens the count to
    `commits > len(proposed_files)`: without a live author check, a human who
    amends or rebases Ripple's commits keeps the count intact and would be
    credited as a clean merge. Loosening the count while the author check was
    dead would have made the system LESS safe, not more.
    """
    import inspect
    import ast

    import pathlib

    from app import webhook

    assert hasattr(webhook, "_fetch_pr_commit_authors"), (
        "no _fetch_pr_commit_authors -- the author check has no source of "
        "authors in production")

    src = inspect.getsource(webhook._fetch_pr_commit_authors)
    assert "/commits" in src, (
        "_fetch_pr_commit_authors does not call the commits endpoint")

    # The fetch must happen at the single convergence point, so no caller has to
    # remember it -- the shape that kept the diff contract from being bypassed.
    terminal = inspect.getsource(webhook._record_pr_terminal)
    assert "_fetch_pr_commit_authors" in terminal, (
        "_record_pr_terminal does not fetch commit authors, so the author check "
        "still has nothing to check")

    # And commit_list must no longer be READ: a payload-supplied author list is a
    # key a test can set and production never will. Checked against the AST rather
    # than the text, because naming it in a docstring that explains why it was
    # removed is documentation, not a live read -- a substring check here would
    # punish the comment that records the bug.
    whole = pathlib.Path(webhook.__file__).read_text()
    tree = ast.parse(whole)
    reads = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "commit_list"):
            reads.append(node.lineno)
        if (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
                and node.slice.value == "commit_list"):
            reads.append(node.lineno)
    assert not reads, (
        f"commit_list is still read as code at line(s) {reads} -- that key is "
        f"test-only, and reading it makes the author check look alive while "
        f"nothing in production populates it")

    # _pr_had_human_edits must take authors as an argument rather than digging
    # them out of the payload itself.
    params = list(inspect.signature(webhook._pr_had_human_edits).parameters)
    assert len(params) >= 3, (
        f"_pr_had_human_edits{tuple(params)} should receive (pr, row, authors) "
        f"so the fetch stays at one place and the predicate stays pure")

    # Every call site must pass all three -- a two-arg call would silently lose
    # the bound or the authors. This is the signature-bind lesson from Stage 2 of
    # the learning loop, where a real call raised TypeError on every invocation.
    tree = ast.parse(whole)
    sig = inspect.signature(webhook._pr_had_human_edits)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_pr_had_human_edits"):
            try:
                sig.bind(*[None] * len(node.args),
                         **{k.arg: None for k in node.keywords if k.arg})
            except TypeError as exc:
                raise AssertionError(
                    f"call to _pr_had_human_edits at line {node.lineno} does not "
                    f"match its signature: {exc}") from exc


if __name__ == "__main__":
    sys.exit(_main())
