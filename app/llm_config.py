"""Single source of truth for which LLM backend Ripple is talking to.

WHY THIS MODULE EXISTS
----------------------
Three call sites talked to an LLM and each made its own decisions:

    fix_generator.py     anthropic.Anthropic()          model hardcoded
    validated_fix.py     anthropic.Anthropic(api_key=)  model hardcoded
    natural_language.py  requests.post(ANTHROPIC_URL)   URL AND model hardcoded

That is the codebase's dominant failure pattern -- one concept implemented
several times, drifting apart. It has already produced six language detectors of
which production reaches one, two consumer finders, two pattern stores and two
PR-body builders. Adding a fourth variant of "how do we reach the model" would
have repeated it.

THE FORMAT TRAP
---------------
Setting ANTHROPIC_BASE_URL is not sufficient on its own: whatever answers at that
URL must speak the ANTHROPIC wire format (POST /v1/messages with Anthropic
fields). Pointing it at a provider's OpenAI-compatible endpoint fails, and the
error surfaces as a confusing "model may not exist" rather than a format error.

    Anthropic API            speaks Anthropic  -> works
    LiteLLM proxy            translates        -> works
    Ollama                   implements /v1/messages natively -> works
    Gemini /v1beta/openai/   OpenAI format     -> FAILS (looks like a bad model)

HONEST LABELLING
----------------
Ripple's PR body reports how a fix was produced. Before this, a fix was labelled
"LLM-generated (semantic)" with the code naming claude-sonnet-4 -- so pointing the
base URL at a Gemini or Llama backend would have made every PR misreport its own
provenance. That is the same defect class as the "Learning: enabled" footer that
shipped on every live PR until it was removed. backend_label() names what actually
answered.

FREE BY DEFAULT, PAID BY OPT-IN (2026-09-13)
--------------------------------------------
The fallbacks used to be api.anthropic.com and claude-sonnet-4, so the module
FAILED OPEN to a metered third-party API. Nothing was configured -> nothing was
attempted, so that default was unreachable in the empty case; the exposure was the
half-configured one, which is the common one:

    ANTHROPIC_API_KEY set, ANTHROPIC_MODEL unset
        -> billed for a model nobody chose
    an OpenRouter key exported as ANTHROPIC_AUTH_TOKEN, BASE_URL forgotten
        -> customer source POSTed to Anthropic under a non-Anthropic credential.
           Auth fails, but the send has already happened.

Now the fallbacks are a free, self-hosted, Apache-2.0 model, and a paid backend needs
BOTH RIPPLE_ALLOW_PAID_MODEL=1 and an explicit ANTHROPIC_MODEL. is_configured() is the
single place that rule lives, because it is the gate every call site already reads --
webhook, fix_generator and natural_language import it and nothing else. Three copies
of the check would be this codebase's dominant failure pattern.

Refusals are stated, never silent: refusal_reason() distinguishes "nothing configured"
from "configured, paid, refused", because the second is an operator mistake worth
naming rather than reporting as an absent LLM.
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

from .llm_providers import (DEFAULT_SELF_HOSTED_BASE_URL,
                            DEFAULT_SELF_HOSTED_MODEL, HOSTED_UNKNOWN,
                            WEIGHTS_LOCAL, data_boundary_for_base_url,
                            is_paid_base_url, model_is_commercially_licensed,
                            sees_customer_data)

#: The PAID hosted API. Named here so paid detection and is_anthropic() can identify
#: it -- deliberately NOT a default. It WAS the default until 2026-09-13, which meant
#: `model()` returned a metered Claude model and `base_url()` returned this endpoint
#: whenever the operator had a credential but had not named a model. Two ways that bit:
#:
#:   ANTHROPIC_API_KEY set, ANTHROPIC_MODEL unset
#:       -> billed for a model nobody chose
#:   an OpenRouter token exported as ANTHROPIC_AUTH_TOKEN, BASE_URL forgotten
#:       -> customer source POSTed to Anthropic under a credential that is not
#:          Anthropic's. The request fails auth, but the SEND has already happened.
#:
#: Failing open to a paid third party is the wrong direction for both cost and data
#: handling, so the fallbacks below are free and local, and this is opt-in.
ANTHROPIC_PAID_BASE = "https://api.anthropic.com"

#: Free, self-hosted, commercially licensed. Imported from the catalogue rather than
#: spelled out here, so the default cannot drift away from the measurements and licence
#: records that justify it.
DEFAULT_BASE = DEFAULT_SELF_HOSTED_BASE_URL
DEFAULT_MODEL = DEFAULT_SELF_HOSTED_MODEL

#: Set to "1" to permit a paid, per-token, third-party backend. Absent, is_configured()
#: refuses one and refusal_reason() says why.
PAID_OPT_IN_ENV = "RIPPLE_ALLOW_PAID_MODEL"

#: Set to "1" to permit a THIRD-PARTY hosted model to receive repository content --
#: source files, repo names, paths, internal symbol names. Separate from the paid
#: opt-in because free and private are different questions: a free hosted tier costs
#: nothing AND sees the prompt, and free tiers commonly reserve the right to log or
#: train on what they are sent. "We only use free models" is a cost claim, not a
#: data-handling one.
HOSTED_OPT_IN_ENV = "RIPPLE_ALLOW_HOSTED_MODEL"

#: Set to "1" to assert that ANTHROPIC_BASE_URL is the operator's OWN infrastructure
#: even though it is a public address. Needed because the boundary resolver is
#: syntactic -- a model served at llm.mycompany.com is indistinguishable from a
#: third-party API by inspection, and guessing "local" there would send source off-network
#: on the strength of a hostname. This is the operator making the claim explicitly.
SELF_HOSTED_ASSERT_ENV = "RIPPLE_SELF_HOSTED"


def api_key() -> str:
    """Auth token. ANTHROPIC_AUTH_TOKEN wins, as proxies commonly use it.

    Do NOT set both this and ANTHROPIC_API_KEY when talking to a proxy -- some
    clients treat that as an auth conflict.
    """
    return (os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or os.environ.get("ANTHROPIC_API_KEY", ""))


def credential_for(env_name: str) -> str:
    """The credential for ONE named provider, resolved here and nowhere else.

    Added for the provider chain. Every chain entry needs its OWN key, and the
    credential-scan gate forbids reading one anywhere but this module -- which is
    working as intended: the first draft of the chain skipped this and reused
    base_url() for every provider, so each entry hit the SAME endpoint and the result
    was attributed to whichever provider happened to be first. A run against a local
    Ollama reported `answered by openrouter` with no OpenRouter key present.

    That is the same misreporting backend_label() exists to prevent, reached by a
    different route: not a mislabelled model, but a correctly-labelled model that was
    never called.

    An empty name means keyless (a self-hosted endpoint that authenticates nothing),
    which is a valid configuration -- see is_self_hosted().
    """
    if not env_name:
        return ""
    return os.environ.get(env_name, "")


def base_url() -> str:
    return os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/") or DEFAULT_BASE


def messages_url() -> str:
    """Full endpoint for a raw HTTP caller (natural_language.py)."""
    return f"{base_url()}/v1/messages"


def model() -> str:
    return os.environ.get("ANTHROPIC_MODEL", "").strip() or DEFAULT_MODEL


def is_anthropic() -> bool:
    """True only when the real Anthropic API is answering."""
    return urlparse(base_url()).netloc.endswith("anthropic.com")


def paid_opt_in() -> bool:
    """Has the operator explicitly accepted a paid, third-party backend?

    Exactly "1". Not truthiness: "0", "false" and "no" all read as refusal to a human,
    and a check that accepted them would spend the customer's money on a typo.
    """
    return os.environ.get(PAID_OPT_IN_ENV, "").strip() == "1"


def is_paid_backend() -> bool:
    """Does the RESOLVED backend bill per token and send source to a third party?

    Reads the resolved base_url rather than the raw env var, so the answer covers the
    fallback as well as an explicit setting. Unknown hosts are not paid -- see
    llm_providers.is_paid_base_url for why that asymmetry is the safe one.
    """
    return is_paid_base_url(base_url())


def hosted_opt_in() -> bool:
    """Has the operator accepted a third party receiving repository content?

    Exactly "1", for the same reason paid_opt_in() is: "0" and "false" read as refusal
    to a human, and a truthiness check would disclose a customer's source on a typo.
    """
    return os.environ.get(HOSTED_OPT_IN_ENV, "").strip() == "1"


def self_hosted_asserted() -> bool:
    """Has the operator declared their base_url to be their own infrastructure?"""
    return os.environ.get(SELF_HOSTED_ASSERT_ENV, "").strip() == "1"


def boundary_for(url: str) -> str:
    """Who receives a prompt sent to THIS endpoint, honouring the operator's assertion.

    THE ONLY PLACE the SELF_HOSTED assertion is applied. It takes an arbitrary URL
    rather than reading the configured one because two callers need it and they need it
    for different endpoints: `data_boundary()` asks about the configured backend, while
    `llm_chain.may_receive` asks about each chain entry, which may be a registry
    provider unrelated to ANTHROPIC_BASE_URL.

    Those two had separate copies of this rule for one commit, and a mutation test
    caught it: breaking the copy here left enforcement intact, so `/test-llm` could
    report `weights_local` while the chain still refused -- one concept in two places,
    disagreeing, which is the pattern this codebase keeps collapsing.

    The assertion only widens in the safe direction: it can reclassify an
    UNIDENTIFIABLE public host as local, because the operator knows something
    inspection cannot establish. It can never declare a catalogued third party local --
    api.anthropic.com asserted as self-hosted is still api.anthropic.com, and honouring
    that would turn an operator mistake into a silent disclosure.
    """
    resolved = data_boundary_for_base_url(url)
    if self_hosted_asserted() and resolved == HOSTED_UNKNOWN:
        return WEIGHTS_LOCAL
    return resolved


def data_boundary() -> str:
    """Who receives a prompt sent to the RESOLVED backend."""
    return boundary_for(base_url())


def is_hosted_backend() -> bool:
    """Would repository content leave the operator's control to reach this backend?"""
    return sees_customer_data(data_boundary())


def client_api_key() -> str:
    """The credential to hand an SDK client. Use this, never api_key(), at a call site.

    WHY THIS IS SEPARATE FROM api_key()
    api_key() answers "did the operator supply a token". This answers "what should
    the client be constructed with", and for a self-hosted backend those differ.

    The Anthropic SDK refuses to construct with an empty api_key:

        Could not resolve authentication method. Expected one of api_key,
        auth_token, or credentials to be set.

    even when base_url points at a server that authenticates nothing. Measured
    against a real local model (Ollama serving native /v1/messages at
    localhost:11434): is_configured() returned True, the gate opened, and every
    request then failed and fell through to the deterministic template -- so the
    self-hosted path looked configured and silently did nothing.

    That is the same disagreement this module was created to end, one layer lower:
    the GATE accepted keyless self-hosted while the CALL SITE could not do keyless.
    A placeholder resolves it, and it is safe because a self-hosted endpoint ignores
    the header.

    NOTHING IS HANDED OUT WHEN NOTHING IS CONFIGURED. If there is no key and no
    self-hosted base_url, this returns "" -- so an unconfigured deployment cannot
    start talking to api.anthropic.com with a fake credential. The placeholder is
    granted ONLY because the operator named their own host.
    """
    real = api_key()
    if real:
        return real
    if is_self_hosted():
        # Any non-empty value satisfies the SDK; the local server ignores it.
        return "self-hosted-no-auth"
    return ""


def is_self_hosted() -> bool:
    """True when ANTHROPIC_BASE_URL points somewhere other than Anthropic.

    A locally run model -- Ollama, llama.cpp's server, a LiteLLM proxy, or a
    sidecar service on a private network -- authenticates nothing. Requiring a key
    for those would make a self-hosted deployment silently fall through to the
    deterministic template, which is the shape where the gate and the call site
    disagreed about whether a key existed.
    """
    return bool(os.environ.get("ANTHROPIC_BASE_URL", "").strip()) and not is_anthropic()


def paid_backend_lacks_explicit_model() -> bool:
    """A paid backend that has not been told WHICH model to bill for.

    The free defaults name a local model, which a paid hosted API will reject as
    nonexistent -- so once the fallbacks became free, "paid endpoint + no model named"
    stopped being a working configuration and started being a confusing 404.

    Refusing it rather than substituting a paid model name is the deliberate choice.
    Opting in to cost should not also pick the cost: Claude models differ in price by
    more than an order of magnitude, so a default here would spend the operator's money
    on a decision they never made. This is the same reasoning as never reintroducing
    ANTHROPIC_DEFAULT_MODEL.
    """
    return is_paid_backend() and not os.environ.get("ANTHROPIC_MODEL", "").strip()


def is_configured() -> bool:
    """Is there a backend to talk to at all?

    KEY *OR* SELF-HOSTED, NOT KEY ALONE. This returned bool(api_key()), so a local
    model reachable at ANTHROPIC_BASE_URL was indistinguishable from no LLM at all.

    The asymmetry is deliberate and is the safety property: reaching the real
    Anthropic API still requires a key, so no source code can be sent to a
    third-party provider by accident. A keyless configuration is only accepted when
    the operator has explicitly named a different host -- i.e. their own.

    AND A PAID BACKEND ALSO REQUIRES THE OPT-IN (added 2026-09-13). This is the ONE
    choke point for that rule, because it is the gate every call site already reads --
    webhook._generate_fix_with_rag_fallback, fix_generator.generate_fix and
    natural_language all import this and nothing else. Enforcing it at each of those
    three call sites instead would be the codebase's dominant failure pattern: one
    concept implemented three times, drifting apart. It is the same lesson as
    overriding a renderer's single normalisation hook rather than sanitising at every
    call site that writes a string.

    A refused paid backend is NOT silent -- refusal_reason() states it, and reporting
    "no LLM configured" when a key is in fact present would be the fail-silent shape
    tools/audit_fail_silent.py exists to catch.
    """
    if is_paid_backend() and not paid_opt_in():
        return False
    if paid_backend_lacks_explicit_model():
        return False
    return bool(api_key()) or is_self_hosted()


def refusal_reason() -> str:
    """Why there is no usable backend, or "" when one is configured.

    Exists so a refusal can be reported rather than inferred. "Not configured" and
    "configured, paid, and not opted in" look identical to a caller reading a bool,
    and the second one is an operator mistake worth naming: they exported a key and
    reasonably expect it to be used.
    """
    if is_configured():
        return ""
    if is_paid_backend() and not paid_opt_in():
        return (
            f"backend {model()} via {urlparse(base_url()).netloc or base_url()} is a "
            f"paid, per-token, third-party API and {PAID_OPT_IN_ENV} is not set to 1, "
            f"so it was refused rather than billed. Either set {PAID_OPT_IN_ENV}=1 to "
            f"accept the cost and sending source off-network, or point "
            f"ANTHROPIC_BASE_URL at a self-hosted model (default: {DEFAULT_BASE} "
            f"running {DEFAULT_MODEL})."
        )
    if paid_backend_lacks_explicit_model():
        return (
            f"backend at {urlparse(base_url()).netloc or base_url()} is a paid API but "
            f"ANTHROPIC_MODEL is not set. The default model is {DEFAULT_MODEL}, which "
            f"is a local model a hosted API will reject as nonexistent. Name the model "
            f"you intend to pay for explicitly."
        )
    return (
        f"no backend configured: set ANTHROPIC_BASE_URL to a self-hosted endpoint "
        f"(default {DEFAULT_BASE}), or supply a credential. Deterministic codemods "
        f"still run without any model."
    )


def backend_label() -> str:
    """Human-readable provenance, e.g. for a PR body.

    Names the model AND the host, because the whole point of the override is that
    the model answering may not be the one the code was written for.

    NOTHING CONFIGURED IS SAID PLAINLY. Since 2026-09-13 the fallbacks are a real
    self-hosted model rather than a paid API, so an unconfigured install would
    otherwise render "qwen2.5-coder:7b via localhost:11434" into a PR body and claim a
    model answered when none was reachable. That is the misreporting this function was
    written to prevent, reached from the other side: not a mislabelled backend, but a
    correctly-labelled backend that was never called.
    """
    if not is_configured():
        return "no model (deterministic only)"
    if is_anthropic():
        return f"{model()} (Anthropic)"
    host = urlparse(base_url()).netloc or base_url()
    return f"{model()} via {host}"


def describe() -> dict:
    """Diagnostics for /test-llm and the local harness."""
    return {
        "base_url": base_url(),
        "model": model(),
        "is_anthropic": is_anthropic(),
        "configured": is_configured(),
        "backend_label": backend_label(),
        "is_paid_backend": is_paid_backend(),
        "paid_opt_in": paid_opt_in(),
        "data_boundary": data_boundary(),
        "is_hosted_backend": is_hosted_backend(),
        "hosted_opt_in": hosted_opt_in(),
        "self_hosted_asserted": self_hosted_asserted(),
        "repository_content_may_leave_network": (
            is_hosted_backend() and hosted_opt_in()),
        "model_commercially_licensed": model_is_commercially_licensed(model()),
        "refusal_reason": refusal_reason(),
        "defaults": {"base_url": DEFAULT_BASE, "model": DEFAULT_MODEL,
                     "note": "free, self-hosted, Apache-2.0"},
        "note": (
            "endpoint must speak the ANTHROPIC wire format; an OpenAI-compatible "
            "endpoint will fail with a misleading 'model may not exist' error"
        ),
    }
