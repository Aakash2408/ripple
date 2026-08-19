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
) -> Optional[GeneratedFix]:
    """
    Generate a fix for a consumer affected by a breaking change.
    
    Strategy:
    1. Read the full consumer file
    2. Send to Claude: "This API changed. Fix this consumer code."
    3. Return the fixed code + diff
    """
    # Read the consumer file
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
    from .llm_config import api_key as _llm_key
    if use_llm and _llm_key():
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
    if (field and lang in _SCANNED
            and _canonical_op(breaking_change.change_type or "") == "remove_field"):
        from .diff_contract import check as _diff_check

        verdict = _diff_check(original_code, fixed_code, field, language=lang)
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
    """Generate fixes for all consumers."""
    fixes = []
    
    for consumer in consumers:
        # Skip low-confidence matches
        if high_confidence_only and consumer.confidence != "high":
            continue
        
        fix = generate_fix(consumer, breaking_change, use_llm=use_llm)
        if fix:
            fixes.append(fix)
    
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

    from .llm_config import api_key as _llm_key, base_url as _llm_base, model as _llm_model
    # base_url passed explicitly rather than relying on the SDK reading the env,
    # so the configured backend is visible at the call site.
    client = anthropic.Anthropic(api_key=_llm_key(), base_url=_llm_base())

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

    try:
        response = client.messages.create(
            model=_llm_model(),
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        
        fixed_code = response.content[0].text.strip()

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
        explanation = _llm_explanation(breaking_change)
        return fixed_code, explanation
        
    except Exception as e:
        print(f"  ⚠️  LLM error: {e}. Using template fix.")
        return _generate_with_template(original_code, consumer, breaking_change)


def _generate_with_template(
    original_code: str,
    consumer: ConsumerMatch,
    breaking_change: BreakingChange,
) -> tuple[str, str]:
    """
    Template-based fix (no LLM needed).
    Works for the common case: "add a field to a JSON payload."
    """
    field_name = breaking_change.field_name
    
    if breaking_change.change_type in ("removed_field", "field_removed"):
        # Use the comprehensive template engine
        from .fix_templates import apply_fix_template
        fixed, explanation = apply_fix_template(
            code=original_code,
            language=consumer.language or "unknown",
            change_type="field_removed",
            field_name=field_name,
        )
        if fixed != original_code:
            return fixed, explanation
        # Fallback to basic line removal
        return _remove_field_references(original_code, field_name, consumer.language)
    
    if breaking_change.change_type in ("field_renamed", "renamed_field"):
        from .fix_templates import apply_fix_template, annotate_references
        # Was getattr(breaking_change, 'new_name', '') on a dataclass with no
        # such field, so new_name was ALWAYS '' and this branch never fired --
        # every rename fell through to "Unsupported change type", leaving the
        # code unchanged, which opens no PR. Detection became silence.
        new_name = breaking_change.new_name
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

    if breaking_change.change_type in ("field_type_changed", "type_changed"):
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
    
    if breaking_change.change_type != "added_required_field":
        return original_code, "Unsupported change type for template fix"

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
        site_hints=_call_site_hints(breaking_change),
    )


def _call_site_hints(breaking_change: BreakingChange) -> tuple[str, ...]:
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
    """Compute a unified diff between original and fixed code."""
    import difflib
    
    original_lines = original.splitlines(keepends=True)
    fixed_lines = fixed.splitlines(keepends=True)
    
    diff = difflib.unified_diff(
        original_lines,
        fixed_lines,
        fromfile=f"a/{filepath}",
        tofile=f"b/{filepath}",
        lineterm="",
    )
    
    return "".join(diff)


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
