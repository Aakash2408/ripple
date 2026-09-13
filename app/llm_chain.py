"""Try each usable LLM backend in turn, and say what happened when none answered.

WHAT WAS THERE BEFORE
One provider, one attempt, and this on any failure (fix_generator.py):

    except Exception as e:
        print(f"  ⚠️  LLM error: {e}. Using template fix.")
        return _generate_with_template(...)

The template fix that comes back is a real fix and it is labelled `template`, so the
PR's provenance was already honest -- that part was right. Three things were not:

1. NO CHAIN. A 429 from the one configured provider ended the attempt, even with two
   other usable backends sitting in the registry.
2. THE FAILURE WAS INVISIBLE. `print()` goes to stdout. There is no _log_activity
   event, so the dashboard shows a template fix and no reason, and "the model was rate
   limited" is indistinguishable from "no model was configured".
3. NO BUDGET. One push fans out over N consumer files, each calling the LLM
   independently. After the first 429 the remaining N-1 calls are certain to 429 too,
   and every one was still attempted.

RATE LIMITED IS NOT THE SAME AS BROKEN
The distinction drives whether trying the next provider makes sense:

    rate_limited  429   this provider, later -- ANOTHER provider now
    no_credit     402   this provider is out of quota -- another may not be
    auth          401   this key is wrong -- another provider's key may be fine
    unreachable   5xx / connection / timeout -- transient, try the next
    bad_request   400   OUR payload is malformed. It will fail everywhere, so
                        continuing down the chain just multiplies the same error.

`bad_request` is the one that must NOT advance the chain. Retrying our own bug against
three providers turns one wrong request into three and buries the cause.

WHY THE FIX CHAIN AND THE PROSE CHAIN DIFFER
Measured 2026-08-26 against the local 3B model on the three shapes the deterministic
codemod REFUSES -- the shapes where an LLM is the only option:

    function parameter   export function dial(): void { console.log(); }
                         parameter deleted, breaks every caller
    aliased then used    export function label(user) { return ''; }
                         body replaced, silently always returns ''
    destructuring        const { name } = user;                    correct

Two of three were wrong, and all three were brace-balanced with the target removed --
so the diff contract passes them and `tsc` compiles them. Nothing downstream can tell
them from a correct fix.

So a small local model must not be a silent fallback for FIX GENERATION: substituting
it for a frontier model would degrade the output in a way no gate can see. For PROSE it
is fine, because bad prose is visible to the reader and cannot break a build. Hence
`fix_chain()` excludes providers marked `fix_capable=False` and `prose_chain()` does
not, and a gate pins that difference so nobody quietly adds the 3B model to the fix
path.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field as _field

#: Outcomes that mean "this provider cannot answer, but another might".
RETRYABLE = ("rate_limited", "no_credit", "auth", "unreachable")

#: And the one that means "stop": the request itself is wrong.
TERMINAL = ("bad_request",)


def classify(exc: Exception) -> tuple:
    """(outcome, detail) for an exception raised while calling a provider.

    Duck-typed on `status_code` rather than importing the SDK's exception classes: the
    generator already tolerates the anthropic package being absent, and a classifier
    that needs the import would fail in exactly the environment that path exists for.
    """
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    text = str(exc)[:200]
    if status == 429:
        return "rate_limited", text
    if status == 402:
        return "no_credit", text
    if status in (401, 403):
        return "auth", text
    if status == 400:
        return "bad_request", text
    if isinstance(status, int) and 500 <= status < 600:
        return "unreachable", text
    # No status: connection refused, DNS, timeout. Treated as transient, because the
    # alternative -- calling it terminal -- would stop the chain when a local Ollama
    # simply is not running.
    return "unreachable", text


@dataclass(frozen=True)
class Attempt:
    """One provider, one call, one outcome. Recorded whether it succeeded or not."""
    provider: str
    model: str
    outcome: str
    detail: str
    seconds: float

    def __str__(self) -> str:
        base = f"{self.provider} ({self.model}) -> {self.outcome} in {self.seconds:.1f}s"
        return f"{base}: {self.detail}" if self.detail else base


@dataclass(frozen=True)
class ChainResult:
    """What the chain produced, and the full record of how it got there."""
    text: str = ""
    answered_by: str = ""
    model: str = ""
    attempts: tuple = _field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return bool(self.text)

    def stated_outcome(self) -> str:
        """One line naming every attempt. Goes in the PR body and the activity log.

        Exhaustion MUST produce text here rather than an empty string or None: the
        defect this replaces was a failure that existed only as a print() to stdout,
        so a template fix and a rate-limited model looked identical afterwards.
        """
        if not self.attempts:
            return "no LLM backend was attempted (none configured)"
        if self.ok:
            return (f"answered by {self.answered_by} ({self.model})" if self.model
                    else f"answered by {self.answered_by}")
        return ("no LLM backend answered: "
                + "; ".join(str(a) for a in self.attempts))


class Budget:
    """Per-provider call ceiling for one push.

    A push fans out over N consumer files and each asks for a fix independently. With
    no ceiling, the first 429 is followed by N-1 more certain 429s. Once a provider
    has reported rate_limited or no_credit it is CLOSED for the rest of the run --
    that is not a guess about when the window resets, it is a refusal to keep asking a
    question already answered.
    """

    def __init__(self, per_provider: int = 25) -> None:
        self.per_provider = per_provider
        self._used: dict = {}
        self._closed: set = set()

    def allows(self, provider: str) -> bool:
        return (provider not in self._closed
                and self._used.get(provider, 0) < self.per_provider)

    def record(self, provider: str, outcome: str) -> None:
        self._used[provider] = self._used.get(provider, 0) + 1
        if outcome in ("rate_limited", "no_credit"):
            self._closed.add(provider)

    def why_skipped(self, provider: str) -> str:
        if provider in self._closed:
            return "closed earlier this run (rate limited or out of credit)"
        return f"per-provider budget of {self.per_provider} calls exhausted"


@dataclass(frozen=True)
class Configured:
    """The explicitly configured backend, as a chain entry.

    ANTHROPIC_BASE_URL pointing at a custom gateway is the pre-existing single-backend
    contract and has to keep working, so it is tried first. It is a distinct type
    rather than a synthetic Provider because it must NOT borrow a registry provider's
    name: `name` here is backend_label(), which reports the model and host that
    actually answered. Attributing a custom gateway's reply to "openrouter" because
    openrouter happens to be first in the registry is the exact bug this shape
    prevents, and it was measured before it was prevented.
    """
    name: str
    base_url: str
    key_env: str = ""
    fix_capable: bool = True


def merge(first, second) -> "ChainResult":
    """Combine two chain runs, preserving the full attempt record.

    Without this the configured-backend attempt would vanish from
    `stated_outcome()` whenever it failed and a registry provider then answered --
    losing exactly the information that explains why the fallback happened.
    """
    if first is None:
        return second
    attempts = tuple(first.attempts) + tuple(second.attempts)
    winner = second if second.ok else first
    return ChainResult(text=winner.text, answered_by=winner.answered_by,
                       model=winner.model, attempts=attempts)


def _reachable(provider) -> bool:
    """Does this provider have what it needs to be called AT ALL?

    A chain entry with no credential is not a fallback, it is a guaranteed 401 -- and
    worse, including it invites the bug that produced this function. The first draft
    reused llm_config.base_url() for every entry, so all of them hit the same endpoint
    and the answer was credited to whichever came first: a run with no OpenRouter key
    reported `answered by openrouter` while a local Ollama had actually replied.

    Keyless is legitimate (a self-hosted endpoint authenticating nothing), so an empty
    key_env means reachable rather than unreachable.
    """
    from .llm_config import credential_for
    if not provider.key_env:
        return True
    return bool(credential_for(provider.key_env))


def _fix_capable(provider) -> bool:
    """Is this provider, WITH ITS CURRENTLY CONFIGURED MODEL, trusted to write code?

    For a hosted provider the answer is the provider's own flag -- the model is theirs
    and we take the vendor's frontier model as given.

    For a SELF-HOSTED provider it is a property of the loaded model, and that
    distinction is not academic. Measured 2026-08-26: on the same local Ollama, the 3B
    and the 16B both deleted a function parameter and broke every caller, while the
    30B declined to edit at all. Same server, same wire format, opposite trust. A
    provider-level flag cannot express that, and it was wrong in the DANGEROUS
    direction the moment a better model was configured -- silently reporting the
    backend as incapable is safe, silently reporting it as capable is not.
    """
    from .llm_providers import model_is_fix_capable
    if not provider.key_env:            # keyless == self-hosted, model is ours
        from .llm_config import model as _configured
        return model_is_fix_capable(_configured())
    return provider.fix_capable


#: WHAT A PROMPT CARRIES, worst-first. The payload class decides which boundaries may
#: receive it, so a caller naming the wrong one is the whole risk -- hence `run()`
#: defaults to the strictest and a caller must opt DOWN, never up.
#:
#:   SOURCE    entire file contents. fix_generator sends the consumer file verbatim.
#:   METADATA  repository names, file paths, internal symbol names. impact.rank and
#:             pr_prose send these. Not file bodies, but they identify the company and
#:             disclose a private internal API surface, so they are still repository
#:             content and still gated.
#:   PUBLIC    nothing derived from the customer's repositories.
PAYLOAD_SOURCE = "source"
PAYLOAD_METADATA = "metadata"
PAYLOAD_PUBLIC = "public"

#: Payload classes that must not cross a boundary out of the operator's control
#: without an explicit opt-in.
_GATED_PAYLOADS = (PAYLOAD_SOURCE, PAYLOAD_METADATA)


def boundary_of(entry) -> str:
    """Where a chain entry sends its prompt.

    Works for both a registry Provider and a Configured custom gateway, because both
    carry `base_url` and the boundary is a property of the endpoint rather than of the
    type. That is what lets ONE check cover both paths -- and covering both is the
    point: fix_generator tries the Configured entry FIRST, bypassing fix_chain()
    entirely, so a filter applied only to the registry chain would guard the path that
    is not taken.

    Delegates to llm_config.boundary_for so the operator's SELF_HOSTED assertion is
    applied in exactly one place. An earlier draft re-implemented that rule here, and a
    mutation test showed the two copies could disagree -- /test-llm reporting
    `weights_local` while the chain still refused.
    """
    from .llm_config import boundary_for
    return boundary_for(getattr(entry, "base_url", ""))


def may_receive(entry, payload: str) -> tuple:
    """(allowed, reason). Reason is "" when allowed."""
    from .llm_config import HOSTED_OPT_IN_ENV, hosted_opt_in
    from .llm_providers import sees_customer_data

    boundary = boundary_of(entry)
    if not sees_customer_data(boundary):
        return True, ""
    if payload not in _GATED_PAYLOADS:
        return True, ""
    if hosted_opt_in():
        return True, ""
    return False, (
        f"refused: {payload} would be sent to a third party ({boundary}) and "
        f"{HOSTED_OPT_IN_ENV} is not set to 1"
    )


def fix_chain() -> list:
    """Providers trusted to WRITE CODE, in attempt order.

    Excludes anything not trusted to write code -- for self-hosted backends that is a
    judgement about the LOADED MODEL, see _fix_capable. Also excludes providers with no
    credential, so a chain of three names cannot quietly be a chain of one endpoint.
    """
    from .llm_providers import usable
    return [p for p in usable() if _fix_capable(p) and _reachable(p)]


def prose_chain() -> list:
    """Providers allowed to WRITE ENGLISH, in attempt order.

    Deliberately wider than fix_chain(): bad prose is visible to whoever reads the PR
    and cannot break a build, so a weaker model is an acceptable last resort here.
    """
    from .llm_providers import usable
    return [p for p in usable() if _reachable(p)]


def run(call, *, chain: list, budget: Budget = None,
        payload: str = PAYLOAD_SOURCE) -> ChainResult:
    """Try each provider until one answers. Never raises; always states what happened.

    `call(provider) -> str` performs one request and returns the model's text. It is
    injected rather than built here so this module holds the ORDERING and the
    CLASSIFICATION and knows nothing about the wire protocol -- the same separation
    that keeps llm_config the only place that decides how to reach a backend.

    `payload` names what the prompt carries, and THIS IS THE DATA-BOUNDARY CHOKE POINT.
    Every path to a model goes through here -- the registry chain and the Configured
    custom gateway both -- so one check covers both, and enforcing it in fix_chain()
    instead would have missed the Configured entry that fix_generator tries first.

    It defaults to PAYLOAD_SOURCE, the strictest class, so a caller that forgets to say
    is treated as sending the customer's source. Fail-closed is the only safe direction
    for a disclosure check: a wrong default the other way is silent and unrecoverable,
    whereas this one merely falls back to the deterministic path and says why.
    """
    budget = budget or Budget()
    attempts = []

    for provider in chain:
        allowed, why = may_receive(provider, payload)
        if not allowed:
            # STATED, not silently dropped. This lands in stated_outcome(), so the PR
            # body and the activity log both carry the reason -- the same requirement
            # that made an exhausted chain report itself instead of print()ing.
            attempts.append(Attempt(provider.name, "", "refused_data_boundary",
                                    why, 0.0))
            continue
        if not budget.allows(provider.name):
            attempts.append(Attempt(provider.name, "", "skipped",
                                    budget.why_skipped(provider.name), 0.0))
            continue
        started = time.time()
        try:
            text = call(provider)
        except Exception as exc:                                   # noqa: BLE001
            outcome, detail = classify(exc)
            budget.record(provider.name, outcome)
            attempts.append(Attempt(provider.name, "", outcome, detail,
                                    time.time() - started))
            if outcome in TERMINAL:
                # Our payload is malformed. Asking two more providers the same
                # malformed question produces two more identical errors and hides
                # which one was the real cause.
                break
            continue

        budget.record(provider.name, "ok")
        elapsed = time.time() - started
        if not (text or "").strip():
            # An empty body is a failure that looks like a success. The old path
            # would have returned it and let the diff contract reject an "empty
            # fix", reporting the wrong cause.
            attempts.append(Attempt(provider.name, "", "empty_response", "", elapsed))
            continue
        attempts.append(Attempt(provider.name, "", "ok", "", elapsed))
        return ChainResult(text=text, answered_by=provider.name,
                           model="", attempts=tuple(attempts))

    return ChainResult(attempts=tuple(attempts))
