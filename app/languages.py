"""Single source of truth for "what language is this file, and should we scan it".

WHY THIS EXISTS
---------------
There were SIX implementations of language detection and TWO of the
scannable-file decision, and they disagreed with each other -- not merely
drifted apart:

    webhook._detect_lang               7 exts ->  5 languages   <- PRODUCTION
    multi_step_reasoning               9 exts ->  7 languages
    history_learner                   10 exts ->  8 languages
    consumer_finder                   13 exts -> 11 languages
    fix_generator_multi               16 exts -> 12 languages
    rag_engine                        19 exts -> 15 languages   <- THE BENCHMARK

The measured recall figure comes from the 15-language path (PropBench's
replay.py imports rag_engine._detect_language) while production ran the
5-language one, so the number described a capability the deployed system did
not have.

Three concrete defects the survey found, all fixed by consolidating here:

1. `.js` was a WRONG ANSWER, not a gap. fix_generator_multi mapped it to
   `typescript`; the other five said `javascript`. Because generate_fix_multi
   routes typescript to the original generator, mislabelling `.js` quietly
   avoided the javascript template gaps instead of exposing them.

2. history_learner returned `None` for a miss where everyone else returned
   `"unknown"`. A caller written `if lang:` and one written
   `if lang == "unknown":` behave differently on the same file. One sentinel
   now: UNKNOWN.

3. The two _is_code_file implementations disagreed WITH the detectors. `.rs`
   and `.rb` passed webhook's file filter but its detector returned "unknown",
   so rust and ruby files were scanned with the generic matcher. Meanwhile
   `.yaml` and `.sh` had matchers written for them and were rejected by the
   file filter before ever reaching one.

WHAT "unknown" COSTS
--------------------
Less than it looks. find_matches_in_file falls back to generic patterns for an
unknown language, so a misdetected file is still scanned -- the cost is the
wrong matcher dialect, not a skipped file. The genuinely skipped files are the
ones is_scannable() rejects, which is why that decision lives here too rather
than in a separate module that can drift from this map again.
"""
from __future__ import annotations

from pathlib import Path

UNKNOWN = "unknown"

# extension -> language. The superset of all six previous implementations.
#
# Config and scripts are deliberate, and measured: 137 files a real PR had to
# change were skipped for having no matcher -- 24 of 36 on kubernetes#109798,
# i.e. most of that change. Manifests and scripts reference removed resources
# by name exactly as source does.
EXTENSION_TO_LANGUAGE = {
    ".py": "python",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".cjs": "javascript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".kt": "kotlin", ".kts": "kotlin",
    ".cs": "csharp",
    ".swift": "swift",
    ".php": "php",
    ".scala": "scala", ".sc": "scala",
    ".dart": "dart",
    ".yaml": "yaml", ".yml": "yaml",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
}

# Contract files. A spec is never a consumer of itself, so these are excluded
# from consumer scanning even though .yaml/.json also appear above.
SPEC_EXTENSIONS = {
    ".proto", ".graphql", ".gql", ".avro", ".thrift", ".smithy",
}

# Vendored dependencies, generated output and build artefacts. Excluding these
# is not a heuristic: they are never hand-edited source, so a PR touching one is
# always wrong.
NON_SOURCE_SEGMENTS = (
    "node_modules/", "vendor/", "third_party/", "dist/", "build/",
    ".next/", "coverage/", "target/debug/", "target/release/",
    "site-packages/", ".venv/", "venv/", "__pycache__/",
    ".git/", "generated/",
)

# Generated code: regenerated FROM the contract, so editing it is pointless --
# the generator output changes when the spec does.
GENERATED_SUFFIXES = (
    ".min.js", ".d.ts", ".pb.go", "_pb2.py", "_pb2_grpc.py",
    ".generated.ts", ".g.dart", "_pb.js", ".pb.cc", ".pb.h",
)


def detect(filepath: str) -> str:
    """Language for a path, or UNKNOWN.

    Returns UNKNOWN rather than None so every caller can compare against one
    sentinel. Callers that need "is this a code file" must ask is_scannable()
    -- `if detect(p):` is always truthy and was a live bug in history_learner.
    """
    return EXTENSION_TO_LANGUAGE.get(Path(filepath).suffix.lower(), UNKNOWN)


def is_known(filepath: str) -> bool:
    """True when the language is recognised. Use instead of truthiness."""
    return detect(filepath) != UNKNOWN


def is_spec(filepath: str) -> bool:
    """True for contract files -- a spec is not a consumer of itself."""
    return Path(filepath).suffix.lower() in SPEC_EXTENSIONS


def is_scannable(filepath: str) -> bool:
    """Would a human hand-edit this file to adapt to a contract change?

    Extension alone is insufficient: vendored and generated code carry real
    source extensions and must never be patched by a PR.
    """
    if not is_known(filepath):
        return False
    lowered = filepath.lower()
    if any(seg in lowered for seg in NON_SOURCE_SEGMENTS):
        return False
    # NOTE: 'website/', 'docs/', 'marketing/' and 'examples/' were previously
    # excluded, to stop Ripple opening a PR against its own landing page. That
    # suppressed a symptom of unscoped repo discovery rather than expressing a
    # real rule -- a customer can legitimately call an API from an example app
    # or a docs site. Installation scope is authoritative now, and per-customer
    # exclusions belong in .ripple.yaml `ignore:` (honoured via
    # config.should_ignore), not in a hardcoded list here.
    return not lowered.endswith(GENERATED_SUFFIXES)


def languages() -> set:
    """Every language this module can produce. Used by the CI coverage gate."""
    return set(EXTENSION_TO_LANGUAGE.values())
