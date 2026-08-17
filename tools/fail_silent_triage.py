"""Triage of every silent-failure path, as data rather than prose.

Stage 2 of the P0 plan classified these; Stage 4 fixed the real bugs; Stage 5
made `tools/audit_fail_silent.py --check` enforce the classification in CI.

WHAT THE GATE ENFORCES
Not "zero silent paths" -- 25 sites are correct-but-invisible and removing that
invisibility is P0.4/P0.5 work. What it enforces is that **no silent path is
unexplained**: every site the audit finds must be classified here with a reason,
no REAL_BUG may be left standing, and a site that was fixed may not come back.

WHY THE KEYS LOOK LIKE THAT
The first version of this file keyed on (file, LINE, function). It detached from
the code within one stage: Stage 3 added 25 lines to webhook.py, _retry_delay
moved 1727 -> 1752, and two LEGITIMATE classifications silently pointed at lines
that no longer existed. The dangerous direction is the other one -- a NEW silent
path landing on line 1727 would have inherited "LEGITIMATE" and been waved
through. The key is now (file, function, kind, caught, ordinal), which moves only
when the silent paths inside that one function actually change.

`caught` is part of the identity on purpose: widening `except ValueError` to
`except Exception` is a different swallow with a different blast radius, so it
forfeits the old classification instead of inheriting it.

THREE BUCKETS

  LEGITIMATE   the failure is expected, the caller cannot act on it, and nothing
               downstream mistakes it for a successful empty result.
  NEEDS_SIGNAL the caller cannot distinguish "nothing there" from "failed to
               look". Correctness is not wrong today, but the outcome is
               invisible -- which is the failure mode this plan exists to remove.
  REAL_BUG     swallowing changes an answer. Fix, do not annotate. The gate
               fails while any REAL_BUG remains, so this bucket is empty by
               construction; FIXED below records the four that were here.

THE ONE PATTERN WORTH NAMING
Three of the four REAL_BUGs were the same shape: an error class that conflates
ABSENT with UNREACHABLE. `except HTTPError: return ''` cannot tell 404 (the file
is not there) from 503 (we could not look). A caller reading "" concludes the
file does not exist, and for a spec fetch that means "no breaking changes". This
was the third appearance of that shape in this codebase -- it caused the
403-vs-404 cache poisoning in PropBench twice and a false published claim once.
"""
from __future__ import annotations

LEGITIMATE = "LEGITIMATE"
NEEDS_SIGNAL = "NEEDS_SIGNAL"
REAL_BUG = "REAL_BUG"

BUCKETS = (REAL_BUG, NEEDS_SIGNAL, LEGITIMATE)


class SAME_AS(tuple):
    """A cross-reference to the site that carries the real reason.

    The version of this file that Stage 5 replaced used prose for this -- "As line
    79.", "As line 458." -- which was stale the moment Stage 3 moved those lines,
    for exactly the same reason the line-number keys were. A SAME_AS must resolve
    to a real entry in the same bucket, and the gate fails if it does not, so a
    reference cannot rot into a decoration.
    """

    def __new__(cls, filename: str, func: str):
        return super().__new__(cls, (filename, func))


def resolve_reason(key: tuple, _seen: frozenset = frozenset()) -> str:
    """Follow SAME_AS references to the underlying prose. Raises if unresolvable.

    A reference names a (file, function); it resolves to the single entry in that
    function that carries prose. Zero or several is an error, not a coin flip --
    ambiguity in an allowlist is how an unexamined site inherits an exemption.
    """
    if key in _seen:
        raise ValueError(f"SAME_AS cycle through {key}")
    bucket, reason = TRIAGE[key]
    if not isinstance(reason, SAME_AS):
        return reason

    targets = [k for k, (b, r) in TRIAGE.items()
               if (k[0], k[1]) == tuple(reason) and not isinstance(r, SAME_AS)]
    if len(targets) != 1:
        raise ValueError(
            f"{key[0]}:{key[1]} defers to {reason[0]}:{reason[1]}, which has "
            f"{len(targets)} explained site(s) -- need exactly 1")
    if TRIAGE[targets[0]][0] != bucket:
        raise ValueError(
            f"{key[0]}:{key[1]} is {bucket} but defers to a "
            f"{TRIAGE[targets[0]][0]} site")
    return resolve_reason(targets[0], _seen | {key})

# Sites that were silent and are now FIXED, keyed by (file, function).
#
# Deliberately kept rather than deleted: the gate fails if the audit ever finds a
# silent path in one of these functions again, so a reverted fix is a build
# failure rather than a rediscovery six weeks later. Keyed by function (not the
# full site key) precisely because a reverted fix would not reproduce the old
# exception clause byte-for-byte, and the check must still catch it.
FIXED: dict[tuple, str] = {
    ("bitbucket_support.py", "get_file"):
        "Stage 4: 404 returns '' (absent); every other status raises, so "
        "unreachable can no longer masquerade as 'no breaking changes'.",
    ("jsonschema_diff.py", "parse_json_schema"):
        "Stage 4: raises SchemaParseError instead of returning {}, so a parse "
        "failure is no longer indistinguishable from a schema with no fields.",
    ("proto_diff.py", "_parse_reserved"):
        "Stage 4: an unparseable reserved statement marks the reserved set "
        "untrustworthy, and _find_field_rename then refuses to infer a rename -- "
        "because a removal reported as a rename is wrong advice, while a missed "
        "rename is only a nuisance.",
}

# (file, function, kind, caught, ordinal) -> (bucket, reason)
TRIAGE: dict[tuple, tuple] = {

    # ------------------------------------------------------------ NEEDS_SIGNAL
    ("api_watcher.py", "_fetch_spec", "swallowed_except", "Exception", 0): (
        NEEDS_SIGNAL,
        "Spec fetch failure returns empty, so the watcher sees no change. Same "
        "absent-vs-unreachable shape as bitbucket_support, but the watcher is "
        "polling and will retry, so it self-heals."),
    ("api_watcher.py", "_fetch_spec", "silent_empty_return", "", 0): (
        NEEDS_SIGNAL, SAME_AS("api_watcher.py", "_fetch_spec")),

    ("avro_diff.py", "parse_avro", "swallowed_except",
     "(json.JSONDecodeError, ValueError)", 0): (
        NEEDS_SIGNAL,
        "Malformed .avsc returns empty; caller cannot tell it from an empty "
        "record. Lower risk than jsonschema only because Avro is stricter."),
    ("avro_diff.py", "parse_avro", "silent_empty_return", "", 0): (
        NEEDS_SIGNAL, SAME_AS("avro_diff.py", "parse_avro")),

    ("custom_playbooks.py", "parse_ripple_config", "swallowed_except",
     "Exception", 0): (
        NEEDS_SIGNAL,
        "A malformed .ripple.yaml silently becomes the default config, so a "
        "customer's ignore rules and confidence threshold are dropped without "
        "anyone being told. Their settings appear not to work."),
    ("custom_playbooks.py", "parse_ripple_config", "silent_empty_return", "", 0): (
        NEEDS_SIGNAL, SAME_AS("custom_playbooks.py", "parse_ripple_config")),

    ("dry_run.py", "dry_run_analysis", "swallowed_except", "Exception", 0): (
        NEEDS_SIGNAL,
        "A user-facing endpoint swallowing Exception returns a clean-looking "
        "analysis for input it failed to process."),

    ("fix_generator.py", "generate_fix", "swallowed_except", "IOError", 0): (
        NEEDS_SIGNAL,
        "Cannot read the consumer file -> returns None -> no fix -> no PR. "
        "Indistinguishable from 'this file needed no fix', which is the exact "
        "silence the outcome enum in Stage 3 exists to remove."),
    ("fix_generator.py", "generate_fix", "silent_empty_return", "", 0): (
        NEEDS_SIGNAL, SAME_AS("fix_generator.py", "generate_fix")),

    ("history_learner.py", "_get_commits", "swallowed_except",
     "(subprocess.TimeoutExpired, FileNotFoundError)", 0): (
        NEEDS_SIGNAL,
        "git missing or timing out returns no commits, which reads as 'this repo "
        "has no co-change history'. The learner then reports low confidence for a "
        "reason that has nothing to do with the repo."),
    ("history_learner.py", "_get_commits", "silent_empty_return", "", 0): (
        NEEDS_SIGNAL, SAME_AS("history_learner.py", "_get_commits")),

    ("monorepo.py", "_git_grep", "swallowed_except",
     "(subprocess.TimeoutExpired, FileNotFoundError)", 0): (
        NEEDS_SIGNAL,
        "Same as history_learner: absent git becomes 'no matches', so monorepo "
        "consumer discovery silently finds nothing."),
    ("monorepo.py", "_git_grep", "silent_empty_return", "", 0): (
        NEEDS_SIGNAL, SAME_AS("monorepo.py", "_git_grep")),

    ("multi_step_reasoning.py", "resolve_fix_target", "swallowed_except",
     "ValueError", 0): (
        NEEDS_SIGNAL,
        "ValueError swallowed while resolving a target; the step reports no "
        "target rather than an unresolvable one."),
    ("multi_step_reasoning.py", "resolve_fix_target", "swallowed_except",
     "ValueError", 1): (
        NEEDS_SIGNAL, SAME_AS("multi_step_reasoning.py", "resolve_fix_target")),

    ("rag_engine.py", "_load_from_disk", "swallowed_except",
     "(json.JSONDecodeError, Exception)", 0): (
        NEEDS_SIGNAL,
        "A corrupt pattern store silently becomes an EMPTY store, so RAG reports "
        "'no patterns' rather than 'the store is damaged'. The store has never "
        "had contents, which is why this has never bitten."),

    ("rag_store.py", "save", "swallowed_except", "(IOError, OSError)", 0): (
        NEEDS_SIGNAL,
        "A failed write loses learned patterns silently. Related to the "
        "durability work in Stage 1: persistence that fails quietly is worse "
        "than no persistence, because the counters keep rising."),

    ("rag_retriever.py", "_resolve_store", "swallowed_except", "Exception", 0): (
        NEEDS_SIGNAL,
        "Falls back to the module singleton without saying so, so per-org "
        "isolation can silently degrade to a shared store."),

    ("slack_notify.py", "_send_via_webhook", "swallowed_except", "Exception", 0): (
        NEEDS_SIGNAL,
        "A dropped notification is invisible: the user believes they were told. "
        "Not a correctness bug, but the whole point of the notification is that "
        "someone finds out."),
    ("slack_notify.py", "_send_via_webhook", "silent_empty_return", "", 0): (
        NEEDS_SIGNAL, SAME_AS("slack_notify.py", "_send_via_webhook")),
    ("slack_notify.py", "_send_via_bot_api", "swallowed_except", "Exception", 0): (
        NEEDS_SIGNAL, SAME_AS("slack_notify.py", "_send_via_webhook")),
    ("slack_notify.py", "_send_via_bot_api", "silent_empty_return", "", 0): (
        NEEDS_SIGNAL, SAME_AS("slack_notify.py", "_send_via_webhook")),

    # -------------------------------------------------------------- LEGITIMATE
    ("dry_run.py", "<module>", "swallowed_except", "ImportError", 0): (
        LEGITIMATE, "Optional-dependency import guard at module scope."),
    ("bitbucket_oauth.py", "<module>", "swallowed_except", "ImportError", 0): (
        LEGITIMATE, "Optional-dependency import guard at module scope."),
    ("gitlab_oauth.py", "<module>", "swallowed_except", "ImportError", 0): (
        LEGITIMATE, "Optional-dependency import guard at module scope."),
    ("gitlab_setup.py", "<module>", "swallowed_except", "ImportError", 0): (
        LEGITIMATE, "Optional-dependency import guard at module scope."),

    ("rag_engine.py", "__init__", "silent_empty_return", "", 0): (
        LEGITIMATE,
        "sentence-transformers/chromadb are ~2GB and deliberately excluded from "
        "CI. The degradation is reported as rag_unavailable rather than hidden."),
    ("rag_engine.py", "__init__", "swallowed_except",
     "(ImportError, Exception)", 0): (
        LEGITIMATE, SAME_AS("rag_engine.py", "__init__")),
    ("rag_engine.py", "__init__", "swallowed_except",
     "(ImportError, Exception)", 1): (
        LEGITIMATE, SAME_AS("rag_engine.py", "__init__")),
    ("rag_engine.py", "index_from_git", "swallowed_except",
     "subprocess.CalledProcessError", 0): (
        LEGITIMATE,
        "A single commit failing `git show` is skipped; indexing is best-effort "
        "over many commits and the total is reported."),
    ("rag_engine.py", "index_from_git", "swallowed_except",
     "subprocess.CalledProcessError", 1): (
        LEGITIMATE, SAME_AS("rag_engine.py", "index_from_git")),
    ("rag_engine.py", "index_from_git", "swallowed_except",
     "subprocess.CalledProcessError", 2): (
        LEGITIMATE, SAME_AS("rag_engine.py", "index_from_git")),
    ("rag_engine.py", "index_single_commit", "swallowed_except",
     "subprocess.CalledProcessError", 0): (
        LEGITIMATE, "Best-effort per-commit indexing, as index_from_git."),
    ("rag_engine.py", "index_single_commit", "swallowed_except",
     "subprocess.CalledProcessError", 1): (
        LEGITIMATE, SAME_AS("rag_engine.py", "index_single_commit")),

    ("api_watcher.py", "_load_state", "swallowed_except",
     "(json.JSONDecodeError, TypeError)", 0): (
        LEGITIMATE,
        "Corrupt watcher state resets to empty, which is the correct recovery: "
        "the next poll rebuilds it."),

    ("github_app_auth.py", "get_installation_token", "swallowed_except",
     "(ValueError, TypeError)", 0): (
        LEGITIMATE,
        "Verified in Stage 4 rather than assumed from the function name: "
        "expires_at defaults to time.time() + 3600 BEFORE the try, and GitHub "
        "documents installation tokens as 1h, so a malformed expires_at falls "
        "back to exactly the real lifetime."),

    ("tls.py", "resolve_ca_bundle", "swallowed_except", "Exception", 0): (
        LEGITIMATE,
        "Candidate CA bundle locations are tried in order; tls.describe() "
        "reports which one was resolved, so the outcome is visible."),
    ("tls.py", "make_ssl_context", "swallowed_except", "Exception", 0): (
        LEGITIMATE, SAME_AS("tls.py", "resolve_ca_bundle")),

    ("webhook.py", "_retry_delay", "swallowed_except",
     "(AttributeError, TypeError, ValueError)", 0): (
        LEGITIMATE,
        "An unparseable Retry-After header falls back to the computed backoff."),
    ("webhook.py", "_scan_repo_tree_for_consumers", "swallowed_except",
     "(ValueError, UnicodeDecodeError)", 0): (
        LEGITIMATE,
        "UnicodeDecodeError on a binary blob skips that file. is_scannable() "
        "already excludes most, and a binary file is not a consumer."),

    ("consumer_finder.py", "find_consumers", "swallowed_except",
     "(IOError, OSError)", 0): (
        LEGITIMATE,
        "An unreadable file during the directory walk is skipped. Same rationale "
        "as the webhook's tree scan."),

    ("token_store.py", "_find_store_dir", "swallowed_except",
     "(IOError, OSError)", 0): (
        LEGITIMATE,
        "Candidate store directories are tried in order until one is writable."),
}

# A reason shorter than this is not a reason.
MIN_REASON = 40


def counts() -> dict:
    out = {b: 0 for b in BUCKETS}
    for bucket, _ in TRIAGE.values():
        out[bucket] += 1
    return out


def by_bucket(bucket: str) -> list:
    return sorted(k for k, (b, _) in TRIAGE.items() if b == bucket)


if __name__ == "__main__":
    c = counts()
    print(f"  {sum(c.values())} sites triaged, {len(FIXED)} function(s) fixed")
    for bucket in BUCKETS:
        print(f"    {bucket:<14} {c[bucket]}")
        for key in by_bucket(bucket):
            f, fn, kind, caught, ordinal = key
            print(f"        {f}:{fn}  {kind}[{ordinal}] {caught}")
