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
      .yaml -> only rag_engine knew it, and the file filter rejected it anyway
      .kts  -> two of eight knew it"""
    from app import languages
    from app.rag_engine import _detect_language as rag
    from app.consumer_finder import _detect_language as cf
    from app.fix_generator_multi import detect_language as fgm
    from app.multi_step_reasoning import _detect_language as msr
    from app.rag_retriever import _language_from_path as rr
    from app.webhook import _detect_lang as wh, _is_code_file as wh_isfile
    from app.history_learner import HistoryLearner

    # Same function OBJECT: no wrapper can have been slipped in.
    for name, fn in (("rag_engine", rag), ("consumer_finder", cf),
                     ("fix_generator_multi", fgm), ("multi_step_reasoning", msr),
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


if __name__ == "__main__":
    sys.exit(_main())
