# Ripple — invariants and traps

Read before changing `app/fix_templates.py`, `app/change_types.py`, the diff
engines, or the webhook change loops.

## The core failure mode: silence

`fixed_code == content` means **no PR opens**. So an unhandled change_type does
not surface an error — it produces silence. Ripple detects a breaking change and
does nothing, while the user's mental model stays "Ripple watches my protos".

Every change to the fix path must preserve this chain:

    engine emits change_type -> canonical_op() -> a dispatch branch -> non-empty diff

`app/change_types.py` is the single source of truth. 47 dialect strings from 10
engines collapse onto 10 canonical operations in 4 categories. Never add a
handler keyed on a raw dialect string — add the dialect to `CHANGE_TYPE_MAP`.

## Category contracts

| Category | Contract |
|---|---|
| MECHANICAL (27 types) | deterministic transform; output must compile |
| JUDGMENT (21 types) | apply the safe part, mark the rest `RIPPLE-ACTION-REQUIRED`. Never invent a value. Never silently delete logic. |
| WIRE_ONLY (2 types) | code unchanged is **correct**. Must say `NO SOURCE CHANGE REQUIRED`. Callers use `is_wire_only()` to tell this from failure. |
| NON_BREAKING (`add_optional`) | not fixable; filtered at RAG ingest by `is_fixable()` |

Wire-only must short-circuit **before** consumer search, in **all three**
platform loops (`_process_spec_change_inner`, `gitlab_webhook`,
`bitbucket_webhook`). Code search returns 0 under installation tokens, so the
tree-walk is the primary path at ~40 calls/repo — an unguarded no-op costs
hundreds of API calls. A guard on only the GitHub path is the bug that shipped
in stage 4 and was caught in stage 5.

## Traps that have each bitten more than once

**Underscore is a word character.** `\bLEGACY\b` does not match Go's
`Status_LEGACY`. This has appeared four times (consumer matcher confidence,
`find_residual_references`, case-arm matcher, judgment site matcher). Always use
`(?<![A-Za-z0-9])NAME(?![A-Za-z0-9])`.

**Removing a line orphans its body.** Deleting `case X:` leaves the statements
beneath it dangling inside the switch. Removal must span to the next
`case`/`default`/closing brace. Same class as the Go trailing-comma defect.

**Declarations come in two shapes.** `enum Status { A, B }` inline vs multiline.
Handling one silently misses the other — found only by auditing green cells.

**Never claim output compiles.** Commenting out `r, err := c.svc.Delete(ctx)`
leaves `return err` undefined. Explanations must state the honest caveat; a
regression test asserts the false claim is absent.

## Gates — first five gate `.github/workflows/checks.yml`, the sixth is report-only

    python3 tools/check_names.py app/*.py       # NameError before deploy
    python3.12 tests/test_regression.py         # 70 tests
    python3 tools/audit_diff_engines.py         # 0 FN / 0 FP
    python3 tools/audit_change_types.py         # 47/47 classified
    python3 tools/coverage_matrix.py            # 459 combos, 0 escapes
    python3 tools/audit_fail_silent.py          # continue-on-error: 51 sites left

`python3.12` is not on PATH. Use
`/home/aakkaash/.toolbox/tools/meshclaw/3.3.7/python3.12/bin/python3.12`.

## Method that actually found the bugs

Test locally against real APIs via `tools/local_e2e.py`; never deploy-and-observe
(one bug per 90s cycle, and only the first in the chain).

Then **audit the green result**. Six real defects in this codebase were found by
investigating cells that reported "no change" rather than accepting the passing
aggregate. A matrix that is 100% green on "no unknown types" can still hide a
transform that matched nothing.

## Claims must not outrun the code

The site, README, and PR footers claim learning ("Learning: enabled", "5
independent learning channels", 7%->17% co-change). RAG executes, but the store
is empty — install-time indexing has never populated it. Until it does, those
claims are false. Do not add new capability claims ahead of verified behaviour.
