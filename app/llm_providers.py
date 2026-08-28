"""Which LLM backends exist, what wire format each speaks, and which are usable TODAY.

WHY THIS IS SEPARATE FROM llm_config.py
`llm_config` answers "what am I configured to talk to right now" -- one backend, read
from the environment. This answers "what could I talk to, and what would it cost me to
add one" -- a catalogue. Keeping the catalogue in `llm_config` would have put a table
of five providers next to the three env-var readers that serve one, which is how a
module acquires a second concern and then a second source of truth.

THE MEASUREMENT THAT MOTIVATED IT
`llm_config`'s docstring records a FORMAT TRAP: the endpoint must speak the Anthropic
wire format (`POST /v1/messages`), and an OpenAI-compatible endpoint fails with a
misleading "model may not exist". That is still true, and it was taken to mean every
free provider needs a second client written first.

Measured 2026-08-26, by POSTing to each endpoint with no key and fingerprinting the
error envelope -- an Anthropic envelope has a TOP-LEVEL `"type": "error"`, an OpenAI
one does not:

    openrouter.ai/api/v1/chat/completions    OpenAI envelope
    openrouter.ai/api/v1/messages            ANTHROPIC envelope   <-- the hole
    api.groq.com/openai/v1/messages          404, no such route
    api.cerebras.ai/v1/messages              non-JSON

OpenRouter serves a native Anthropic Messages endpoint, confirmed against its own API
reference. So `llm_config` reaches it UNMODIFIED -- verified: with
`ANTHROPIC_BASE_URL=https://openrouter.ai/api`, `messages_url()` returns
`https://openrouter.ai/api/v1/messages`, `is_configured()` is True, and
`backend_label()` already reports the model and host honestly.

The trap has one exit, and it was already in the code.

WHY `usable_today` IS DERIVED AND NOT A FIELD
A provider is usable when SOMETHING HERE CAN SPEAK ITS FORMAT -- not when someone
writes `usable=True` next to it. `FORMAT_CLIENTS` maps a wire format to the function
that implements it, `usable_today()` reads that map, and a gate checks that every
function named there exists, is declared in the reachability audit, and is called from
inside `generate_fix` so the diff contract wraps it.

That is the forcing function. Declaring a second format without writing and wiring its
client makes the gate fail rather than making `usable_today()` lie -- which matters,
because the four gates that guard the existing LLM path all key on the single name
`_generate_with_llm` and would have gone on passing while asserting nothing whatsoever
about a second client.
"""
from __future__ import annotations

from dataclasses import dataclass

#: Wire format -> the function that speaks it, module-qualified.
#:
#: ADDING AN ENTRY HERE IS A CAPABILITY CLAIM, and three separate gates check it:
#: the function must exist, it must be declared in
#: tools/audit_safety_reachability.py::FUNCTION_LAYERS, and it must be called from
#: within fix_generator.generate_fix -- because the diff contract that verifies an LLM
#: patch lives there, and a generator called directly would bypass it.
FORMAT_CLIENTS: dict = {
    "anthropic": ("fix_generator", "_generate_with_llm"),
    # "openai": ("fix_generator", "_generate_with_openai"),   <- not written
}


@dataclass(frozen=True)
class ModelFacts:
    """Measured properties of ONE model, on THIS host.

    Every number here came from a real call, not a datasheet. `gen` and `prompt` are
    tokens/second and they differ by more than an order of magnitude across models, so
    guessing them would mis-size every latency decision downstream.

    `fix_capable` is the load-bearing field and `evidence` is why. A model earns True by
    DECLINING the shape that cannot be fixed mechanically -- removing a field that is a
    function parameter, where any edit breaks every caller. That single probe separated
    the models more sharply than any throughput number.
    """
    gen: float          # tokens/sec generating
    prompt: float       # tokens/sec ingesting context -- the binding constraint
    ram_gb: float       # resident while loaded
    fix_capable: bool
    evidence: str


#: Measured 2026-08-26 on 8 cores / 61GB / NO GPU, ~12 GB/s effective bandwidth.
#:
#: WHY THIS TABLE EXISTS AT ALL
#: `Provider.fix_capable` was a provider-level flag carrying model-level reasoning: the
#: ollama entry said "qwen2.5-coder:3b ... NOT as a fix engine", which is a fact about a
#: MODEL attached to a SERVER. Point ANTHROPIC_MODEL at something stronger and the flag
#: was silently wrong in the dangerous direction -- claiming a capable backend is
#: incapable is safe, but the reverse is not, and the same field would have done that
#: once a better model was configured.
#:
#: THE MoE FINDING, since it is counter-intuitive enough to be worth recording
#: On a CPU, generation is memory-bandwidth-bound, so tokens/sec tracks the bytes read
#: per token rather than the parameter count. Mixture-of-experts models read only their
#: ACTIVE experts, so a 30B-A3B beats a dense 7B on speed AND quality:
#:     dense  14B   4.0 tok/s     MoE  16B-A2.4B  13.4 tok/s
#:     dense   7B   7.1 tok/s     MoE  30B-A3B    15.3 tok/s
#: Every dense model above 7B is unusable here; both MoE models are comfortable.
MODEL_MEASUREMENTS: dict = {
    "qwen2.5-coder:3b": ModelFacts(
        gen=12.8, prompt=220.0, ram_gb=2.0, fix_capable=False,
        evidence="deleted the function parameter on the codemod-refused shape "
                 "(`dial(phoneNumber)` -> `dial()`), breaking every caller. Also got "
                 "2 of 3 refused shapes wrong while staying brace-balanced, so the "
                 "diff contract and tsc both accepted the broken output."),
    "deepseek-coder-v2:16b": ModelFacts(
        gen=13.4, prompt=118.0, ram_gb=9.0, fix_capable=False,
        evidence="produced a BYTE-IDENTICAL wrong answer to the 3B on the same shape. "
                 "Being 5x the size changed nothing, which is why capability here is "
                 "measured rather than inferred from parameter count."),
    "qwen3-coder:30b": ModelFacts(
        gen=15.3, prompt=69.0, ram_gb=28.0, fix_capable=True,
        evidence="returned the file UNCHANGED on the function-parameter shape, "
                 "matching the deterministic codemod's refusal. Verified from the "
                 "chain provenance that the model answered rather than the template "
                 "falling back. The only local model to get this right."),
    "qwen2.5-coder:7b": ModelFacts(
        gen=7.1, prompt=71.0, ram_gb=5.0, fix_capable=False,
        evidence="not probed on the refused shape, so it does not get the benefit of "
                 "the doubt -- and it is slower than the 16B MoE anyway, so there is "
                 "no reason to."),
    "qwen2.5-coder:14b": ModelFacts(
        gen=4.0, prompt=31.0, ram_gb=9.0, fix_capable=False,
        evidence="4.0 tok/s makes it unusable regardless of quality: 30s to write "
                 "three sentences. Dense models are bandwidth-bound on this host."),
}

#: The model to configure for code-writing work, from the table above.
RECOMMENDED_FIX_MODEL = "qwen3-coder:30b"

#: And for prose. Deliberately a DIFFERENT model: prose is read by a human and cannot
#: break a build, so the 16B's 1.7x faster context ingestion (118 vs 69 tok/s) is worth
#: more here than the 30B's judgement. This is the same fix/prose asymmetry llm_chain
#: already encodes, now with a model behind each side.
RECOMMENDED_PROSE_MODEL = "deepseek-coder-v2:16b"


def facts_for(model: str):
    """Measured facts for a model, or None if it has never been measured here."""
    return MODEL_MEASUREMENTS.get((model or "").strip())


def model_is_fix_capable(model: str) -> bool:
    """Is this MODEL trusted to write code?

    Unmeasured models are False. Absence of evidence is not clearance -- the same rule
    the capability registry applies to validation, and the reason the 16B is excluded
    despite being five times the size of the 3B.
    """
    facts = facts_for(model)
    return bool(facts and facts.fix_capable)


@dataclass(frozen=True)
class Provider:
    """One backend Ripple could be pointed at.

    `wire_format` is MEASURED, not assumed -- see the module docstring for the probe.
    `note` states what is measured versus documented versus unverified, because the
    difference decides whether the next person has to re-check it.

    `fix_capable` is whether this backend is trusted to WRITE CODE, as opposed to
    English. It is a separate axis from `usable_today` because the two failures are
    different: an unusable provider produces no output, whereas a weak one produces
    output that passes every gate here and is still wrong. Measured 2026-08-26 -- the
    3B local model got two of three codemod-refused shapes wrong while staying
    brace-balanced, so the diff contract and tsc both accepted them. See
    app/llm_chain.py for the full measurement.
    """
    name: str
    base_url: str
    wire_format: str
    key_env: str
    free_tier: bool
    fix_capable: bool
    note: str

    @property
    def usable_today(self) -> bool:
        """Is there a client in this codebase that speaks this provider's format?"""
        return self.wire_format in FORMAT_CLIENTS


#: Ordered by how cheaply they can be reached from the code as it stands.
PROVIDERS: dict = {
    "ollama": Provider(
        name="ollama",
        base_url="http://localhost:11434",
        wire_format="anthropic",
        key_env="",                      # keyless; is_self_hosted() covers it
        free_tier=True,
        fix_capable=False,
        note="MEASURED: serves /v1/messages natively, no proxy needed. Local and "
             "CPU-only on this host (8 cores, 61GB, no GPU, ~12 GB/s effective "
             "bandwidth). Whether it may WRITE CODE depends on the loaded model, not "
             "on this entry -- see MODEL_MEASUREMENTS and llm_chain._fix_capable. "
             "Dense models above 7B are unusable here (14B = 4.0 tok/s); MoE models "
             "are comfortable because only active experts are read per token.",
    ),
    "openrouter": Provider(
        name="openrouter",
        base_url="https://openrouter.ai/api",
        wire_format="anthropic",
        key_env="ANTHROPIC_AUTH_TOKEN",  # sk-or-... goes here; no code change needed
        free_tier=True,
        fix_capable=True,
        note="MEASURED: /v1/messages returns an Anthropic error envelope and is "
             "documented as an Anthropic Messages endpoint. 21 models were free on "
             "2026-08-26, including code specialists and `openrouter/free`, a router "
             "across free models. UNVERIFIED: whether it wants x-api-key (Anthropic "
             "style, which the SDK sends) or Bearer -- a keyless POST with Bearer "
             "returned 'Missing Authentication header'. Needs one real key to settle.",
    ),
    "anthropic": Provider(
        name="anthropic",
        base_url="https://api.anthropic.com",
        wire_format="anthropic",
        key_env="ANTHROPIC_API_KEY",
        free_tier=False,
        fix_capable=True,
        note="The default. Not free, so not a candidate for this work.",
    ),
    "groq": Provider(
        name="groq",
        base_url="https://api.groq.com/openai",
        wire_format="openai",
        key_env="GROQ_API_KEY",
        free_tier=True,
        fix_capable=True,
        note="MEASURED: 404 on /v1/messages -- OpenAI format only. Documented free "
             "tier is the largest of the group and the fastest tokens. NOT usable "
             "until an OpenAI-format client exists, and adding one must close the "
             "four gates that key on the name _generate_with_llm.",
    ),
    "cerebras": Provider(
        name="cerebras",
        base_url="https://api.cerebras.ai",
        wire_format="openai",
        key_env="CEREBRAS_API_KEY",
        free_tier=True,
        fix_capable=True,
        note="MEASURED: no JSON on /v1/messages -- OpenAI format only. Strongest "
             "free reasoning model of the group. Same precondition as groq.",
    ),
}


def usable() -> list:
    """Providers a client here can actually speak to."""
    return [p for p in PROVIDERS.values() if p.usable_today]


def free_and_usable() -> list:
    """The intersection that matters for this work: free AND reachable today."""
    return [p for p in usable() if p.free_tier]


def blocked_on_a_client() -> list:
    """Free providers whose only obstacle is that nothing speaks their format."""
    return [p for p in PROVIDERS.values()
            if p.free_tier and not p.usable_today]


def describe() -> dict:
    """Catalogue diagnostics, for /test-llm and the local harness."""
    return {
        "formats_with_a_client": sorted(FORMAT_CLIENTS),
        "usable": [p.name for p in usable()],
        "free_and_usable": [p.name for p in free_and_usable()],
        "blocked_on_a_client": [p.name for p in blocked_on_a_client()],
    }
