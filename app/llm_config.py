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
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

ANTHROPIC_DEFAULT_BASE = "https://api.anthropic.com"
ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-4-20250514"


def api_key() -> str:
    """Auth token. ANTHROPIC_AUTH_TOKEN wins, as proxies commonly use it.

    Do NOT set both this and ANTHROPIC_API_KEY when talking to a proxy -- some
    clients treat that as an auth conflict.
    """
    return (os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or os.environ.get("ANTHROPIC_API_KEY", ""))


def base_url() -> str:
    return os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/") or ANTHROPIC_DEFAULT_BASE


def messages_url() -> str:
    """Full endpoint for a raw HTTP caller (natural_language.py)."""
    return f"{base_url()}/v1/messages"


def model() -> str:
    return os.environ.get("ANTHROPIC_MODEL", "").strip() or ANTHROPIC_DEFAULT_MODEL


def is_anthropic() -> bool:
    """True only when the real Anthropic API is answering."""
    return urlparse(base_url()).netloc.endswith("anthropic.com")


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


def is_configured() -> bool:
    """Is there a backend to talk to at all?

    KEY *OR* SELF-HOSTED, NOT KEY ALONE. This returned bool(api_key()), so a local
    model reachable at ANTHROPIC_BASE_URL was indistinguishable from no LLM at all.

    The asymmetry is deliberate and is the safety property: reaching the real
    Anthropic API still requires a key, so no source code can be sent to a
    third-party provider by accident. A keyless configuration is only accepted when
    the operator has explicitly named a different host -- i.e. their own.
    """
    return bool(api_key()) or is_self_hosted()


def backend_label() -> str:
    """Human-readable provenance, e.g. for a PR body.

    Names the model AND the host, because the whole point of the override is that
    the model answering may not be the one the code was written for.
    """
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
        "note": (
            "endpoint must speak the ANTHROPIC wire format; an OpenAI-compatible "
            "endpoint will fail with a misleading 'model may not exist' error"
        ),
    }
