"""Every breaking change ends in a STATED outcome. Silence is not an outcome.

THE FAILURE THIS REMOVES
Ripple's central invariant is `fixed_code == content` means no PR opens. That is
correct as a PR rule and disastrous as an outcome: a detected breaking change
could end with nothing recorded and nothing shown. The fix loop logged
`fix_generated {changed: false}` and stopped -- a fact about the code, not a
statement about what happened or what the user should do.

Measured instances this year, all of which presented identically as "nothing
happened": a rename fell through because BreakingChange had no new_name field; a
type change was a no-op in six of nine languages; `git rm api/user.proto` was
never even read from the push payload; 47 change types reached fix_templates and
44 returned "Unknown change_type".

THE OUTCOME IS DERIVED, NOT PASSED
terminal_outcome() computes the outcome from the change and the generated code.
A parameter callers must remember to set is how the package vector ended up
built, tested, CI-gated and unreachable -- so no caller gets to assert an
outcome. The same reasoning as change_types.vector_for().

BLOCKED IS DEFINED AND CURRENTLY UNREACHABLE FOR VALIDATION
BLOCKED means "Ripple could not safely produce a fix". It fires today when a
transformation produced nothing. It does NOT yet fire on validation failure,
because real toolchain validation does not exist -- see app/capability_claims.py.
Defining the state without the trigger is deliberate: the alternative is either
pretending validation runs, or leaving no vocabulary for a refusal.
"""
from __future__ import annotations

from enum import Enum


class Outcome(str, Enum):
    """Terminal states. Every detected change reaches exactly one per consumer."""

    DETECTED = "DETECTED"
    NO_CONSUMERS = "NO_CONSUMERS"
    CONSUMERS_FOUND = "CONSUMERS_FOUND"
    FIX_GENERATED = "FIX_GENERATED"
    HUMAN_ACTION_REQUIRED = "HUMAN_ACTION_REQUIRED"
    PR_CREATED = "PR_CREATED"
    BLOCKED = "BLOCKED"
    # Distinct from BLOCKED: for a wire-only change, unchanged source is the
    # CORRECT answer. Collapsing the two would report a correct refusal as a
    # failure -- the same mistake the capability registry made when it demanded a
    # transformation from wire_only operations.
    NO_CHANGE_REQUIRED = "NO_CHANGE_REQUIRED"


# Outcomes that mean "this consumer is done, nothing more will happen".
TERMINAL = frozenset({
    Outcome.NO_CONSUMERS, Outcome.PR_CREATED, Outcome.BLOCKED,
    Outcome.NO_CHANGE_REQUIRED, Outcome.HUMAN_ACTION_REQUIRED,
})

# Outcomes a human must look at. Used by the PR safety policy.
NEEDS_HUMAN = frozenset({Outcome.HUMAN_ACTION_REQUIRED, Outcome.BLOCKED})


def terminal_outcome(change_type: str, original_code: str, fixed_code: str,
                     explanation: str = "") -> Outcome:
    """The outcome for one consumer file. Derived, never asserted by a caller.

    Ordering matters:
      1. wire_only first -- unchanged is correct there, so it must not be read
         as a failure to produce a fix.
      2. an explicit marker means a human is required, even though code changed.
      3. unchanged code with no marker is BLOCKED. This is the case that used to
         be silence.
    """
    from app.change_types import category
    from app.fix_templates import MARKER

    try:
        cat = category(change_type)
    except Exception:
        cat = "unknown"

    if cat == "wire_only":
        return Outcome.NO_CHANGE_REQUIRED
    if cat == "non_breaking":
        return Outcome.NO_CHANGE_REQUIRED

    changed = fixed_code != original_code
    marked = MARKER in (fixed_code or "") or MARKER in (explanation or "")

    if marked:
        return Outcome.HUMAN_ACTION_REQUIRED
    if changed:
        return Outcome.FIX_GENERATED
    return Outcome.BLOCKED


def blocked_reason(change_type: str, explanation: str = "") -> str:
    """Why Ripple refused. A BLOCKED outcome with no reason is still silence."""
    from app.change_types import canonical_op
    try:
        op = canonical_op(change_type)
    except Exception:
        op = change_type
    if explanation and "Unsupported change type" in explanation:
        return (f"no transformation exists for {op} in this language -- "
                f"see tools/audit_capabilities.py")
    if explanation and "No template fix applied" in explanation:
        return (f"a transformation for {op} exists but matched nothing in this "
                f"file; the reference may be in a shape Ripple cannot rewrite")
    return (f"the {op} transformation produced no change to this file, so Ripple "
            f"cannot say it fixed anything")


def record(outcome: Outcome, **detail) -> dict:
    """Log an outcome through the one activity store.

    Uses a single action name so the dashboard and /logs/recent can count
    outcomes without knowing every producer -- and so a new outcome cannot be
    introduced without appearing there.
    """
    from app.activity import record as _record
    payload = {"outcome": outcome.value, **detail}
    return _record("outcome", payload)
