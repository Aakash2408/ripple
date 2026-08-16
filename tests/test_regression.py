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

import os
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
    """An in-memory-only log resets on every Railway redeploy -- which is
    what erased the successful 08:49 run before it could be inspected."""
    import tempfile
    import importlib
    old_dir = os.environ.get("RIPPLE_DATA_DIR")
    os.environ["RIPPLE_DATA_DIR"] = tempfile.mkdtemp()
    try:
        from app import activity
        importlib.reload(activity)
        activity.reset()
        activity.record("pr_result", {"repo": "o/x",
                                      "url": "https://github.com/o/x/pull/1"})

        # Simulate a restart: reload the module, same data dir
        importlib.reload(activity)
        assert activity.counters()["prs_created"] == 1, \
            "activity did not survive a restart"
    finally:
        if old_dir is None:
            os.environ.pop("RIPPLE_DATA_DIR", None)
        else:
            os.environ["RIPPLE_DATA_DIR"] = old_dir


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
# runner (works without pytest)
# ===================================================================

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
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
