"""The run-level terminal state: exactly one per breaking change.

WHY THIS IS SEPARATE FROM app/outcomes.py
app/outcomes.py records EVENT-level outcomes -- DETECTED, CONSUMERS_FOUND,
FIX_GENERATED, PR_CREATED, BLOCKED -- and several fire per breaking change, one per
consumer file. Useful for tracing, useless for counting: you cannot compute
"what happened to this breaking change?" from a stream in which the same change
produced FIX_GENERATED four times and BLOCKED twice.

This is the other layer. One breaking change in, exactly one terminal state out.
It is what makes the Autonomous Resolution Rate computable at all -- without a
single answer per change there is no denominator.

EXACTLY ONCE, STRUCTURALLY
ChangeRun is a context manager, so the terminal state is emitted on __exit__
whether the body returns, breaks, or raises. An exception becomes FAILED rather
than nothing, which is the case that would otherwise vanish: `_process_spec_change`
wraps engine calls in a broad handler that logs process_spec_error, so before this
an exception mid-consumer-loop produced a logged error and NO statement about the
change itself.

The alternative -- asking every exit path to remember to emit -- is exactly how
the package vector ended up built, tested, CI-gated and unreachable, and how two
bare `continue`s in the consumer loop dropped work without a record.

DERIVED, NOT ASSERTED
Callers report FACTS: a consumer was found, a PR was created, a fix was refused
with a reason. No caller may say "this run is PARTIAL". Same rule as
terminal_outcome() and vector_for(), for the same reason: a value someone must
remember to set is a value that ends up wrong.

WHY SIX STATES AND NOT THE FIVE SPECIFIED
RESOLVED / PARTIAL / NO_CONSUMER / BLOCKED / FAILED, plus NO_CHANGE_REQUIRED.

The sixth exists to keep the Autonomous Resolution Rate honest. A wire_only change
(a proto field number changed) requires NO source edit -- consumers never reference
field numbers. Mapping that to BLOCKED would report a correct refusal as a failure,
which is the mistake the capability registry made when it demanded a transformation
from wire_only operations. Mapping it to RESOLVED would be worse: it would inflate
the resolution rate with changes where Ripple did nothing, and that number is
supposed to be the one the company is built on.

So it is counted separately and excluded from both numerator and denominator.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Terminal(str, Enum):
    RESOLVED = "RESOLVED"                    # every affected consumer has a validated PR
    PARTIAL = "PARTIAL"                      # some fixed, some not -- or fixed but unvalidated
    NO_CONSUMER = "NO_CONSUMER"              # nothing references it
    BLOCKED = "BLOCKED"                      # consumers exist, no PR could be opened
    FAILED = "FAILED"                        # the run itself errored
    NO_CHANGE_REQUIRED = "NO_CHANGE_REQUIRED"  # correct to touch nothing (wire_only)


#: States that count toward the Autonomous Resolution Rate numerator.
COUNTS_AS_RESOLVED = frozenset({Terminal.RESOLVED})

#: States excluded from the ARR denominator entirely -- Ripple was not asked to
#: change any code, so scoring itself on them measures nothing.
EXCLUDED_FROM_RATE = frozenset({Terminal.NO_CHANGE_REQUIRED})


@dataclass
class ChangeRun:
    """One breaking change's journey. Use as a context manager.

        with ChangeRun(change_type="remove_field", spec="api/user.yaml",
                       repo="acme/api") as run:
            run.consumer_found("checkout.ts")
            run.pr_created("https://...", "checkout.ts", validated=False)
        # -> emits exactly one terminal state here, PARTIAL
    """

    change_type: str
    spec: str
    repo: str
    consumers: list = field(default_factory=list)
    prs: list = field(default_factory=list)
    validated: list = field(default_factory=list)
    refusals: list = field(default_factory=list)   # (target, reason)
    no_change_required: bool = False
    error: str = ""
    emitted: int = 0

    # ---- facts in -------------------------------------------------------
    def consumer_found(self, target: str) -> None:
        if target not in self.consumers:
            self.consumers.append(target)

    def pr_created(self, url: str, target: str, validated: bool = False) -> None:
        self.consumer_found(target)
        self.prs.append({"url": url, "target": target, "validated": validated})
        if validated:
            self.validated.append(target)

    def refused(self, target: str, reason: str) -> None:
        """A consumer Ripple could not fix. A refusal without a reason is silence."""
        if not reason:
            raise ValueError("refused() requires a reason -- an unexplained "
                             "refusal is the silence this module exists to remove")
        self.consumer_found(target)
        self.refusals.append({"target": target, "reason": reason})

    def requires_no_change(self) -> None:
        """wire_only: correct to touch nothing. Not a success, not a failure."""
        self.no_change_required = True

    # ---- state out ------------------------------------------------------
    def terminal(self) -> Terminal:
        if self.error:
            return Terminal.FAILED
        if self.no_change_required:
            return Terminal.NO_CHANGE_REQUIRED
        if not self.consumers:
            return Terminal.NO_CONSUMER
        if not self.prs:
            return Terminal.BLOCKED
        # A PR exists. It is only RESOLVED if EVERY consumer got one and every one
        # of those was validated. Until a real validation layer exists, `validated`
        # is never populated, so RESOLVED is unreachable -- deliberately, and for
        # the same reason AUTO is unreachable. An unvalidated fix is a proposal.
        covered = {p["target"] for p in self.prs}
        if covered == set(self.consumers) and len(self.validated) == len(covered):
            return Terminal.RESOLVED
        return Terminal.PARTIAL

    def detail(self) -> dict:
        return {
            "change_type": self.change_type,
            "spec": self.spec,
            "repo": self.repo,
            "consumers": len(self.consumers),
            "prs": len(self.prs),
            "validated": len(self.validated),
            "refused": len(self.refusals),
            "reasons": [r["reason"] for r in self.refusals][:5],
            "counts_toward_rate": self.terminal() not in EXCLUDED_FROM_RATE,
        }

    # ---- exactly once ---------------------------------------------------
    def __enter__(self) -> "ChangeRun":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None and not self.error:
            self.error = f"{exc_type.__name__}: {exc}"[:300]
        self._emit()
        return False          # never swallow -- the caller's handler still runs

    def _emit(self) -> dict:
        from app.activity import record as _record
        self.emitted += 1
        payload = {"terminal": self.terminal().value, **self.detail()}
        if self.emitted > 1:
            # Cannot happen through the context manager, but if someone calls
            # _emit() directly the duplicate is labelled rather than hidden.
            payload["duplicate_emission"] = self.emitted
        return _record("change_terminal", payload)
