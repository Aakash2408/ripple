"""Let a model write the PR narrative, and refuse it when it overclaims.

WHAT THE MODEL IS AND IS NOT ALLOWED TO WRITE
The PR body has two kinds of content and they carry different risk:

    THE LEDGER     what was edited, what was refused, how many lines, which
                   validator ran, what the confidence was. Derived from structured
                   facts. Stays template-generated.
    THE NARRATIVE  why this break matters, what a reviewer should look at first,
                   what to do about the parts that were refused. Prose.

Only the narrative is model-authored. That boundary is the whole design, because a
model writing the ledger is a NEW FABRICATION SURFACE for the exact defect the codemod
work spent three stages removing: explanations that asserted a fixed list of cleaned
shapes regardless of what happened, on remove_field, then remove_type, then
remove_enum_value. Handing the ledger to a model would reintroduce it in a form no
regex could pin, since the wording would differ every time.

So the model receives the facts and is asked to render them, never to add to them.

AND IT IS CHECKED, BECAUSE ASKING IS NOT ENFORCING
A prompt saying "do not overclaim" is a request. `FORBIDDEN_CLAIMS` is the enforcement:
phrases that assert verification, completeness, or learning that this pipeline cannot
support. If the model emits one, its prose is REJECTED and the deterministic narrative
is used instead.

Rejected rather than sanitised, deliberately. Stripping the offending phrase would
leave prose written around a claim it is no longer making, and would hide that the
model overclaimed at all -- the failure becomes invisible, which is the shape of every
defect in this codebase's history. A rejection is recorded and stated.

WHY PROSE MAY USE THE WEAKER MODELS
`llm_chain.prose_chain()` is wider than `fix_chain()` and includes the local 3B model
that is excluded from fix generation. Measured 2026-08-26: that model produced
compiling-but-wrong CODE on two of three codemod-refused shapes, and nothing
downstream could tell. Bad PROSE has the opposite property -- a reviewer reads it, and
it cannot break a build. The asymmetry is the justification, not convenience.
"""
from __future__ import annotations

from dataclasses import dataclass, field as _field

#: Phrases the narrative may not contain, and why each one is unearned.
#:
#: Every entry here was chosen because this pipeline cannot support the claim, not
#: because the wording is unattractive. The first three groups mirror the strings
#: already pinned absent by test_pr_body_makes_no_unearned_learning_claims -- that gate
#: pins the TEMPLATE footer, and without this the model could reintroduce the same
#: claims in its own words and pass.
FORBIDDEN_CLAIMS: dict = {
    # Verification the narrative cannot know happened. The AUTO heading states it
    # separately, from a real validator run with evidence.
    "verified": "only a validator run can claim verification, and it says so itself",
    "i tested": "nothing in this path runs the consumer's tests",
    "tests pass": "nothing in this path runs the consumer's tests",
    "compiles cleanly": "the validator reports this, with evidence, or not at all",
    # Completeness. A refusal list exists precisely because completeness is often
    # false, and the ledger already states the true count.
    "all references": "the ledger states the count; refusals mean this is often false",
    "every reference": "the ledger states the count; refusals mean this is often false",
    "fully resolved": "a partial fix is the common case and must not read as total",
    "completely removed": "a partial fix is the common case and must not read as total",
    # Learning. No merge history is tracked, which is why the footer claims were
    # deleted in the first place.
    "similar fixes": "no prior merge is tracked, so similarity cannot be asserted",
    "previously merged": "no prior merge is tracked",
    "learned from": "the pattern store is empty until a real PR merges",
    # Assurance about the reviewer's own repository.
    "safe to merge": "that is the reviewer's decision, and the heading states the level",
    "no risk": "unquantifiable here, and the confidence table already speaks",
    # AGENCY INVERSION. Measured against the local 3B model, which opened with "we
    # have removed the `phone_number` field from the API". Ripple did not remove it --
    # the UPSTREAM contract did, and Ripple is editing a consumer in response. Getting
    # that backwards tells the reviewer this PR is the cause of their problem rather
    # than a reaction to it, which is worse than vague: it is wrong about who did what.
    "we have removed": "Ripple reacts to an upstream removal; it does not remove the field",
    "we removed": "Ripple reacts to an upstream removal; it does not remove the field",
    "we have deleted": "Ripple reacts to an upstream change; it does not change the API",
    "this pr removes the field": "the field was removed upstream, before this PR existed",
}


@dataclass(frozen=True)
class ProseResult:
    """The narrative, where it came from, and why if it was not the model."""
    text: str
    source: str                      # "model" | "template"
    reason: str = ""                 # why the model's prose was not used
    violations: tuple = _field(default_factory=tuple)
    attempts: tuple = _field(default_factory=tuple)

    def provenance_line(self) -> str:
        """One line naming who wrote the narrative. Goes in the PR body.

        Present on BOTH paths. A body that names the author only when it happens to be
        a model teaches the reader that unlabelled prose is human-written, which is
        the inference this must not create.
        """
        if self.source == "model":
            return f"_Narrative written by {self.reason or 'a language model'}; "\
                   "the facts above are generated from the diff._"
        return "_Narrative generated from the diff, not model-written"\
               + (f" ({self.reason})_" if self.reason else "._")


def check(text: str) -> list:
    """Every forbidden claim present in `text`, as (phrase, why) pairs.

    Case-insensitive and substring-based on purpose: the goal is to catch the CLAIM,
    and a model that writes "Verified that" should be caught by "verified".
    """
    low = (text or "").lower()
    return [(phrase, why) for phrase, why in sorted(FORBIDDEN_CLAIMS.items())
            if phrase in low]


def _facts_block(facts: dict) -> str:
    """The ground truth, formatted for the prompt. Nothing else is supplied."""
    lines = []
    for key in ("change_type", "field_name", "language", "consumer_file"):
        if facts.get(key):
            lines.append(f"- {key}: {facts[key]}")
    for label, key in (("edited", "edits"), ("refused", "refusals"),
                       ("noted", "notes")):
        items = facts.get(key) or []
        if items:
            lines.append(f"- {label} ({len(items)}):")
            for item in items[:8]:
                shape = item.get("shape") if isinstance(item, dict) else item
                lines.append(f"    - {str(shape)[:120]}")
    if facts.get("residual_refs"):
        lines.append(f"- references still present: {len(facts['residual_refs'])}")
    return "\n".join(lines) or "- no structured detail available"


def _template_narrative(facts: dict) -> str:
    """The deterministic fallback. Says less, and cannot be wrong.

    Also the answer when no model is configured, so the PR body shape does not depend
    on whether a backend happened to be reachable.
    """
    field = facts.get("field_name") or "the field"
    refusals = facts.get("refusals") or []
    parts = [
        f"The upstream contract no longer provides `{field}`, so this consumer "
        f"references something that will not be there."
    ]
    if refusals:
        parts.append(
            f"{len(refusals)} reference(s) were left alone because removing them "
            f"changes behaviour rather than just syntax -- those need a decision "
            f"from someone who knows the intent."
        )
    else:
        parts.append("Start the review at the changed lines in the diff.")
    return " ".join(parts)


def render(facts: dict, *, call_chain=None) -> ProseResult:
    """Ask a model for the narrative; fall back deterministically, and say which.

    `call_chain(prompt) -> ChainResult` is injected so this module holds the PROMPT and
    the CLAIM CHECK and nothing about transport -- the same separation that keeps
    llm_config the only module deciding how to reach a backend.
    """
    fallback = _template_narrative(facts)

    if call_chain is None:
        return ProseResult(text=fallback, source="template",
                           reason="no model was asked")

    prompt = f"""You are writing the explanatory part of a pull request that fixes a
consumer of a changed API. Write for an engineer who has never seen this tool.

WHO DID WHAT -- getting this backwards is the worst thing you can do here:
- Someone else changed the upstream API. That already happened.
- This pull request edits a CONSUMER of that API so it stops referencing what is gone.
- You are NOT removing the field. You are reacting to its removal.

FACTS -- these are the only things known to be true. Do not add to them:
{_facts_block(facts)}

WRITE:
- Two or three sentences of plain prose.
- Why this break matters to this consumer, and what the reviewer should look at first.
- If anything was refused, say plainly that it needs a human decision and why.

DO NOT:
- Claim anything was verified, tested, compiled or validated.
- Claim all or every reference was handled.
- Claim similarity to past fixes, or that anything was learned.
- Say it is safe to merge.
- SPECULATE ABOUT WHAT THE FIELD OR THE CODE IS FOR. You have not seen the codebase
  and you do not know what it validates, who calls it, or what it means to the
  business. Inventing that is the most likely way for you to be confidently wrong,
  and a reviewer who spots one invented detail will not trust the rest.
- Describe consequences you cannot observe, such as effects on reliability, customers
  or revenue.
- Restate the facts as labelled values. "The field_name is 'x' and the language is
  Python" is a data dump, not an explanation -- the ledger below your text already
  lists them, and repeating them makes the PR read like a form.
- Count the edits or refusals. The ledger states the counts; if you also state them
  and disagree, the reader has two numbers and no way to choose.
- Use bullet points, headings, or a preamble. Prose only.

NARRATIVE:"""

    result = call_chain(prompt)
    attempts = tuple(getattr(result, "attempts", ()) or ())

    if not getattr(result, "ok", False):
        return ProseResult(text=fallback, source="template",
                           reason=getattr(result, "stated_outcome", lambda: "")(),
                           attempts=attempts)

    text = (result.text or "").strip()
    violations = check(text)
    if violations:
        # REJECTED, not sanitised. See the module docstring: removing the phrase
        # would leave prose built around a claim it no longer makes, and would hide
        # that the model overclaimed.
        phrases = ", ".join(p for p, _ in violations)
        return ProseResult(
            text=fallback, source="template",
            reason=f"model prose rejected -- unearned claim(s): {phrases}",
            violations=tuple(violations), attempts=attempts)

    return ProseResult(text=text, source="model",
                       reason=getattr(result, "answered_by", ""), attempts=attempts)
