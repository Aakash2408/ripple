"""Triage of every silent-failure path, as data rather than prose.

Stage 2 of the P0 plan. NO FIXES HERE -- the classification is the deliverable,
because a wrong call hides a defect, and a prose document would drift from the
code exactly as landing/index.html and changelog.html did.

Consumed by tools/audit_fail_silent.py once it becomes blocking: a site must be
classified, and a LEGITIMATE classification must carry a reason.

THREE BUCKETS

  LEGITIMATE   the failure is expected, the caller cannot act on it, and nothing
               downstream mistakes it for a successful empty result.
  NEEDS_SIGNAL the caller cannot distinguish "nothing there" from "failed to
               look". Correctness is not wrong today, but the outcome is
               invisible -- which is the failure mode this plan exists to remove.
  REAL_BUG     swallowing changes an answer. Fix, do not annotate.

THE ONE PATTERN WORTH NAMING
Three of the four REAL_BUGs are the same shape: an error class that conflates
ABSENT with UNREACHABLE. `except HTTPError: return ""` cannot tell 404 (the file
is not there) from 503 (we could not look). A caller reading "" concludes the
file does not exist, and for a spec fetch that means "no breaking changes". This
is the third appearance of that shape in this codebase -- it caused the
403-vs-404 cache poisoning in PropBench twice and a false published claim once.
"""
from __future__ import annotations

LEGITIMATE = "LEGITIMATE"
NEEDS_SIGNAL = "NEEDS_SIGNAL"
REAL_BUG = "REAL_BUG"

# (file, line, function) -> (bucket, reason)
TRIAGE: dict[tuple, tuple] = {

    # ---------------------------------------------------------------- REAL_BUG
    ("bitbucket_support.py", 82, "get_file"): (
        REAL_BUG,
        "`except HTTPError: return ''` conflates 404 (file absent) with "
        "401/403/429/503 (could not look). A caller reading '' concludes the "
        "spec does not exist, i.e. no breaking changes. Same shape as the "
        "403-vs-404 cache poisoning that hit PropBench twice."),
    ("proto_diff.py", 154, "_parse_reserved"): (
        REAL_BUG,
        "A malformed reserved RANGE is skipped, so those numbers are absent from "
        "reserved_numbers. _find_field_rename treats a reserved number as a "
        "deliberate removal -- so this can silently flip a REMOVAL into a "
        "RENAME, telling consumers to rename references to a field that is gone. "
        "Narrow trigger, high consequence, and newly load-bearing since field "
        "numbers became the rename signal."),
    ("proto_diff.py", 159, "_parse_reserved"): (
        REAL_BUG,
        "Same as line 154 for a single malformed reserved number."),
    ("jsonschema_diff.py", 30, "parse_json_schema"): (
        REAL_BUG,
        "A malformed JSON Schema returns an empty parse, which the differ reads "
        "as a schema with no properties -- so every field looks removed, or "
        "nothing looks changed, depending on which side failed. A parse failure "
        "and an empty schema must not be the same value."),

    # ------------------------------------------------------------ NEEDS_SIGNAL
    ("bitbucket_support.py", 83, "get_file"): (
        NEEDS_SIGNAL,
        "The empty return paired with the REAL_BUG handler at line 82. Listed "
        "separately because the audit counts the handler and the return as two "
        "sites, and fixing one without the other would leave the gate red."),
    ("avro_diff.py", 28, "parse_avro"): (
        NEEDS_SIGNAL,
        "Malformed .avsc returns empty; caller cannot tell it from an empty "
        "record. Lower risk than jsonschema only because Avro is stricter."),
    ("avro_diff.py", 29, "parse_avro"): (
        NEEDS_SIGNAL, "Empty return paired with the handler above."),
    ("jsonschema_diff.py", 31, "parse_json_schema"): (
        NEEDS_SIGNAL, "Empty return paired with the REAL_BUG handler at line 30."),
    ("custom_playbooks.py", 155, "parse_ripple_config"): (
        NEEDS_SIGNAL,
        "A malformed .ripple.yaml silently becomes the default config, so a "
        "customer's ignore rules and confidence threshold are dropped without "
        "anyone being told. Their settings appear not to work."),
    ("custom_playbooks.py", 156, "parse_ripple_config"): (
        NEEDS_SIGNAL, "Empty return paired with the handler above."),
    ("fix_generator.py", 50, "generate_fix"): (
        NEEDS_SIGNAL,
        "Cannot read the consumer file -> returns None -> no fix -> no PR. "
        "Indistinguishable from 'this file needed no fix', which is the exact "
        "silence the outcome enum in Stage 3 exists to remove."),
    ("fix_generator.py", 51, "generate_fix"): (
        NEEDS_SIGNAL, "Empty return paired with the handler above."),
    ("api_watcher.py", 182, "_fetch_spec"): (
        NEEDS_SIGNAL,
        "Spec fetch failure returns empty, so the watcher sees no change. Same "
        "absent-vs-unreachable shape as bitbucket_support, but the watcher is "
        "polling and will retry, so it self-heals."),
    ("api_watcher.py", 183, "_fetch_spec"): (
        NEEDS_SIGNAL, "Empty return paired with the handler above."),
    ("history_learner.py", 226, "_get_commits"): (
        NEEDS_SIGNAL,
        "git missing or timing out returns no commits, which reads as 'this repo "
        "has no co-change history'. The learner then reports low confidence for a "
        "reason that has nothing to do with the repo."),
    ("history_learner.py", 227, "_get_commits"): (
        NEEDS_SIGNAL, "Empty return paired with the handler above."),
    ("monorepo.py", 166, "_git_grep"): (
        NEEDS_SIGNAL,
        "Same as history_learner: absent git becomes 'no matches', so monorepo "
        "consumer discovery silently finds nothing."),
    ("monorepo.py", 167, "_git_grep"): (
        NEEDS_SIGNAL, "Empty return paired with the handler above."),
    ("slack_notify.py", 292, "_send_via_webhook"): (
        NEEDS_SIGNAL,
        "A dropped notification is invisible: the user believes they were told. "
        "Not a correctness bug, but the whole point of the notification is that "
        "someone finds out."),
    ("slack_notify.py", 293, "_send_via_webhook"): (
        NEEDS_SIGNAL, "Empty return paired with the handler above."),
    ("slack_notify.py", 316, "_send_via_bot_api"): (
        NEEDS_SIGNAL, "As _send_via_webhook."),
    ("slack_notify.py", 317, "_send_via_bot_api"): (
        NEEDS_SIGNAL, "Empty return paired with the handler above."),
    ("rag_engine.py", 168, "_load_from_disk"): (
        NEEDS_SIGNAL,
        "A corrupt pattern store silently becomes an EMPTY store, so RAG reports "
        "'no patterns' rather than 'the store is damaged'. The store has never "
        "had contents, which is why this has never bitten."),
    ("rag_store.py", 156, "save"): (
        NEEDS_SIGNAL,
        "A failed write loses learned patterns silently. Related to the "
        "durability work in Stage 1: persistence that fails quietly is worse "
        "than no persistence, because the counters keep rising."),
    ("rag_retriever.py", 37, "_resolve_store"): (
        NEEDS_SIGNAL,
        "Falls back to the module singleton without saying so, so per-org "
        "isolation can silently degrade to a shared store."),
    ("github_app_auth.py", 211, "get_installation_token"): (
        NEEDS_SIGNAL,
        "A malformed expires_at is swallowed, leaving whatever default was set. "
        "NOT VERIFIED: I did not confirm that default, so I am not claiming this "
        "is safe -- if it is far-future the token is used past expiry and every "
        "call 401s. Needs a look before Stage 4, not a guess."),
    ("multi_step_reasoning.py", 237, "resolve_fix_target"): (
        NEEDS_SIGNAL,
        "ValueError swallowed while resolving a target; the step reports no "
        "target rather than an unresolvable one."),
    ("multi_step_reasoning.py", 252, "resolve_fix_target"): (
        NEEDS_SIGNAL, "As line 237."),
    ("dry_run.py", 100, "dry_run_analysis"): (
        NEEDS_SIGNAL,
        "A user-facing endpoint swallowing Exception returns a clean-looking "
        "analysis for input it failed to process."),

    # -------------------------------------------------------------- LEGITIMATE
    ("dry_run.py", 37, "<module>"): (
        LEGITIMATE, "Optional-dependency import guard at module scope."),
    ("bitbucket_oauth.py", 34, "<module>"): (
        LEGITIMATE, "Optional-dependency import guard at module scope."),
    ("gitlab_oauth.py", 39, "<module>"): (
        LEGITIMATE, "Optional-dependency import guard at module scope."),
    ("gitlab_setup.py", 22, "<module>"): (
        LEGITIMATE, "Optional-dependency import guard at module scope."),
    ("rag_engine.py", 79, "__init__"): (
        LEGITIMATE,
        "sentence-transformers/chromadb are ~2GB and deliberately excluded from "
        "CI. The degradation is reported as rag_unavailable rather than hidden."),
    ("rag_engine.py", 80, "__init__"): (LEGITIMATE, "As line 79."),
    ("rag_engine.py", 91, "__init__"): (LEGITIMATE, "As line 79."),
    ("rag_engine.py", 458, "index_from_git"): (
        LEGITIMATE,
        "A single commit failing `git show` is skipped; indexing is best-effort "
        "over many commits and the total is reported."),
    ("rag_engine.py", 476, "index_from_git"): (LEGITIMATE, "As line 458."),
    ("rag_engine.py", 485, "index_from_git"): (LEGITIMATE, "As line 458."),
    ("rag_engine.py", 543, "index_single_commit"): (LEGITIMATE, "As line 458."),
    ("rag_engine.py", 552, "index_single_commit"): (LEGITIMATE, "As line 458."),
    ("api_watcher.py", 215, "_load_state"): (
        LEGITIMATE,
        "Corrupt watcher state resets to empty, which is the correct recovery: "
        "the next poll rebuilds it."),
    ("tls.py", 67, "resolve_ca_bundle"): (
        LEGITIMATE,
        "Candidate CA bundle locations are tried in order; tls.describe() "
        "reports which one was resolved, so the outcome is visible."),
    ("tls.py", 92, "make_ssl_context"): (LEGITIMATE, "As line 67."),
    ("webhook.py", 1727, "_retry_delay"): (
        LEGITIMATE,
        "An unparseable Retry-After header falls back to the computed backoff."),
    ("webhook.py", 2365, "_scan_repo_tree_for_consumers"): (
        LEGITIMATE,
        "UnicodeDecodeError on a binary blob skips that file. is_scannable() "
        "already excludes most, and a binary file is not a consumer."),
    ("consumer_finder.py", 91, "find_consumers"): (
        LEGITIMATE,
        "An unreadable file during the directory walk is skipped. Same rationale "
        "as the webhook's tree scan."),
    ("token_store.py", 38, "_find_store_dir"): (
        LEGITIMATE,
        "Candidate store directories are tried in order until one is writable."),
}


def counts() -> dict:
    out = {LEGITIMATE: 0, NEEDS_SIGNAL: 0, REAL_BUG: 0}
    for bucket, _ in TRIAGE.values():
        out[bucket] += 1
    return out


def by_bucket(bucket: str) -> list:
    return sorted(k for k, (b, _) in TRIAGE.items() if b == bucket)


if __name__ == "__main__":
    c = counts()
    print(f"  {sum(c.values())} sites triaged")
    for bucket in (REAL_BUG, NEEDS_SIGNAL, LEGITIMATE):
        print(f"    {bucket:<14} {c[bucket]}")
        for key in by_bucket(bucket):
            f, line, fn = key
            print(f"        {f}:{line}  {fn}")
