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
from urllib.parse import urlparse

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
class Licence:
    """One weights licence, and whether a COMMERCIAL product may ship against it.

    WHY THIS IS A TABLE AND NOT A STRING ON EACH MODEL
    "free to download" and "free to build a business on" are different questions, and
    the second one is the product's. Qwen publishes most of its weights under Apache-2.0
    but NOT all of them -- from Qwen's own release notes: "All our open-source models,
    except for the 3B and 72B variants, are licensed under Apache 2.0." So two models
    from the same family, same generation, same repository naming scheme, differ on the
    only axis that matters here. A per-model string would have been copied from its
    sibling and been wrong.

    `osi_approved` is tracked separately from `commercial_ok` because they diverge: the
    DeepSeek licence permits commercial use while adding use-based restrictions, so it
    is neither "proprietary" nor "open source" in the sense a procurement review means.
    Collapsing the two into one boolean is what would let that distinction disappear.

    `source` is the URL the terms were read from, because a licence claim nobody can
    re-check is the same as no claim.
    """
    name: str
    commercial_ok: bool
    osi_approved: bool
    source: str
    note: str = ""


#: Read 2026-09-13 from each publisher's own terms. A model may only reference a
#: licence declared here, and a gate fails on an unknown one -- so adding a model
#: forces its licence to be looked up rather than assumed from its family.
LICENCES: dict = {
    "apache-2.0": Licence(
        name="apache-2.0",
        commercial_ok=True,
        osi_approved=True,
        source="https://qwenlm.github.io/blog/qwen2.5/",
        note="Unrestricted commercial use, patent grant, attribution only. The only "
             "licence in this table that raises no question in a procurement review.",
    ),
    "qwen-research": Licence(
        name="qwen-research",
        commercial_ok=False,
        osi_approved=False,
        source="https://qwenlm.github.io/blog/qwen2.5/",
        note="NON-COMMERCIAL. Qwen's release notes carve the 3B and 72B variants out "
             "of Apache-2.0. A model under this licence must never be Ripple's "
             "default or recommendation, and shipping a paid product against it "
             "would be a licence breach rather than a performance tradeoff.",
    ),
    "deepseek": Licence(
        name="deepseek",
        commercial_ok=True,
        osi_approved=False,
        source="https://github.com/deepseek-ai/DeepSeek-V2/blob/main/LICENSE-MODEL",
        note="Commercial use is permitted, but the agreement adds use-based "
             "restrictions and is not OSI-approved. Usable; it is the entry an "
             "enterprise legal review asks about, which is why it is no longer the "
             "recommended prose model.",
    ),
    "proprietary-paid": Licence(
        name="proprietary-paid",
        commercial_ok=True,
        osi_approved=False,
        source="https://www.anthropic.com/legal/commercial-terms",
        note="A metered hosted API rather than weights. Costs money per token and "
             "sends the customer's source to a third party, so it is reachable only "
             "behind the explicit paid opt-in -- see llm_config.paid_opt_in().",
    ),
}


def licence_for(name: str):
    """The declared licence, or None if the id is not in LICENCES."""
    return LICENCES.get((name or "").strip())


#: WHERE THE CUSTOMER'S CODE ENDS UP. Free is not the same question as private, and
#: conflating them is the mistake this classification exists to prevent: a free hosted
#: tier costs nothing AND receives the prompt, and free tiers commonly reserve the
#: right to log or train on what they are sent. Ripple's prompts carry the customer's
#: source. So "we only use free models" is not a data-handling claim, and an enterprise
#: review asks the second question, not the first.
WEIGHTS_LOCAL = "weights_local"    # runs on infrastructure the operator controls
HOSTED_FREE = "hosted_free"        # a third party, free of charge, sees the prompt
HOSTED_PAID = "hosted_paid"        # a third party, billed, sees the prompt
HOSTED_UNKNOWN = "hosted_unknown"  # a public host we cannot identify -- assume third party

#: Host suffixes that mean "not reachable from the public internet", so a model served
#: there is on the operator's own network. `.internal` covers the Railway private
#: network Ripple's own deployment uses.
_PRIVATE_SUFFIXES = (".internal", ".local", ".localdomain", ".lan", ".svc",
                     ".cluster.local")
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]")


def _is_private_host(host: str) -> bool:
    """Is this host unreachable from the public internet?

    Deliberately syntactic rather than a DNS lookup: a resolver call here would make
    the data boundary depend on network state, so the same configuration could classify
    differently between two runs, and the failure would be intermittent.
    """
    host = (host or "").strip().lower().split(":")[0]
    if not host:
        return False
    if host in _LOOPBACK_HOSTS or host.startswith("127."):
        return True
    if host.endswith(_PRIVATE_SUFFIXES):
        return True
    if "." not in host:
        # A bare name resolves only inside a private network (a container alias, a
        # /etc/hosts entry, a Docker service name).
        return True
    # RFC1918 and link-local, matched on the literal form.
    if host.startswith(("10.", "192.168.", "169.254.")):
        return True
    if host.startswith("172."):
        parts = host.split(".")
        if len(parts) > 1 and parts[1].isdigit() and 16 <= int(parts[1]) <= 31:
            return True
    return False


def data_boundary_for_base_url(url: str) -> str:
    """Who receives a prompt sent to this endpoint.

    ORDER MATTERS, and the private-address check comes FIRST for a reason: a catalogued
    provider is identified by host, and an operator running their own gateway on a
    private address is not that provider even if the port matches.

    An unidentifiable PUBLIC host is HOSTED_UNKNOWN, not local. This is deliberately
    the opposite asymmetry from `is_paid_base_url`, which treats an unknown host as
    not-paid. The two differ because the cost of being wrong differs: mislabelling an
    unknown host as free costs nothing, whereas mislabelling it as local would send the
    customer's source to a third party on the strength of a guess. An operator whose
    own model genuinely lives on a public address says so with RIPPLE_SELF_HOSTED=1.
    """
    host = (urlparse(url if "//" in (url or "") else f"//{url or ''}").netloc or "")
    if _is_private_host(host):
        return WEIGHTS_LOCAL
    prov = provider_for_base_url(url)
    if prov is None:
        return HOSTED_UNKNOWN if host else WEIGHTS_LOCAL
    if not prov.key_env:
        return WEIGHTS_LOCAL
    return HOSTED_FREE if prov.free_tier else HOSTED_PAID


def sees_customer_data(boundary: str) -> bool:
    """Does a prompt sent across this boundary leave the operator's control?"""
    return boundary != WEIGHTS_LOCAL


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

    `licence` is the OTHER load-bearing field, and it is not measurable -- it is read
    from the publisher's terms and keyed into LICENCES. It is here rather than inferred
    because throughput decides whether a model is USABLE while the licence decides
    whether it is SHIPPABLE, and only the second one can make the product undeliverable.
    """
    gen: float          # tokens/sec generating
    prompt: float       # tokens/sec ingesting context -- the binding constraint
    ram_gb: float       # resident while loaded
    fix_capable: bool
    evidence: str
    licence: str        # a key into LICENCES


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
        licence="qwen-research",
        evidence="deleted the function parameter on the codemod-refused shape "
                 "(`dial(phoneNumber)` -> `dial()`), breaking every caller. Also got "
                 "2 of 3 refused shapes wrong while staying brace-balanced, so the "
                 "diff contract and tsc both accepted the broken output. SEPARATELY "
                 "and decisively: the 3B variant is carved out of Qwen's Apache-2.0 "
                 "release, so it is non-commercial and could not be recommended even "
                 "if it were the fastest and the best."),
    "deepseek-coder-v2:16b": ModelFacts(
        gen=13.4, prompt=118.0, ram_gb=9.0, fix_capable=False,
        licence="deepseek",
        evidence="produced a BYTE-IDENTICAL wrong answer to the 3B on the same shape. "
                 "Being 5x the size changed nothing, which is why capability here is "
                 "measured rather than inferred from parameter count."),
    "qwen3-coder:30b": ModelFacts(
        gen=15.3, prompt=69.0, ram_gb=28.0, fix_capable=True,
        licence="apache-2.0",
        evidence="returned the file UNCHANGED on the function-parameter shape, "
                 "matching the deterministic codemod's refusal. Verified from the "
                 "chain provenance that the model answered rather than the template "
                 "falling back. The only local model to get this right."),
    "qwen2.5-coder:7b": ModelFacts(
        gen=7.1, prompt=71.0, ram_gb=5.0, fix_capable=False,
        licence="apache-2.0",
        evidence="not probed on the refused shape, so it does not get the benefit of "
                 "the doubt for FIXES and is excluded from the fix chain. It is the "
                 "recommended PROSE model on a different basis: it is the fastest "
                 "Apache-2.0 model here at context ingestion (71 tok/s) that is not "
                 "also the fix model, and bad prose is visible to a reader while bad "
                 "code is not."),
    "qwen2.5-coder:14b": ModelFacts(
        gen=4.0, prompt=31.0, ram_gb=9.0, fix_capable=False,
        licence="apache-2.0",
        evidence="4.0 tok/s makes it unusable regardless of quality: 30s to write "
                 "three sentences. Dense models are bandwidth-bound on this host."),
}

#: The model to configure for code-writing work, from the table above.
#: Apache-2.0, so recommending it carries no licence question.
RECOMMENDED_FIX_MODEL = "qwen3-coder:30b"

#: And for prose. Deliberately a DIFFERENT model, and the reason CHANGED on 2026-09-13.
#:
#: It used to be deepseek-coder-v2:16b, justified by 1.7x faster context ingestion
#: (118 vs 69 tok/s). That justification was sound and is now outranked: the DeepSeek
#: licence permits commercial use but adds use-based restrictions and is not
#: OSI-approved, and it was the single entry an enterprise legal review would stop on.
#: A licence question blocks a sale outright; ingestion speed only makes prose slower.
#:
#: WHAT THE SWITCH COSTS, stated because it is a real regression and not a free win:
#:     ingestion  118 -> 71 tok/s   (still faster than the fix model, so the
#:                                   fix/prose asymmetry this encodes still holds)
#:     generation 13.4 -> 7.1 tok/s (1.9x SLOWER -- a paragraph goes from ~8s to ~15s)
#:
#: 7b rather than 3b despite the 3B ingesting 3x faster: the 3B variant is carved out
#: of Qwen's Apache-2.0 release and is non-commercial, which disqualifies it outright.
#: 14b is Apache-2.0 but ingests at 31 tok/s, slower than the fix model, which would
#: leave the prose recommendation with no basis at all.
RECOMMENDED_PROSE_MODEL = "qwen2.5-coder:7b"

#: What an operator who configures NOTHING gets. Must be free, self-hosted, and
#: commercially licensed -- llm_config imports this rather than naming a model itself,
#: so the default cannot drift away from the catalogue that justifies it.
DEFAULT_SELF_HOSTED_BASE_URL = "http://localhost:11434"
DEFAULT_SELF_HOSTED_MODEL = RECOMMENDED_PROSE_MODEL


def facts_for(model: str):
    """Measured facts for a model, or None if it has never been measured here."""
    return MODEL_MEASUREMENTS.get((model or "").strip())


def model_licence(model: str):
    """The Licence record for a model, or None if the model is unknown here."""
    facts = facts_for(model)
    return licence_for(facts.licence) if facts else None


def model_is_commercially_licensed(model: str) -> bool:
    """May a PAID product ship against this model's weights?

    Unknown models are False, for the same reason `model_is_fix_capable` says no:
    absence of evidence is not clearance. Guessing yes here is a licence breach rather
    than a degraded result, so this is the one place where the conservative direction
    is unambiguous.
    """
    lic = model_licence(model)
    return bool(lic and lic.commercial_ok)


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

    @property
    def data_boundary(self) -> str:
        """Who receives a prompt sent to this provider.

        DERIVED, not a field, for the reason `usable_today` is derived: a provider's
        boundary is a consequence of what it IS -- a public host needing a credential
        is a third party -- and a hand-written field would be copied from a sibling and
        go stale. A hosted provider that stopped requiring a key would silently become
        `weights_local` if this keyed on key_env alone, which is why the resolver checks
        the host first.
        """
        return data_boundary_for_base_url(self.base_url)

    @property
    def sees_customer_data(self) -> bool:
        return sees_customer_data(self.data_boundary)


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
        note="PAID, and no longer the default -- as of 2026-09-13 an unconfigured "
             "install resolves to the free self-hosted backend instead. Reaching this "
             "provider needs a key AND the explicit paid opt-in, because it is the "
             "one entry here that both bills per token and sends the customer's "
             "source to a third party. See llm_config.paid_opt_in().",
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


def provider_for_base_url(url: str):
    """The catalogued provider serving this base URL, or None if it is not one of ours.

    Matched on HOST, not on the full string, because a base URL legitimately varies in
    scheme, trailing slash and path prefix (`https://openrouter.ai/api` vs
    `openrouter.ai/api/`) while the host is what identifies who receives the request.
    """
    host = (urlparse(url if "//" in (url or "") else f"//{url or ''}").netloc or "")
    host = host.strip().lower()
    if not host:
        return None
    for prov in PROVIDERS.values():
        if (urlparse(prov.base_url).netloc or "").lower() == host:
            return prov
    return None


def is_paid_base_url(url: str) -> bool:
    """Does this endpoint bill per token?

    Only a KNOWN non-free provider counts as paid. An unrecognised host is treated as
    NOT paid, and that asymmetry is deliberate: an unknown host is overwhelmingly an
    operator's own Ollama, llama.cpp, LiteLLM proxy or private sidecar -- the case
    `llm_config.is_self_hosted()` exists to serve -- and refusing those would make the
    self-hosted deployment, which is the one this product recommends, the one that
    silently does nothing.

    The cost of the asymmetry is bounded: an unknown host still has to be NAMED by the
    operator before it is reached at all, so nothing is sent anywhere nobody chose.
    """
    prov = provider_for_base_url(url)
    return bool(prov and not prov.free_tier)


def describe() -> dict:
    """Catalogue diagnostics, for /test-llm and the local harness."""
    return {
        "formats_with_a_client": sorted(FORMAT_CLIENTS),
        "usable": [p.name for p in usable()],
        "free_and_usable": [p.name for p in free_and_usable()],
        "blocked_on_a_client": [p.name for p in blocked_on_a_client()],
        "default_backend": {
            "base_url": DEFAULT_SELF_HOSTED_BASE_URL,
            "model": DEFAULT_SELF_HOSTED_MODEL,
            "licence": (model_licence(DEFAULT_SELF_HOSTED_MODEL) or Licence(
                "unknown", False, False, "")).name,
        },
        "recommended": {
            "fix": RECOMMENDED_FIX_MODEL,
            "prose": RECOMMENDED_PROSE_MODEL,
        },
        "models": {
            name: {
                "licence": f.licence,
                "commercial_ok": model_is_commercially_licensed(name),
                "fix_capable": f.fix_capable,
            }
            for name, f in MODEL_MEASUREMENTS.items()
        },
    }
