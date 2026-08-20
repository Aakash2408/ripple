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

        # Default: the real Anthropic API.
        importlib.reload(L)
        assert L.base_url() == "https://api.anthropic.com"
        assert L.is_anthropic() is True
        assert "Anthropic" in L.backend_label()

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


def test_llm_key_is_resolved_only_through_llm_config():
    """Two sites GATED on os.environ["ANTHROPIC_API_KEY"] while the call they
    guarded built its client from llm_config.api_key(). An LLM-gateway setup
    sets ANTHROPIC_AUTH_TOKEN and deliberately leaves ANTHROPIC_API_KEY unset,
    so both gates failed closed and fell back to the template -- while the
    caller believed the LLM had answered. Verified against a live LiteLLM
    proxy: 0 requests arrived until the gates were fixed.

    Pins the STRUCTURE, not the behaviour: any new direct read reintroduces the
    same silent divergence, and no unit test of either function would fail."""
    import os as _os
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    app = _os.path.join(root, "app")
    offenders = []
    for name in sorted(_os.listdir(app)):
        if not name.endswith(".py") or name == "llm_config.py":
            continue
        src = open(_os.path.join(app, name)).read()
        for pat in ('environ.get("ANTHROPIC_API_KEY")',
                    'environ["ANTHROPIC_API_KEY"]',
                    'getenv("ANTHROPIC_API_KEY")'):
            if pat in src:
                offenders.append(f"{name}: {pat}")
    assert not offenders, (
        "ANTHROPIC_API_KEY read outside llm_config -- use llm_config.api_key() "
        f"so ANTHROPIC_AUTH_TOKEN is honoured: {offenders}"
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
    # EXACTLY one. Not zero (the mechanism must be able to say yes) and not many
    # (breadth without proof is the thing being refused).
    assert ready == [("typescript", "openapi", "remove_field")], ready
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
    # readiness leaked across the matrix.
    assert cc.blocking_reasons("typescript", "proto", "remove_field")
    assert cc.blocking_reasons("python", "openapi", "remove_field")
    assert cc.blocking_reasons("typescript", "openapi", "rename_field")

    # And a language WITHOUT a runner still reports both.
    unwired = cc.blocking_reasons("go", "openapi", "remove_field")
    assert any("UNABLE_TO_VALIDATE" in r for r in unwired), unwired


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

    # And an unclaimed cell must not read as tested.
    # Stage 6: this cell IS now claimed, and the invariant above proves the named
    # test exists and is callable. What must still hold is that a cell WITHOUT a
    # fixture is not silently credited.
    assert e2e_tested("typescript", "openapi", "remove_field")
    assert E2E_FIXTURES[("typescript", "openapi", "remove_field")] == \
        "test_e2e_typescript_openapi_remove_field"
    assert not e2e_tested("python", "openapi", "remove_field")
    assert not e2e_tested("typescript", "proto", "remove_field")


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

    # 3. nothing is claimed yet
    from app.capability_claims import E2E_FIXTURES, e2e_tested
    assert e2e_tested("typescript", "openapi", "remove_field"), \
        "Stage 6 earned this claim; losing it means the evidence was dropped"
    assert len(E2E_FIXTURES) == 1, \
        f"one cell has end-to-end evidence, not {len(E2E_FIXTURES)}"


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
                                choose_backend, RUNNERS)

    # 1. A language with no runner makes no claim.
    for lang in ("python", "go", "rust", "cobol"):
        v = validate(lang, "/nonexistent")
        assert v.state is ValidationState.UNABLE_TO_VALIDATE, lang
        assert not v.is_valid
        assert "no validation runner" in v.reason

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

    for unwired in ("python", "go"):
        assert not VALIDATORS[unwired].is_wired, unwired
        assert not validates(unwired)
        assert validation_state(unwired) is ValidationState.UNABLE_TO_VALIDATE

    # A path that does not resolve must NOT count as wired.
    from app.capability_claims import ValidatorSpec
    assert not ValidatorSpec("x", "t", implemented_by="app.nope:missing").is_wired
    assert not ValidatorSpec("x", "t", implemented_by="app.validation:not_a_func").is_wired
    assert not ValidatorSpec("x", "t", implemented_by="no_colon_here").is_wired

    # 7. The superseded stub is gone, not merely frozen.
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert not os.path.exists(os.path.join(root, "app", "validated_fix.py"))
    assert "typescript" in RUNNERS and len(RUNNERS) == 1, \
        "only TypeScript has a real runner; adding a key here is a capability claim"

    # 8. choose_backend never invents one.
    backend, note = choose_backend()
    assert backend in ("", "docker", "host"), backend
    assert note


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
        with open(evidence_path, "w") as fh:
            json.dump({"ran_at": __import__("time").time(),
                       "cell": ["typescript", "openapi", "remove_field"],
                       "backend": after.evidence["backend"],
                       "typecheck_exit": after.evidence["typecheck_exit"],
                       "validated": True}, fh, indent=2)
    finally:
        _sh.rmtree(work_root, ignore_errors=True)


def test_auto_is_real_for_exactly_one_cell_and_unreachable_otherwise():
    """AUTO exists now. It must remain impossible to obtain without earning it.

    Before Stage 6 this was easy to guarantee, because AUTO was unreachable for all
    1800 cells -- nothing validated. Now exactly one cell reaches it, which is the
    first time the mechanism has had to distinguish rather than simply refuse. That
    makes this the load-bearing test of the whole plan.
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

    # 1. exactly the golden cell, and nothing else
    assert autos == [("typescript", "openapi", "remove_field")], autos
    assert counts["AUTO"] == 1 and counts["REVIEW"] == 1799, counts

    # 1b. AUTO REQUIRES A LIVE VALIDATION OF THIS PATCH. Registry evidence proves the
    #     CELL works -- that this combination has an end-to-end fixture which
    #     compiles. It says nothing about whether THIS patch, on THIS repository,
    #     compiles, and conflating the two is what AUTO used to rest on.
    golden = ("typescript", "openapi", "remove_field")
    for validated, expected in ((True, Level.AUTO),
                                (False, Level.REVIEW),
                                (None, Level.REVIEW)):
        d = pr_level(*golden, confidence=0.99, min_confidence=0.5,
                     validated=validated)
        assert d.level is expected, (
            f"validated={validated!r} produced {d.level.value}, wanted "
            f"{expected.value} -- 'we could not check' must never read as 'it is fine'")
    assert any("not validated" in r
               for r in pr_level(*golden, confidence=0.99, min_confidence=0.5,
                                 validated=None).reasons), \
        "an unvalidated fix is downgraded without saying why"
    assert any("did not typecheck" in r
               for r in pr_level(*golden, confidence=0.99, min_confidence=0.5,
                                 validated=False).reasons), \
        "a fix that failed the compiler is downgraded without saying why"

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
    from app.fix_templates import apply_fix_template, _LAST_TS_RESULT

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
    assert not (_LAST_TS_RESULT.get("edits") or []), \
        "edits were reported for a patch that was refused"
    assert any("diff contract" in str(r) for r in _LAST_TS_RESULT.get("refusals") or []), \
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
    key = ("fix_generator", "_generate_with_llm")
    assert key in R.FUNCTION_LAYERS, (
        f"{key} is not declared. The LLM branch is safety-relevant: it is the only "
        f"path on which customer source leaves the machine.")
    entry = R.FUNCTION_LAYERS[key]
    assert entry.get("role"), "the declaration has no stated role"
    if entry.get("reachable"):
        assert not entry.get("consequence"), (
            "a reachable layer must not still carry a consequence for being "
            "unreachable -- delete it and say what is true now")
    else:
        assert entry.get("consequence"), (
            "an unreachable layer must name its cost, or the declaration is just "
            "a note")


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

    clean = {"number": 1, "commits": 1,
             "commit_list": [{"author": {"login": "ripple-api[bot]"}}],
             "changed_files": 1}
    contaminated, reason = w._outcome_is_contaminated(clean, row)
    assert not contaminated, f"a clean single-commit merge was called contaminated: {reason}"

    for label, pr in (
        ("extra files touched", {"number": 2, "commits": 1, "changed_files": 7,
                                 "commit_list": [{"author": {"login": "ripple-api[bot]"}}]}),
        ("squash of many commits", {"number": 3, "commits": 6, "changed_files": 1,
                                    "commit_list": []}),
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

    result = w._handle_pr_merged(
        {"repository": {"full_name": "acme/api"}},
        {"number": 4242, "html_url": url, "commits": 1, "changed_files": 1,
         "commit_list": [{"author": {"login": "ripple-api[bot]"}}]})

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


if __name__ == "__main__":
    sys.exit(_main())
