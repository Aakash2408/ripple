from __future__ import annotations
"""
ripple/app/fix_generator.py

Fix Generator — generates code fixes for consumers affected by API breaking changes.

Uses Claude API to generate minimal, correct fixes.
Falls back to template-based fixes if no API key.

For the YC demo: the magic moment is seeing actual code diffs appear.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .diff_engine import BreakingChange
from .consumer_finder import ConsumerMatch


@dataclass
class GeneratedFix:
    """A generated fix for one consumer file."""
    consumer: ConsumerMatch
    original_code: str
    fixed_code: str
    explanation: str
    diff: str             # unified diff format


def generate_fix(
    consumer: ConsumerMatch,
    breaking_change: BreakingChange,
    use_llm: bool = True,
    original_code: str | None = None,
) -> Optional[GeneratedFix]:
    """
    Generate a fix for a consumer affected by a breaking change.

    Strategy:
    1. Obtain the consumer's current code
    2. LLM if a key is configured and the operation is briefed, else the template
    3. Verify against the diff contract, then return the fixed code + diff

    `original_code` EXISTS BECAUSE THIS FUNCTION WAS CLI-SHAPED. It only ever read
    from disk, so app/webhook.py -- which holds content fetched from a platform API
    and has no local file -- could not call it: the open() would raise IOError and
    it would return None. That is why the LLM branch, the one thing gated by
    `use_llm and _llm_key()`, was unreachable from every webhook. Passing content
    explicitly is what makes the guarded path usable from a request, and the
    default of None keeps the CLI reading from disk exactly as before.
    """
    # Read the consumer file only when the caller has not supplied its content.
    if original_code is None:
        try:
            with open(consumer.file_path, "r") as f:
                original_code = f.read()
        except IOError:
            return None
    
    # Gate on the SAME resolution the call site uses. Reading
    # ANTHROPIC_API_KEY here while _generate_with_llm builds its client from
    # llm_config.api_key() meant an ANTHROPIC_AUTH_TOKEN setup fell through to
    # the template silently -- the gate and the call disagreed about whether a
    # key existed.
    #
    # is_configured() rather than api_key(): a SELF-HOSTED model authenticates
    # nothing, so keying the gate on a token would make a local deployment fall
    # through to the template for the same reason. Reaching the real Anthropic API
    # still requires a key, so nothing can be sent to a third party by accident.
    from .llm_config import is_configured as _llm_ready
    from_llm = bool(use_llm and _llm_ready())
    if from_llm:
        fixed_code, explanation = _generate_with_llm(
            original_code, consumer, breaking_change
        )
    else:
        fixed_code, explanation = _generate_with_template(
            original_code, consumer, breaking_change
        )
    
    if not fixed_code or fixed_code == original_code:
        return None

    # RESTORE THE TRAILING NEWLINE CONVENTION, for EVERY generator.
    #
    # `_generate_with_llm` calls .strip() on the response to shed surrounding
    # whitespace and code fences, which also removes the file's final newline. An
    # otherwise CORRECT live fix was then rejected by the diff contract with
    # "REMOVED text that does not reference 'phoneNumber': '\n'". The contract was
    # right -- losing it puts "\ No newline at end of file" in the diff, an unrelated
    # change. The loss was ours, not the model's.
    #
    # This lives HERE rather than inside the LLM branch, where it was first written.
    # A test that monkeypatches the generator bypassed it entirely, which was the
    # signal that it also protected only one of the two paths. Normalising once at
    # the point both paths converge is the correct placement, and relaxing the
    # verifier to ignore trailing whitespace would have hidden a real class of
    # unrelated change.
    if original_code.endswith("\n") and not fixed_code.endswith("\n"):
        fixed_code += "\n"
        if fixed_code == original_code:
            return None                  # the only difference WAS the newline

    # THE DIFF CONTRACT, ON EVERY PATH -- INCLUDING THE LLM.
    #
    # It was wired inside fix_templates._remove_field_typescript, which the LLM
    # branch never reaches: _generate_with_llm returns its output directly and only
    # falls back to a template on EXCEPTION. So the deterministic generator was
    # checked and the probabilistic one was not, which is exactly backwards.
    #
    # Measured, not hypothesised. Asked to REMOVE `phoneNumber`, a live Gemini call
    # through the LiteLLM proxy ADDED a parameter instead:
    #
    #   - export function formatContact(user: User): string {
    #   + export function formatContact(user: User, phoneNumber: string): string {
    #
    # That breaks every caller. `tsc --noEmit` returned VALID -- adding a parameter
    # and using it is perfectly well-typed -- and only the diff contract objected,
    # with "INSERTED text into a line, which a removal never does". Preserved as
    # known_bad_fix_007 in tools/audit_negative_corpus.py.
    #
    # TWO GATES, both of which matter:
    #
    #   REMOVALS ONLY. The contract forbids insertions. An add_required fix inserts
    #   by definition, so applying this to one would reject every correct fix.
    #
    #   ONLY LANGUAGES WITH A REAL SCANNER. The gate is source_regions.SCANNED
    #   rather than a literal tuple, so adding a scanner is the ONE edit that widens
    #   coverage and a language can never be admitted here without one. It used to
    #   read ("typescript", "javascript") with a note that Python needed teaching
    #   first -- and it did: scanning Python with the TypeScript rules means
    #   `# phone_number is gone` is not a comment, the surviving mention reads as
    #   CODE, and the "still present in CODE" rule REJECTS A CORRECT FIX. Measured
    #   both ways before widening.
    from .change_types import canonical_op as _canonical_op
    from .source_regions import SCANNED as _SCANNED

    # Read DECLARED fields directly. `getattr(bc, "field_name", "")` was caught by
    # test_no_phantom_getattr_on_breaking_change and rightly: a default silently
    # turns a renamed or missing field into "no field name", which disables this
    # whole check without anyone noticing. Both attributes are on the dataclass, so
    # a real absence should raise.
    field = breaking_change.field_name or ""
    lang = (consumer.language or "").lower()
    # A marker-only result changes no code -- it inserts comments the removal
    # contract (which forbids insertions) would reject. It is checked by
    # _is_marker_only instead: the original must survive line for line.
    # The TEMPLATE path verifies its own removals (_remove_field_fix: each
    # step through its own checker, unverifiable ones only marked), and its
    # output can legitimately combine a structural GraphQL removal with marker
    # lines -- which a single removal contract would reject. So this guards
    # what nothing else checks: model output.
    if (field and lang in _SCANNED and from_llm
            and not _is_marker_only(original_code, fixed_code)
            and _canonical_op(breaking_change.change_type or "") == "remove_field"):
        verdict = _diff_check_any_spelling(original_code, fixed_code, field, lang)
        if not verdict.ok:
            # Refuse the whole patch. Returning None is what the caller already
            # treats as "no fix", and it is the same decision the template path
            # makes by returning the original code unchanged.
            _log = ("; ".join(verdict.violations[:3]))[:300]
            print(f"[fix_generator] diff contract REJECTED the generated fix for "
                  f"{consumer.file_path}: {_log}")
            return None

    diff = _compute_diff(original_code, fixed_code, consumer.file_path)
    
    return GeneratedFix(
        consumer=consumer,
        original_code=original_code,
        fixed_code=fixed_code,
        explanation=explanation,
        diff=diff,
    )


def generate_fixes(
    consumers: list[ConsumerMatch],
    breaking_change: BreakingChange,
    use_llm: bool = True,
    high_confidence_only: bool = True,
) -> list[GeneratedFix]:
    """Generate fixes for all consumers.

    One fix per FILE. Consumers arrive one per matched line, and each fix is a
    whole-file rewrite, so a file with two matching lines produced the same fix
    twice -- two identical diffs, and a doubled count in every summary.
    """
    fixes = []
    seen_files = set()

    for consumer in consumers:
        # Skip low-confidence matches
        if high_confidence_only and consumer.confidence != "high":
            continue
        if consumer.file_path in seen_files:
            continue

        fix = generate_fix(consumer, breaking_change, use_llm=use_llm)
        if fix:
            seen_files.add(consumer.file_path)
            fixes.append(fix)
    
    return fixes


def generate_combined_fixes(
    per_change: list,
    use_llm: bool = True,
    high_confidence_only: bool = True,
    sources: dict = None,
) -> list[GeneratedFix]:
    """ONE fix per consumer file, folding in every breaking change that hits it.

    `per_change` is [(breaking_change, consumer_matches)]. Each change is applied
    to the file's CURRENT content, after the previous changes, through
    generate_fix -- so every step still passes its own diff contract.

    It was one fix per (change x file), each computed from the ORIGINAL file:
    1,228 code-deleting fixes on one saleor commit, 58 overlapping PRs over 7
    files on immich, each of which would conflict with the others on merge.
    """
    order, by_file = [], {}
    for change, matches in per_change:
        for m in matches:
            if high_confidence_only and m.confidence != "high":
                continue
            if m.file_path not in by_file:
                by_file[m.file_path] = []
                order.append(m.file_path)
            if not any(c is change for c, _ in by_file[m.file_path]):
                by_file[m.file_path].append((change, m))

    fixes = []
    for path in order:
        if sources and path in sources:
            original = sources[path]
        else:
            try:
                with open(path, "r") as fh:
                    original = fh.read()
            except IOError:
                continue
        current, notes, consumer = original, [], None
        for change, match in by_file[path]:
            consumer = consumer or match
            fix = generate_fix(match, change, use_llm=use_llm,
                               original_code=current)
            if fix:
                current = fix.fixed_code
                notes.append(f"[{change.change_type} {change.field_name}] "
                             f"{fix.explanation}")
        if current != original:
            fixes.append(GeneratedFix(
                consumer=consumer, original_code=original, fixed_code=current,
                explanation="\n".join(notes),
                diff=_compute_diff(original, current, path)))
    return fixes


#: Per-OPERATION instructions for the LLM, and the fields each one needs filled.
#:
#: WHY THIS TABLE EXISTS
#: There was one hardcoded instruction block, used for every change type:
#:
#:     1. Add the new required field "{field_name}" to the API call.
#:     2. Add it as a parameter/argument that callers must provide.
#:
#: with NO branching on change_type. So a removal asked the model to ADD the field.
#: Measured on a live gemini-flash-latest call: asked to remove `phoneNumber`, it
#: added `phoneNumber: string` as a parameter to two functions and explained itself
#: as "Added required field 'phoneNumber'" -- doing exactly what it was told. The
#: diff contract rejected the patch, which is the only reason it never shipped.
#:
#: That misread as model unreliability for a day. It was the prompt.
#:
#: AN ALLOWLIST, NOT A DEFAULT
#: An operation absent from this table gets NO LLM attempt -- it returns to the
#: deterministic template path. The previous shape was the "unknown enum falls
#: through to the weakest path" defect: every unrecognised operation silently
#: inherited the add-a-required-field instructions. A new operation must be added
#: here deliberately, and until it is, it cannot be given contradictory orders.
#:
#: JUDGMENT OPERATIONS ARE ABSENT ON PURPOSE
#: remove_operation, remove_package and restrict_schema are not here. Deleting call
#: sites for an endpoint that no longer exists is a product decision, and REVIEW is
#: the permanently correct answer for it -- not a prompt.
_LLM_INSTRUCTIONS: dict = {
    "remove_field": (
        (),
        '1. The field "{field}" no longer exists upstream. REMOVE every reference\n'
        '   to it from this file.\n'
        '2. Do NOT remove a function parameter or a destructured binding -- those\n'
        '   change the signature callers depend on. Leave them and remove nothing\n'
        '   else if that is all you find.\n'
        '3. Do NOT edit comments or string literals that mention "{field}".\n'
        '4. Insert nothing. A removal never adds a line.'
    ),
    "add_required": (
        (),
        '1. The field "{field}" is now REQUIRED by the API. Add it to the call in\n'
        '   this file.\n'
        '2. Add it as a parameter or argument that callers must provide.\n'
        '3. Do not invent a value -- thread it through from the caller.'
    ),
    "rename_field": (
        ("new_name",),
        '1. The field "{field}" was RENAMED to "{new_name}".\n'
        '2. Rename every reference, preserving the surrounding shape exactly.\n'
        '3. Do not rename anything whose name merely CONTAINS "{field}".'
    ),
    "change_field_type": (
        ("new_type",),
        '1. The field "{field}" changed type from "{old_type}" to "{new_type}".\n'
        '2. Adapt the uses of that field to the new type. Convert at the boundary\n'
        '   rather than changing unrelated signatures.\n'
        '3. Do not write the contract type name into the source -- use the\n'
        '   language\'s own type.'
    ),
    "remove_enum_value": (
        (),
        '1. The enum value "{field}" was removed.\n'
        '2. Remove the branch or case that handles it, INCLUDING its body -- an\n'
        '   orphaned body is a syntax error.\n'
        '3. Leave every other branch untouched.'
    ),
}


def _llm_instructions(change: BreakingChange) -> str:
    """Instructions for THIS operation, or "" if the LLM must not be asked.

    Returns "" when the operation is not in the allowlist, or when it is but a field
    its instructions interpolate is empty -- `Rename "x" to ""` is worse than no
    attempt, because the model will invent something plausible.
    """
    from .change_types import canonical_op

    entry = _LLM_INSTRUCTIONS.get(canonical_op(change.change_type or ""))
    if entry is None:
        return ""
    required, template = entry
    values = {"field": change.field_name, "new_name": change.new_name,
              "old_type": change.old_type or change.field_type,
              "new_type": change.new_type}
    if any(not values.get(name) for name in required):
        return ""
    return template.format(**values)


def _llm_explanation(change: BreakingChange) -> str:
    """What was actually done, for the PR body.

    Kept beside _LLM_INSTRUCTIONS so the two cannot drift: an operation briefed one
    way and announced another is how a removal PR came to say "Added required field".
    """
    from .change_types import canonical_op

    field = change.field_name
    where = f"{(change.method or '').upper()} {change.path}".strip()
    suffix = f" in the {where} call" if where else ""
    return {
        "remove_field": f"Removed references to the deleted field '{field}'{suffix}",
        "add_required": f"Added the newly required field '{field}'{suffix}",
        "rename_field": (f"Renamed field '{field}' to "
                         f"'{change.new_name}'{suffix}"),
        "change_field_type": (
            f"Adapted uses of '{field}' from "
            f"{change.old_type or change.field_type} to {change.new_type}{suffix}"),
        "remove_enum_value": f"Removed handling of the deleted enum value '{field}'",
    }.get(canonical_op(change.change_type or ""),
          f"Applied a fix for {change.change_type} on '{field}'{suffix}")


#: Largest consumer file we will put in a model prompt, in characters.
#:
#: WHY A CAP EXISTS AT ALL
#: The prompt below interpolates {original_code} -- the WHOLE consumer file -- and
#: nothing bounded it. Measured against this repo's own sources, the median file is
#: ~10.7K chars (~2.7K tokens) but the largest is 211K chars, which is ~69K tokens
#: on a current tokenizer. Two distinct failures follow from that, and the second
#: is the one that matters:
#:
#:   1. COST is unbounded in file size. Only the OUTPUT was capped
#:      (max_tokens=2000); input had no limit, so one large file cost 5x a median
#:      one on the same model.
#:   2. CORRECTNESS. 69K tokens exceeds the context window of the default
#:      self-hosted model (qwen2.5-coder:7b), and a provider that quietly drops
#:      the overflow would hand back a fix generated from PART of the file. A
#:      patch written against code the model never saw is not a worse fix, it is
#:      an actively dangerous one -- and it would arrive looking exactly like a
#:      good one.
#:
#: WHY REFUSE RATHER THAN TRUNCATE
#: Truncating here would manufacture defect 2 deliberately. So an oversized file
#: takes the deterministic template path instead, which is the same answer this
#: function already gives when the anthropic package is missing or no instructions
#: exist for the change type -- and it says why, out loud.
#:
#: 60K chars is ~15K tokens, leaving headroom for the 2K output inside a 32K
#: window. It admits the p90 file (26K chars) and refuses only genuine outliers.
MAX_FIX_SOURCE_CHARS = int(
    os.environ.get("RIPPLE_MAX_FIX_SOURCE_CHARS", "60000") or "60000")


def _generate_with_llm(
    original_code: str,
    consumer: ConsumerMatch,
    breaking_change: BreakingChange,
) -> tuple[str, str]:
    """Use the configured LLM to generate the fix."""
    try:
        import anthropic
    except ImportError:
        print("  ⚠️  anthropic package not installed. Using template fix.")
        return _generate_with_template(original_code, consumer, breaking_change)

    instructions = _llm_instructions(breaking_change)
    if not instructions:
        # No contradictory orders. The deterministic path is the correct answer for
        # an operation we cannot brief the model on.
        print(f"  ⚠️  no LLM instructions for change_type="
              f"{breaking_change.change_type!r}; using the template instead")
        return _generate_with_template(original_code, consumer, breaking_change)

    if len(original_code) > MAX_FIX_SOURCE_CHARS:
        # Refuse, do not truncate. See MAX_FIX_SOURCE_CHARS.
        print(f"  ⚠️  {consumer.file_path} is {len(original_code):,} chars, over "
              f"the {MAX_FIX_SOURCE_CHARS:,}-char prompt cap "
              f"(~{len(original_code) // 4:,} tokens); using the template instead "
              f"rather than sending a partial file")
        return _generate_with_template(original_code, consumer, breaking_change)

    from .llm_config import client_api_key as _llm_client_key, base_url as _llm_base, model as _llm_model
    # base_url passed explicitly rather than relying on the SDK reading the env,
    # so the configured backend is visible at the call site.
    #
    # client_api_key() rather than api_key(): the SDK refuses to construct with an
    # empty credential even against a server that authenticates nothing, so a
    # keyless self-hosted model failed here AFTER is_configured() had opened the
    # gate -- and fell silently through to the template.

    prompt = f"""You are a code assistant. An API has a breaking change. Fix the consumer code.

BREAKING CHANGE:
- Endpoint: {(breaking_change.method or '').upper()} {breaking_change.path}
- Change: {breaking_change.change_type}
- Field: "{breaking_change.field_name}" (type: {breaking_change.field_type})
- Description: {breaking_change.description}

CONSUMER CODE ({consumer.language}):
```
{original_code}
```

INSTRUCTIONS:
{instructions}

FINALLY:
- Keep the fix minimal -- only change what is necessary.
- Return ONLY the complete fixed file content, no explanation.
- Do NOT add comments explaining the fix.

FIXED CODE:"""

    from . import llm_chain

    def _ask(provider) -> str:
        """One request to ONE provider, using THAT provider's endpoint and key.

        The first draft read llm_config.base_url() for every entry, so a three-name
        chain was really one endpoint called three times and the answer was credited
        to whichever name came first -- measured: a local Ollama reply reported as
        `answered by openrouter` with no OpenRouter key set. Each entry now carries
        its own connection details, and llm_config remains the only module that reads
        a credential.
        """
        from .llm_config import credential_for
        client = anthropic.Anthropic(
            api_key=credential_for(provider.key_env) or "not-needed",
            base_url=provider.base_url,
        )
        response = client.messages.create(
            model=_llm_model(),
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text

    def _ask_configured(_provider) -> str:
        """The explicitly configured backend, whatever it is.

        ANTHROPIC_BASE_URL pointing at a custom gateway is the pre-existing
        single-backend contract and must keep working, so it is tried FIRST and named
        by backend_label() rather than borrowing a registry provider's name.
        """
        client = anthropic.Anthropic(api_key=_llm_client_key(), base_url=_llm_base())
        response = client.messages.create(
            model=_llm_model(),
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text

    chain = llm_chain.fix_chain()
    budget = _llm_budget()
    result = None

    from .llm_config import backend_label as _label, base_url as _base_raw
    import os as _os
    if _os.environ.get("ANTHROPIC_BASE_URL"):
        # Try the configured backend first, under its own honest label.
        configured = llm_chain.Configured(name=_label(), base_url=_base_raw())
        result = llm_chain.run(_ask_configured, chain=[configured], budget=budget,
                               payload=llm_chain.PAYLOAD_SOURCE)

    if result is None or not result.ok:
        chained = llm_chain.run(_ask, chain=chain, budget=budget,
                                payload=llm_chain.PAYLOAD_SOURCE)
        result = llm_chain.merge(result, chained)

    if not result.ok:
        # STATED, not silent. The old path printed "LLM error: ... Using template fix"
        # to stdout and returned the template, so afterwards a rate-limited model and
        # a never-configured one were indistinguishable -- and the reason existed
        # nowhere a reader would look.
        #
        # The template fix is still returned, and its provenance is still `template`,
        # which is TRUE. What changes is that the explanation now carries why the LLM
        # did not answer, so the PR body says it and _record_pr_provenance still
        # derives the honest source from the prefix.
        fixed, expl = _generate_with_template(original_code, consumer, breaking_change)
        return fixed, f"{expl}\nLLM NOT USED: {result.stated_outcome()}"

    fixed_code = result.text.strip()

    # Strip markdown code fences if present
    if fixed_code.startswith("```"):
        lines = fixed_code.split("\n")
        # Remove first and last lines (fences)
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        fixed_code = "\n".join(lines)

    # The trailing newline is restored centrally in generate_fix(), where
    # both generator paths converge -- see the note there.

    # The explanation is what a human reads in the PR body, so it must describe
    # what was actually DONE. It was hardcoded to "Added required field" for
    # every operation -- the same defect as the prompt, and visible to customers:
    # a removal PR announced itself as an addition.
    #
    # And it now names WHICH backend answered. With a chain, "[llm]" alone would
    # hide that a fallback provider produced the patch -- the same misreporting
    # llm_config.backend_label() exists to prevent for the single-backend case.
    explanation = _llm_explanation(breaking_change)
    return fixed_code, f"{explanation}\n{result.stated_outcome()}"


#: Per-push call ceiling, shared by every consumer file in one push.
#:
#: Module-level because the scope that matters is the PUSH and _generate_with_llm is
#: called once per consumer file, so a budget created inside it would reset on every
#: call and cap nothing. reset_llm_budget() is called by the webhook at push entry;
#: a gate asserts that, because a budget nobody resets slowly closes every provider
#: and then reports them closed forever.
_LLM_BUDGET = None


def _llm_budget():
    global _LLM_BUDGET
    if _LLM_BUDGET is None:
        from .llm_chain import Budget
        _LLM_BUDGET = Budget()
    return _LLM_BUDGET


def reset_llm_budget() -> None:
    """Start a fresh per-provider budget. Called once per push."""
    global _LLM_BUDGET
    _LLM_BUDGET = None


def _generate_with_template(
    original_code: str,
    consumer: ConsumerMatch,
    breaking_change: BreakingChange,
) -> tuple[str, str]:
    """Template fix, with one check every template's EDIT must pass: the net
    bracket balance of the file is unchanged.

    Each template has its own contract, but 9 of 15 wrong fixes on real saleor
    commits slipped past them the same way -- deleting the line that OPENS a
    construct (`orderLine: {`, `if (x === E.VOID) {`, `case E.X:` with half its
    arm) and leaving the rest. That always shifts a bracket count, so it is
    caught here once, for every template and every change type. A refused edit
    becomes markers on the same references; nothing is deleted on a guess.
    """
    fixed, explanation = _template_fix(original_code, consumer, breaking_change)
    if fixed == original_code or _is_marker_only(original_code, fixed):
        return fixed, explanation
    from .source_regions import brackets_preserved
    lang = (consumer.language or "unknown").lower()
    if brackets_preserved(original_code, fixed, lang):
        return fixed, explanation
    from .fix_templates import annotate_references
    field = breaking_change.field_name or ""
    print(f"[fix_generator] template edit for {consumer.file_path} changed the "
          f"file's bracket balance; refused, marking instead")
    marked, why = annotate_references(
        original_code, lang, field,
        f"'{field}' changed in the contract ({breaking_change.change_type}); "
        f"an automated edit here would have left unbalanced brackets -- "
        f"update this reference by hand.")
    return marked, ("The automated edit was refused because it would have left "
                    "unbalanced brackets. " + why)


def _template_fix(
    original_code: str,
    consumer: ConsumerMatch,
    breaking_change: BreakingChange,
) -> tuple[str, str]:
    """
    Template-based fix (no LLM needed).
    Works for the common case: "add a field to a JSON payload."
    """
    field_name = breaking_change.field_name
    # Branch on the CANONICAL operation, not the engine's dialect. This matched
    # five literal strings, so response_field_removed, column_removed,
    # member_removed, rpc_removed, endpoint_removed, required_argument_added
    # and every other dialect the taxonomy maps returned "Unsupported change
    # type": detected, then silence -- on the CLI and in the webhook fallback.
    from .change_types import canonical_op
    op = canonical_op(breaking_change.change_type or "")

    if op == "remove_field":
        return _remove_field_fix(original_code, consumer, breaking_change)

    if op == "remove_enum_value":
        return _remove_enum_value_fix(original_code, consumer, breaking_change)

    if op == "rename_field":
        from .fix_templates import apply_fix_template, annotate_references
        # Was getattr(breaking_change, 'new_name', '') on a dataclass with no
        # such field, so new_name was ALWAYS '' and this branch never fired --
        # every rename fell through to "Unsupported change type", leaving the
        # code unchanged, which opens no PR. Detection became silence.
        new_name = breaking_change.new_name
        from .smart_consumer_finder import needs_owner_scope
        if new_name and needs_owner_scope(breaking_change):
            # Same rule as removal: a one-word field (`name`) cannot be tied to
            # its owning type per reference, and the rename template rewrote a
            # function PARAMETER `name` -- and `${name}` inside a query string
            # -- to the new name. Mark instead.
            return annotate_references(
                original_code, consumer.language or "unknown", field_name,
                f"field '{field_name}' was renamed to '{new_name}' -- update "
                f"the references that read this field.")
        if new_name:
            fixed, expl = apply_fix_template(
                code=original_code,
                language=consumer.language or "unknown",
                change_type="field_renamed",
                field_name=field_name,
                new_name=new_name,
            )
            if fixed != original_code:
                return fixed, expl
            # The template recognised the operation but matched nothing (it is
            # a no-op for javascript and ruby today). Flag rather than go quiet.
            return annotate_references(
                original_code, consumer.language or "unknown", field_name,
                f"field '{field_name}' was renamed to '{new_name}' -- update "
                f"these references.")
        # An engine that detects the rename but cannot name the target. Do NOT
        # borrow field_removed here: that deletes references to a field which
        # still exists under a new name, and would report "Removed all
        # references".
        return annotate_references(
            original_code, consumer.language or "unknown", field_name,
            f"field '{field_name}' was renamed, but the diff engine did not "
            f"report the new name -- rename these references by hand.")

    if op == "change_field_type":
        from .fix_templates import apply_fix_template, annotate_references
        # PRECEDENCE BUG: this read
        #     getattr(bc,'old_type','') or bc.field_type.split(...)[0] if COND else ''
        # which Python groups as
        #     (getattr(...) or split(...)) if COND else ''
        # because a conditional expression binds looser than `or`. So whenever
        # field_type lacked ' -> ', old_type became '' EVEN IF the attribute
        # held a value -- and the attribute never existed anyway. The branch
        # therefore fired only when field_type happened to be formatted as
        # "old -> new", i.e. it depended on a string-formatting accident.
        old_type = breaking_change.old_type
        new_type = breaking_change.new_type
        if not (old_type and new_type):
            # Fall back to parsing the display string, accepting either arrow.
            raw = str(breaking_change.field_type)
            for arrow in (" \u2192 ", " -> "):
                if arrow in raw:
                    left, _, right = raw.partition(arrow)
                    old_type = old_type or left.strip()
                    new_type = new_type or right.strip()
                    break
        if old_type and new_type:
            fixed, expl = apply_fix_template(
                code=original_code,
                language=consumer.language or "unknown",
                change_type="type_changed",
                field_name=field_name,
                old_type=old_type,
                new_type=new_type,
            )
            if fixed != original_code:
                return fixed, expl
            # change_field_type is a measured no-op in 6 of 9 languages. That is
            # a template gap, but it must not surface as silence.
            return annotate_references(
                original_code, consumer.language or "unknown", field_name,
                f"type of '{field_name}' changed from {old_type} to {new_type} "
                f"-- verify these references still compile.")
        # A type change we cannot describe is still a break the consumer must
        # look at.
        return annotate_references(
            original_code, consumer.language or "unknown", field_name,
            f"type of '{field_name}' changed, but the diff engine did not "
            f"report the old and new types -- verify these references.")
    
    if op != "add_required":
        if not op:
            return original_code, "Unsupported change type for template fix"
        # Every other operation (remove_type, remove_enum_value,
        # remove_operation, restrict_schema, rename_type, remove_package,
        # wire_incompatible) has its handler in fix_templates, which applies
        # the safe part and marks the rest, or reports a wire-only break.
        from .fix_templates import apply_fix_template
        from .smart_consumer_finder import consumer_query
        site_target, site_vector = consumer_query(breaking_change)
        return apply_fix_template(
            code=original_code,
            language=consumer.language or "unknown",
            change_type=breaking_change.change_type,
            field_name=field_name,
            new_name=breaking_change.new_name,
            site_target=site_target,
            site_vector=site_vector,
        )

    # DELEGATE, like every other change type above. This branch used to carry
    # its own inline implementation for typescript/python/java, which was a
    # second implementation of an operation fix_templates already handles for
    # all nine languages -- and the two disagreed on the CATEGORY.
    #
    # added_required_field is JUDGMENT (app/change_types.py). The contract is:
    # apply the safe part, mark the rest, never invent a value. The inline
    # version treated it as mechanical -- it appended a REQUIRED positional
    # parameter and wrote the field into the payload, which breaks every
    # existing caller (`create_user(name, email)` -> TypeError) and silently
    # decides what value to send. fix_templates annotates each construction
    # site instead and says explicitly that it did not choose a value.
    #
    # Deleting the duplicate also puts this path under tools/coverage_matrix.py,
    # which exercises apply_fix_template and never reached the inline code.
    from .fix_templates import apply_fix_template
    return apply_fix_template(
        code=original_code,
        language=consumer.language or "unknown",
        change_type="add_required",
        field_name=field_name,
        site_hints=_call_site_hints(breaking_change, original_code,
                                    consumer.language or "unknown"),
    )


def _remove_field_fix(original_code: str, consumer: ConsumerMatch,
                      breaking_change: BreakingChange) -> tuple[str, str]:
    """remove_field: verified edits only; everything else becomes a marker.

    1. GraphQL documents embedded in TS/JS (gql`...`): selections the document
       PROVES are made on the owning type are removed structurally and checked
       (fix_templates.remove_graphql_selections). Unproven ones -- inside a
       field's sub-selection, whose type only the schema knows -- are marked.
    2. The source itself: the language template, kept only where a scanner
       exists and the diff contract accepts it.
    3. Any reference neither step removed gets a marker. Nothing is deleted on
       a guess.

    Measured on 12 real commits before this existed: the Go template turned
    `var s traceql.Static` + `for _, r := range rs {` into `var s traceql._, r
    := range rs {`, and the line-deleting fallback removed whole DOM handlers
    that named an unrelated `target`.
    """
    from .fix_templates import (apply_fix_template, embedded_graphql_docs,
                                remove_graphql_selections)
    from .smart_consumer_finder import field_owner
    field_name = breaking_change.field_name
    lang = (consumer.language or "unknown").lower()
    code, notes = original_code, []

    owner = field_owner(breaking_change)
    if (lang in ("typescript", "javascript") and owner
            and breaking_change.location == "graphql"):
        docs = embedded_graphql_docs(code)
        if docs:
            code, removed, unproven = remove_graphql_selections(
                code, field_name, owner, docs)
            if removed:
                notes.append(f"Removed {removed} selection(s) of "
                             f"'{owner}.{field_name}' from GraphQL documents.")
            if unproven:
                notes.append(f"{unproven} selection(s) of '{field_name}' sit "
                             f"inside a sub-selection whose type the document "
                             f"does not state; not removed.")

    fixed, explanation = apply_fix_template(
        code=code, language=lang, change_type="field_removed",
        field_name=field_name)
    from .smart_consumer_finder import needs_owner_scope
    if fixed != code and needs_owner_scope(breaking_change):
        # A one-word field (`status`, `target`) cannot be tied to its owning
        # type at the source level without type information: `u.status` in a
        # file that also uses TransactionItem may be any object's status. The
        # template would edit every one (it removed `${u.status}` from an
        # unrelated message), so a one-word name gets markers in source code.
        why_scope = ("is used by other types in the spec too" if
                     breaking_change.name_shared else "is too generic")
        notes.append(f"'{field_name}' {why_scope}, so source references cannot "
                     f"be tied to '{owner or 'its type'}' by name; not edited.")
    elif fixed != code and _removal_is_verified(code, fixed, field_name, lang):
        code = fixed
        notes.append(explanation)
    elif fixed != code:
        notes.append("The source template's edit could not be verified for "
                     "this language and was not applied.")

    marked, why = _mark_field_references(
        code, field_name, lang, "an automated edit could not be verified here")
    if marked != code:
        code = marked
        notes.append(why)
    if code == original_code:
        return original_code, why if why.startswith("No change") else (
            notes[-1] if notes else why)
    return code, " ".join(notes)


def _enum_ref(owner: str, value: str) -> "re.Pattern":
    """`Owner.VALUE`, `Owner_VALUE` (Go/protobuf) or `Owner::VALUE`, the value
    ending at a real boundary. `_` is NOT a boundary here: the previous matcher
    accepted it, so the removed `VOID` also matched `TRANSACTION_VOID_REQUESTED`."""
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(owner)}(?:\.|::|_)"
                      rf"{re.escape(value)}(?![A-Za-z0-9_])")


#: A statement that ends a switch arm, optionally after its own label
#: (`case A: return 1;`).
_ARM_END = re.compile(r"^\s*(?:(?:case\b[^:]*|default\s*):\s*)?"
                      r"(?:return|break|throw|continue)\b")


def _stmt_start(mlines: list, k: int) -> str:
    """The masked line on which the statement ENDING on line k begins: walk
    back until the brackets closed since then are all opened again."""
    balance = 0
    for j in range(k, -1, -1):
        ml = mlines[j]
        balance += (sum(ml.count(c) for c in "})]") - sum(ml.count(c) for c in "{(["))
        if balance <= 0:
            return ml
    return mlines[0] if mlines else ""


def _remove_enum_value_fix(original_code: str, consumer: ConsumerMatch,
                           breaking_change: BreakingChange) -> tuple[str, str]:
    """A removed enum value: edit only what is provably that enum's, mark the rest.

    On a real saleor commit removing `TransactionActionEnum.VOID`, the shared
    template deleted every line containing `<anything>.VOID` -- also
    `OrderAction.VOID`, `TransactionKind.VOID` and
    `OrderErrorCode.VOID_INACTIVE_PAYMENT`, which still exist -- and deleted
    `if (action === TransactionActionEnum.VOID) {` while leaving its body.

    So, with the owning enum known from the spec:
      * only `Owner.VALUE` (and its `_` / `::` spellings) counts as a reference;
      * TS/JS edits are two shapes: the member line inside `enum Owner { }`
        (generated client types), and a whole `case Owner.VALUE:` arm -- label
        plus body, its end found by bracket depth, refused if the arm above
        falls through into it;
      * every other reference -- a comparison, a list entry, an argument --
        is marked. Deleting the line it is on deletes whatever else it does.
    Without an owner nothing is edited.
    """
    from .fix_templates import MARKER, _comment_token
    from .smart_consumer_finder import field_owner
    from .source_regions import mask
    value = breaking_change.field_name or ""
    owner = field_owner(breaking_change)
    lang = (consumer.language or "unknown").lower()
    if not value:
        return original_code, "No change: the enum value has no name."
    ref = _enum_ref(owner, value) if owner else re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(value)}(?![A-Za-z0-9_])")
    lines = original_code.split("\n")
    mlines = mask(original_code, lang).split("\n")
    drop: set = set()
    edits = []

    if owner and lang in ("typescript", "javascript"):
        # 1. The member inside `enum Owner { ... }`.
        enum_head = re.compile(rf"\benum\s+{re.escape(owner)}\s*\{{\s*$")
        member = re.compile(rf"^\s*{re.escape(value)}\s*(?:=\s*[^,]+)?,?\s*$")
        i = 0
        while i < len(lines):
            if enum_head.search(mlines[i]):
                j = i + 1
                while j < len(lines) and not mlines[j].strip().startswith("}"):
                    if member.match(lines[j]) and "{" not in mlines[j]:
                        drop.add(j)
                        edits.append(f"member of enum {owner}")
                    j += 1
                i = j
            i += 1
        # 2. Whole `case Owner.VALUE:` arms.
        label = re.compile(rf"^\s*case\s+{ref.pattern}\s*:\s*$")
        code_idx = [k for k, ml in enumerate(mlines) if ml.strip()]
        for pos, i in enumerate(code_idx):
            if not label.match(lines[i]):
                continue
            prev = mlines[code_idx[pos - 1]] if pos else ""
            nxt = mlines[code_idx[pos + 1]] if pos + 1 < len(code_idx) else ""
            prev_is_label = bool(re.match(r"^\s*(?:case\b.*|default\s*):\s*$", prev))
            if prev_is_label or re.match(r"^\s*(?:case\b|default\b)", nxt):
                drop.add(i)                 # one label of a group: the body stays
                edits.append(f"case label {owner}.{value}")
                continue
            # The arm above must not fall through into this one. Judged on the
            # line its last STATEMENT starts on: `return intl.formatMessage({`
            # ends on `});`, which on its own says nothing.
            prev_stmt = _stmt_start(mlines, code_idx[pos - 1]) if pos else ""
            if not (re.search(r"\bswitch\b.*\{\s*$", prev_stmt)
                    or _ARM_END.search(prev_stmt)):
                continue
            depth, j, end = 0, i + 1, None
            while j < len(lines):
                ml = mlines[j]
                if depth == 0 and re.match(r"^\s*(?:case\b|default\b)", ml):
                    end = j
                    break
                d = depth + sum(ml.count(c) for c in "{([") - sum(ml.count(c) for c in "})]")
                if d < 0:
                    end = j                 # the switch's own closing brace
                    break
                depth, j = d, j + 1
            if end is None or depth != 0:
                continue
            last_k = next((k for k in range(end - 1, i, -1) if mlines[k].strip()), None)
            if last_k is None or not _ARM_END.search(_stmt_start(mlines, last_k)):
                continue                    # the arm itself falls through
            drop.update(range(i, end))
            edits.append(f"case arm {owner}.{value}")

    token = _comment_token(lang)
    note = (f"enum value '{owner + '.' if owner else ''}{value}' was removed "
            f"from the contract -- replace or remove this reference.")
    out, marked = [], 0
    for k, line in enumerate(lines):
        if k in drop:
            continue
        if ref.search(mlines[k]) and not (out and MARKER in out[-1]):
            out.append(f"{line[:len(line) - len(line.lstrip())]}{token} {MARKER}: {note}")
            marked += 1
        out.append(line)
    result = "\n".join(out)
    if result == original_code:
        return original_code, (f"No change: no code reference to "
                               f"'{owner + '.' if owner else ''}{value}'.")
    parts = []
    if edits:
        parts.append(f"Removed {len(edits)} reference(s) to the deleted enum value "
                     f"'{owner}.{value}': " + ", ".join(dict.fromkeys(edits)) + ".")
    if marked:
        parts.append(f"PARTIAL: marked {marked} other reference(s) with {MARKER}; "
                     f"they need a replacement value a human must choose.")
    return result, " ".join(parts)


def _is_marker_only(original: str, fixed: str) -> bool:
    """True when `fixed` is `original` plus RIPPLE-ACTION-REQUIRED comment
    lines and nothing else."""
    from .fix_templates import MARKER
    kept = [ln for ln in fixed.split("\n")
            if not (MARKER in ln and ln.lstrip()[:2] in ("//", "# ", "--"))]
    return kept == original.split("\n") and fixed != original


def _diff_check_any_spelling(original: str, fixed: str, field_name: str, language: str):
    """The diff contract's verdict, judged in the consumer's own SPELLING.

    The contract matches the field literally, and a spec names it in its own
    convention: proto `phone_number`, read in TypeScript as `phoneNumber`. Fed
    the spec spelling, every correct cross-convention removal was rejected as
    "deleted a line that does not reference 'phone_number'". Each case variant
    that occurs in the file is tried; the removal is accepted if one of them
    accounts for every change. A file mixing two spellings is refused -- the
    conservative direction.
    """
    from .diff_contract import check
    from .fix_templates import name_variants
    first = None
    for v in dict.fromkeys(name_variants(field_name).values()):
        if not v or not re.search(rf"\b{re.escape(v)}\b", original):
            continue
        verdict = check(original, fixed, v, language=language)
        if verdict.ok:
            return verdict
        first = first or verdict
    return first or check(original, fixed, field_name, language=language)


def _removal_is_verified(original: str, fixed: str, field_name: str,
                         language: str) -> bool:
    """May this template removal ship as an EDIT?

    Only where a real scanner exists (source_regions.SCANNED) and the diff
    contract accepts it. Checked here, inside _generate_with_template, because
    the webhook calls that directly and never reaches generate_fix's check.
    """
    from .source_regions import SCANNED
    if language not in SCANNED:
        return False
    return _diff_check_any_spelling(original, fixed, field_name, language).ok


def _mark_field_references(code: str, field_name: str, language: str,
                           why: str) -> tuple[str, str]:
    """Marker-only answer for a removed field: annotate each CODE reference
    (not strings or comments), delete nothing. Unchanged when there is none --
    a file with no reference is not a consumer, and a file-top marker would
    open a PR that says nothing."""
    from .fix_templates import (MARKER, _STR_LITERAL, _comment_token,
                                _matches_symbol, _symbol_names, name_variants)
    from .fix_templates import embedded_graphql_docs
    from .operation_sites import graphql_typed_selections, multiline_string_opens
    names = _symbol_names(name_variants(field_name)) | {field_name}
    token = _comment_token(language)
    note = (f"field '{field_name}' was removed from the contract -- remove or "
            f"replace this reference.")
    # A line INSIDE a multi-line string gets no marker of its own: a `//` line
    # inserted into a gql`` document becomes part of the query and breaks it.
    # A GraphQL document that still selects the field is marked once, above
    # the line its string opens on; any other multi-line string is text.
    opens = multiline_string_opens(code)
    targets = set()
    for start, end in embedded_graphql_docs(code):
        if any(n == field_name for _o, _k, n, _d, _t in
               graphql_typed_selections(code[start:end])):
            line = code.count("\n", 0, start)
            targets.add(opens.get(line + 1, line) if line + 1 in opens else line)
    src = code.split("\n")
    for i, line in enumerate(src):
        s = line.strip()
        if i in opens or not s or s.startswith(token) or s.startswith("*"):
            continue
        if _matches_symbol(_STR_LITERAL.sub('""', line), names):
            targets.add(i)
    out, sites = [], 0
    for i, line in enumerate(src):
        if i in targets and not (out and MARKER in out[-1]):
            indent = line[:len(line) - len(line.lstrip())]
            out.append(f"{indent}{token} {MARKER}: {note}")
            sites += 1
        out.append(line)
    if not sites:
        return code, f"No change: no code reference to '{field_name}' ({why})."
    return "\n".join(out), (
        f"PARTIAL: marked {sites} reference(s) to removed field '{field_name}' "
        f"with {MARKER}. Ripple did NOT delete them: {why}, so an automated "
        f"edit could break the file.")


def _call_site_hints(breaking_change: BreakingChange, code: str = "",
                     language: str = "unknown") -> tuple[str, ...]:
    """Anchors for locating the construction site of an affected request.

    add_required cannot anchor on the field: a newly required field is by
    definition absent from consumer code. The endpoint path is the one thing the
    contract and the consumer both name -- `post("/users", ...)` and, for
    proto/GraphQL engines where `path` holds a message or type name, `User{...}`.

    Ordered widest-anchor-first. The trailing segment is included because
    consumers commonly build the URL (`f"{base}/users/{id}"`) rather than
    writing the path literally, and it is only used when nothing more specific
    matched -- a spurious anchor costs a stray comment, never a code edit.
    """
    hints: list[str] = []
    from .smart_consumer_finder import add_required_anchor
    anchor, kind = add_required_anchor(breaking_change)
    if kind == "symbol":
        # Non-HTTP engines put the SPEC FILE in `path` and the containing
        # type/table/struct in `method`; anchoring on `path` tagged lines
        # mentioning `schema.graphql`, i.e. nothing. Same anchor the consumer
        # finder used to decide this file is a consumer.
        hints.append(anchor)
        hints.extend(n for n in _field_variants(anchor) if n != anchor)
        return tuple(hints)
    if kind == "endpoint" and code:
        # Anchor on the exact lines the consumer finder scored as calling THIS
        # operation. The `/users` substring also tagged `get('/users')` and
        # `/users/{id}` with "supply a value", and a GET sends no body.
        from .smart_consumer_finder import find_endpoint_consumers
        calls = [m.line_content for m in
                 find_endpoint_consumers(code, "", anchor, language)
                 if m.match_type == "endpoint_call"]
        if calls:
            return tuple(dict.fromkeys(calls))
    path = (breaking_change.path or "").strip()
    if path:
        hints.append(path)
        if "/" in path:
            segment = path.rstrip("/").rsplit("/", 1)[-1]
            # 4+ chars: shorter segments ("id", "me", "v1") match everywhere.
            if segment and segment != path and len(segment) >= 4:
                hints.append(segment)
        else:
            # An identifier-shaped path is a type/message name: cover the
            # casings a consumer might use.
            hints.extend(n for n in _field_variants(path) if n != path)
    return tuple(hints)


def _remove_field_references(original_code: str, field_name: str, language: str) -> tuple[str, str]:
    """Line-based removal, VERIFIED. The unchecked engine is below.

    WHY THIS WRAPPER EXISTS
    -----------------------
    generate_fix() applies the diff contract at line 132. But app/webhook.py:72
    imports the private `_generate_with_template` and calls it directly at line
    2188, reaching around that guard -- so on the first live end-to-end run the
    weakest generator in this codebase ran completely unverified.

    Worse, it fires EXACTLY WHEN the hardened template refuses (line 396). A
    deliberate refusal therefore became a silent downgrade to line deletion. What
    it opened as a PR, on a real repository:

        -    email: string,
        -    phoneNumber: string,          removed the PARAMETER
        +    email: string
             const request: CreateUserRequest = { name, email, phoneNumber };
                                                                ^^^ still there

        -    body: JSON.stringify(body),   a DIFFERENT function
        +    body: JSON.stringify(body)

    The stray comma is the whole-file `re.sub` below; the contract also caught a
    destroyed doc comment. Only "no tree, so no validation -> REVIEW" stopped
    that reaching AUTO, which is luck rather than design.

    So the check lives HERE, at the single convergence point, rather than in one
    caller. A guard a caller can decline to invoke is not a guard -- the same
    reason the trailing-newline fix had to move out of the monkeypatched branch.

    UNSCANNED LANGUAGES PASS THROUGH, DELIBERATELY. The gate is
    source_regions.SCANNED because scanning Go with the TypeScript comment rules
    produces confident nonsense in both directions. Go and Java therefore still
    get an unverified fallback fix -- but neither has a validator, so
    routing.pr_level() can never grant them AUTO. They are REVIEW by
    construction, and the note says the patch was not contract-checked.
    """
    candidate, explanation = _remove_field_references_unchecked(
        original_code, field_name, language)
    if candidate == original_code:
        return original_code, explanation

    from .source_regions import SCANNED as _SCANNED
    lang = (language or "").lower()
    if lang not in _SCANNED:
        return candidate, f"{explanation} (not contract-checked: no scanner for {lang or 'unknown'})"

    from .diff_contract import check as _diff_check
    verdict = _diff_check(original_code, candidate, field_name, language=lang)
    if verdict.ok:
        return candidate, explanation

    # The refusal SURVIVES. Returning the original is what every other rejected
    # path does, and the caller already reads "unchanged" as "no fix".
    first = (verdict.violations[0] if verdict.violations else "unspecified")[:160]
    return original_code, (
        f"refused: the line-based fallback violated the diff contract ({first}). "
        f"No reference matched a shape that can be removed safely, so the code is "
        f"unchanged."
    )


def _remove_field_references_unchecked(original_code: str, field_name: str, language: str) -> tuple[str, str]:
    """
    Remove all references to a deleted field from consumer code.
    Works across all languages by:
    1. Removing lines that contain the field name in common patterns
    2. Cleaning up dangling commas and empty blocks

    NOT FOR DIRECT USE. This is the raw engine: it deletes any line mentioning the
    field, with no shape analysis, and then rewrites trailing commas across the
    WHOLE FILE. Call _remove_field_references() instead, which verifies the result
    against the diff contract. This name is public only so a test can pin what the
    verified wrapper is protecting against.
    """
    lines = original_code.split("\n")
    removed_lines = []
    result_lines = []
    
    # Generate variants of the field name
    variants = _field_variants(field_name)
    
    for i, line in enumerate(lines):
        line_lower = line.lower().replace("-", "_").replace(" ", "")
        should_remove = False
        
        for variant in variants:
            variant_lower = variant.lower().replace("-", "_").replace(" ", "")
            if variant_lower in line_lower:
                # Check it's a meaningful reference (not a comment about something else)
                stripped = line.strip()
                # Remove lines that are: assignments, struct fields, params, dict keys, interface props
                if any([
                    f"{field_name}" in line and ("=" in line or ":" in line or "," in line),
                    f"'{field_name}'" in line,
                    f'"{field_name}"' in line,
                    f".{variant}" in line and variant != field_name[:3],  # method calls like .phoneNumber
                    f"{variant}:" in line,  # Go struct field
                    f"{variant} =" in line,  # assignment
                    f"{variant}," in line,  # param in list
                    f"self.{variant}" in line,  # Python instance attr
                    f"this.{variant}" in line,  # JS/TS instance
                ]):
                    should_remove = True
                    break
        
        if should_remove:
            removed_lines.append((i, line))
        else:
            result_lines.append(line)
    
    if not removed_lines:
        return original_code, "No references to removed field found"
    
    # Clean up dangling commas
    fixed_code = "\n".join(result_lines)
    # Remove trailing commas before closing braces/parens
    fixed_code = re.sub(r',\s*(\n\s*[}\])])', r'\1', fixed_code)
    # Remove double blank lines
    fixed_code = re.sub(r'\n{3,}', '\n\n', fixed_code)
    
    explanation = f"Removed {len(removed_lines)} reference(s) to deleted field '{field_name}'"
    return fixed_code, explanation


def _field_variants(field_name: str) -> list[str]:
    """Generate naming variants of a field: snake_case, camelCase, PascalCase, kebab-case."""
    variants = [field_name]
    
    # snake_case → camelCase
    parts = field_name.split("_")
    if len(parts) > 1:
        camel = parts[0] + "".join(p.capitalize() for p in parts[1:])
        pascal = "".join(p.capitalize() for p in parts)
        variants.extend([camel, pascal])
    
    # camelCase → snake_case
    snake = re.sub(r'([A-Z])', r'_\1', field_name).lower().lstrip('_')
    if snake != field_name:
        variants.append(snake)
    
    # kebab-case
    kebab = field_name.replace("_", "-")
    if kebab != field_name:
        variants.append(kebab)
    
    return list(set(variants))


def _compute_diff(original: str, fixed: str, filepath: str) -> str:
    """A unified diff that `git apply` / `patch` accept.

    This used lineterm="" with keepends=True, so the `---`, `+++` and `@@`
    header lines had no newline and ran together (`--- a/x+++ b/x@@ -1 +1 @@`)
    while content lines kept theirs: the CLI printed diffs no tool could apply.
    A last line with no trailing newline also ran into the next line; it now
    gets the standard `\\ No newline at end of file` marker.
    """
    import difflib

    def _lines(text: str) -> list:
        out = text.splitlines(keepends=True)
        if out and not out[-1].endswith(("\n", "\r")):
            out[-1] += "\n\\ No newline at end of file\n"
        return out

    return "".join(difflib.unified_diff(
        _lines(original), _lines(fixed),
        fromfile=f"a/{filepath}", tofile=f"b/{filepath}"))


def format_fixes(fixes: list[GeneratedFix]) -> str:
    """Format generated fixes for display."""
    if not fixes:
        return "  No fixes generated."
    
    lines = [
        f"  Generated {len(fixes)} fix(es):",
        "",
    ]
    
    for i, fix in enumerate(fixes, 1):
        lines.append(f"  ━━━ Fix [{i}]: {fix.consumer.file_path} ━━━")
        lines.append(f"  Language: {fix.consumer.language}")
        lines.append(f"  Explanation: {fix.explanation}")
        lines.append("")
        lines.append(fix.diff)
        lines.append("")
    
    return "\n".join(lines)
