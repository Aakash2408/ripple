"""The PR safety level, decided by the capability registry.

WHAT THIS FIXES
The registry has been able to compute `production_ready(language, contract, op)`
since it was built, and NOTHING IN PRODUCTION ASKED IT. Only tools/ and tests/
imported it. So the registry reported honestly while the running service treated
a cell with two blockers exactly like a validated one: `should_create_pr()`
consulted confidence and nothing else, and `format_pr_body()` titled the result
"## Ripple - Automated Fix" regardless.

That is the same defect shape as the package vector -- built, tested, CI-gated,
and unreachable from the code path that mattered. A registry that reports but does
not govern is a report.

THE THREE LEVELS

  AUTO     the registry says the cell is production-ready AND the change is a
           deterministic transform AND confidence clears the threshold.
  REVIEW   a fix was produced, but something is unproven. The PR opens and says
           so, with the specific reasons.
  BLOCKED  no PR. Today this fires only on confidence below the configured
           threshold, which is what the old code did by `continue`-ing.

WHAT THIS DELIBERATELY DOES NOT DO
It does not stop Ripple opening PRs. 45 of the 48 fixable V1 cells are not
production-ready -- all of them blocked on validation and end-to-end evidence --
so mapping "not ready" to BLOCKED would silence the product entirely. It maps to
REVIEW, which is the honest statement: a fix exists, nobody has proven it
compiles.

The consequence worth stating plainly: **AUTO is currently unreachable.** The
three production-ready cells are `wire_only` operations, which correctly produce
NO_CHANGE_REQUIRED and never reach a PR. So every PR Ripple opens today is
REVIEW. That is not a regression -- it is the pre-existing truth, previously
hidden behind a heading that said "Automated Fix".

NO LANGUAGE LISTS LIVE HERE. Every eligibility question is asked of the registry.
app/ai_confidence.py held a KNOWN_LANGUAGES set described as "languages Ripple has
strong fix generation support for" -- a capability claim outside the registry,
which also listed `proto` and `graphql` as LANGUAGES when they are contract types.
It was deleted in this stage along with app/impact_prediction.py; both were dead,
so neither list had ever decided anything.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app import capability_claims as _claims
from app.change_types import CANONICAL_OPS, MECHANICAL, canonical_op


class Level(str, Enum):
    AUTO = "AUTO"
    REVIEW = "REVIEW"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class Decision:
    level: Level
    reasons: tuple

    @property
    def opens_pr(self) -> bool:
        return self.level is not Level.BLOCKED

    @property
    def review_required(self) -> bool:
        return self.level is Level.REVIEW

    def as_detail(self) -> dict:
        return {"level": self.level.value, "reasons": list(self.reasons)}


def pr_level(language: str, contract: str, change_type: str,
             confidence: float, min_confidence: float) -> Decision:
    """Decide the safety level. Callers pass facts, never a level.

    Same rule as Stage 3's terminal_outcome(): the decision is DERIVED here so no
    caller can assert it. A level someone must remember to set is exactly how the
    package vector ended up unreachable.
    """
    # 1. Below the configured confidence threshold: no PR. Delegated to the
    #    EXISTING should_create_pr() rather than restating `confidence <
    #    min_confidence` here -- a second copy of a threshold rule is how the
    #    language maps drifted into eight disagreeing implementations.
    from app.confidence import should_create_pr
    if not should_create_pr(confidence, min_confidence):
        return Decision(Level.BLOCKED, (
            f"confidence {confidence:.2f} is below the configured minimum "
            f"{min_confidence:.2f}",))

    reasons = []

    # 2. Canonicalise ONCE, at the boundary. The registry is keyed by canonical
    #    operation, and engines emit dialects ("added_required_field",
    #    "field_renamed"). The first version of this function read CANONICAL_OPS
    #    directly and reported "added_required_field is unknown" for Ripple's most
    #    common change -- a second lookup of a question canonical_op() already
    #    answers, which is the duplicate-implementation defect this stage exists to
    #    remove.
    op = canonical_op(change_type)
    if not op:
        return Decision(Level.REVIEW, (
            f"{change_type!r} does not map to any canonical operation, so the "
            f"registry cannot be asked whether this is safe",))

    # 3. Ask the registry. It owns this question; routing does not keep a copy.
    reasons.extend(_claims.blocking_reasons(language, contract, op))

    # 4. A judgment change cannot be automatic even if everything else holds --
    #    the transformation is not deterministic, so a human must read it.
    category = CANONICAL_OPS[op][0]
    if category != MECHANICAL:
        reasons.append(f"{op} is {category}, not a deterministic transform")

    if reasons:
        return Decision(Level.REVIEW, tuple(reasons))

    # Reached only when the registry says production-ready AND the change is
    # mechanical AND confidence clears. Guarded by a regression test that fails
    # if AUTO can ever be produced for a cell the registry has not cleared.
    return Decision(Level.AUTO, ())


def is_production_ready(language: str, contract: str, change_type: str) -> bool:
    """Single re-export so callers never reach past routing into the registry."""
    return _claims.production_ready(language, contract, change_type)
