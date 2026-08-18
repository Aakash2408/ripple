#!/usr/bin/env python3
"""Six fixes that were once called VALID. None may ever be called VALID again.

WHY THIS EXISTS
`app/validated_fix.py` (deleted in a62beac) validated TypeScript by counting
brackets and ended its dispatcher with:

    else:
        # Can't validate -- assume valid
        return True, ""

So it accepted proto content, Java content, and `!!! not rust`, because none of
them have unbalanced brackets. It also accepted no-ops, because it never asked
whether anything had changed. Six specific inputs got a VALID verdict they had
not earned.

Deleting that module removed the bug. It did not create a memory of the bug. This
file is the memory: each case is replayed against a frozen reproduction of the old
logic to prove it really did pass, then run through the current safety stack to
prove it now fails. A corpus that only did the second half could be padded with
cases that were never a problem, and would slowly become decoration.

THE INVARIANT, STATED PRECISELY
Not "the compiler rejects all six" -- that is false, and Stage 3 measured it: three
of five corrupting mutations passed `tsc --noEmit`. A half-fix that keeps a function
parameter compiles perfectly. The invariant is weaker in wording and stronger in
effect:

    no case in this corpus may ever reach a state where a PR is opened
    automatically -- and each case must name the layer that stops it.

Naming the layer is what keeps the corpus honest. If a case's blocking layer ever
CHANGES, that is a finding worth reading, not a detail: it means a layer we relied
on stopped catching something, and another happened to cover for it.

WHY THIS RUNS WITHOUT DOCKER
Every case is blocked by the diff contract or the no-op check, both of which are
pure functions over text. The compiler is consulted when available and its verdict
is REPORTED, but it is never what the gate depends on -- a gate that cannot run in
CI is the same defect as a matcher that cannot be reached.

Usage:
    python3.12 tools/audit_negative_corpus.py
    python3.12 tools/audit_negative_corpus.py --compiler     # also consult tsc
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# --------------------------------------------------------------------------
# FROZEN REPRODUCTION of app/validated_fix.py as it stood at a62beac^.
#
# This is a HISTORICAL ARTEFACT and must never be imported by production code.
# It lives in tools/ so that the frozen-surface gate cannot mistake it for
# resurrection of the deleted module, and so `grep validated_fix app/` stays empty.
# Copied verbatim rather than paraphrased: a paraphrase could accidentally fix the
# bug, and then the replay would prove nothing.
# --------------------------------------------------------------------------
def _historical_validate_typescript(code: str) -> tuple:
    """Bracket matching. This is the entire TypeScript 'validator' that shipped."""
    stack = []
    pairs = {')': '(', ']': '[', '}': '{'}
    in_string = False
    string_char = None
    for i, char in enumerate(code):
        if in_string:
            if char == string_char and (i == 0 or code[i - 1] != '\\'):
                in_string = False
            continue
        if char in ('"', "'", '`'):
            in_string = True
            string_char = char
            continue
        if char in '([{':
            stack.append(char)
        elif char in ')]}':
            if not stack:
                return False, f"Unmatched closing '{char}' at position {i}"
            if stack[-1] != pairs[char]:
                return False, f"Mismatched '{char}' at position {i}"
            stack.pop()
    if stack:
        return False, f"Unclosed '{stack[-1]}'"
    return True, ""


def _historical_validate(code: str, language: str) -> tuple:
    """The dispatcher, including the `else: return True` that caused the damage."""
    if language == "python":
        try:
            compile(code, "<generated>", "exec")
            return True, ""
        except SyntaxError as e:
            return False, f"SyntaxError at line {e.lineno}: {e.msg}"
    elif language in ("typescript", "javascript"):
        return _historical_validate_typescript(code)
    else:
        return True, ""          # <-- the bug, preserved


# --------------------------------------------------------------------------
# The corpus. Each entry is a real historical false VALID.
# --------------------------------------------------------------------------
FIELD = "phoneNumber"

FIXTURE = os.path.join(ROOT, "fixtures", "typescript-openapi", "remove-field",
                       "consumer")

#: Every case is built from the REAL fixture file, not a synthetic lookalike.
#:
#: The first version of this corpus invented its own `before` containing a call to
#: an undefined `post()`. Writing that into the fixture project made `tsc` report
#: INVALID for all six cases -- including the half-fix, which I had claimed compiles.
#: The verdict was an artefact of the harness: the file failed for reasons that had
#: nothing to do with the fix under test. A measurement that agrees with your
#: argument for the wrong reason is worse than no measurement, because you stop
#: looking.
with open(os.path.join(FIXTURE, "src", "checkout.ts")) as _fh:
    BEFORE_CHECKOUT = _fh.read()

#: A consumer that takes the field as a PARAMETER. This is a separate file so it can
#: be added to the fixture project and genuinely typechecked -- which is the only way
#: the "a half-fix compiles" claim can be measured instead of asserted.
BEFORE_NOTIFY = """\
import { User } from "./types";

export function notify(user: User, phoneNumber: string): void {
  console.log(`${user.email} ${phoneNumber}`);
}
"""

CORPUS = [
    {
        "id": "known_bad_fix_001_proto_type_in_typescript",
        "what_happened": "the generator emitted proto syntax into a .ts file; "
                         "brackets balanced, so the old validator said VALID",
        "language": "typescript",
        "target": "src/checkout.ts",
        "before": BEFORE_CHECKOUT,
        "after": BEFORE_CHECKOUT.replace(
            "    phone: user.phoneNumber,", "    phoneNumber: int32 = 4;"),
        "blocked_by": "diff",
        "compiler_note": None,
    },
    {
        "id": "known_bad_fix_002_java_field_in_typescript",
        "what_happened": "Java member syntax emitted into a .ts file; no brackets "
                         "at all, so bracket matching had nothing to object to",
        "language": "typescript",
        "target": "src/checkout.ts",
        "before": BEFORE_CHECKOUT,
        "after": BEFORE_CHECKOUT.replace(
            "    phone: user.phoneNumber,", "    public int32 PhoneNumber;"),
        "blocked_by": "diff",
        "compiler_note": None,
    },
    {
        "id": "known_bad_fix_003_half_fix_parameter_kept",
        "what_happened": "the usage was removed but the function parameter that fed "
                         "it was left behind -- a partial removal",
        "language": "typescript",
        "target": "src/notify.ts",
        "before": BEFORE_NOTIFY,
        "after": BEFORE_NOTIFY.replace(
            "  console.log(`${user.email} ${phoneNumber}`);",
            "  console.log(`${user.email}`);"),
        "blocked_by": "diff",
        # The entry that justifies the whole diff layer -- and the claim is
        # MEASURED by --compiler, not asserted here.
        "compiler_note":
            "an unused parameter is legal TypeScript, so this compiles -- while the "
            "signature still demands a phoneNumber from every caller for a field "
            "that no longer exists",
    },
    {
        "id": "known_bad_fix_004_not_typescript_at_all",
        "what_happened": "`!!! not rust` -- placeholder text from a template that "
                         "had no implementation for the language",
        "language": "typescript",
        "target": "src/checkout.ts",
        "before": BEFORE_CHECKOUT,
        "after": "!!! not rust\n",
        "blocked_by": "diff",
        "compiler_note": None,
    },
    {
        "id": "known_bad_fix_005_no_op_identical",
        "what_happened": "the generator returned the input unchanged and the result "
                         "was reported as a fix; nothing ever asked 'did it change?'",
        "language": "typescript",
        "target": "src/checkout.ts",
        "before": BEFORE_CHECKOUT,
        "after": BEFORE_CHECKOUT,
        "blocked_by": "no-op",
        # Measured, and it corrected me: for remove_field the compiler DOES catch a
        # no-op, because the reason the fix was needed is that the field is gone from
        # the type. That is specific to removals and must not be generalised.
        "compiler_note":
            "tsc catches this one, because a no-op leaves the very compile error the "
            "fix existed to repair -- true for removals, not for fixes in general",
    },
    {
        "id": "known_bad_fix_006_no_op_whitespace_only",
        "what_happened": "the only difference was trailing whitespace -- defeats a "
                         "naive `before != after` check, which case 005 does not",
        "language": "typescript",
        "target": "src/checkout.ts",
        "before": BEFORE_CHECKOUT,
        "after": BEFORE_CHECKOUT.replace(
            "export function toCrmPayload(user: User): Record<string, string> {",
            "export function toCrmPayload(user: User): Record<string, string> {   "),
        "blocked_by": "diff",
        "compiler_note":
            "whitespace is insignificant to tsc, so this is the no-op case again "
            "wearing a disguise the byte-comparison misses",
    },
]


def _run_stack(case: dict) -> dict:
    """Put one candidate fix through the current safety layers, in order."""
    from app.diff_contract import check

    before, after = case["before"], case["after"]

    # Layer 0 -- no-op. Byte-identical output is not a fix, and is checked first
    # because it is the cheapest and the least ambiguous.
    if after == before:
        return {"blocked": True, "layer": "no-op",
                "detail": "output is byte-identical to the input"}

    # Layer 1 -- diff contract. Did it change ONLY references to the field?
    verdict = check(before, after, FIELD)
    if not verdict.ok:
        return {"blocked": True, "layer": "diff",
                "detail": verdict.violations[0]}

    return {"blocked": False, "layer": "",
            "detail": "NOTHING STOPPED THIS -- it would reach a PR"}


def _consult_compiler(case: dict) -> str:
    """Ask tsc what it thinks of THIS case. Never used to decide the gate.

    Isolating the file under test matters more than it looks. The fixture ships
    UNFIXED -- `checkout.ts` still references a field the type no longer has -- so
    typechecking a new file inside it would report checkout's error too, and every
    case would come back INVALID for a reason that has nothing to do with the case.
    When a case targets some other file, the known-correct fix is applied to
    checkout.ts first so the project baseline compiles and the verdict is about the
    case alone.
    """
    import shutil
    import tempfile

    from app.validation import validate, choose_backend

    backend, _note = choose_backend()
    if not backend:
        return "unavailable"

    tmp = tempfile.mkdtemp(prefix="ripple-negative-")
    try:
        work = os.path.join(tmp, "consumer")
        shutil.copytree(FIXTURE, work,
                        ignore=shutil.ignore_patterns("node_modules", ".git"))

        if case["target"] != "src/checkout.ts":
            checkout = os.path.join(work, "src", "checkout.ts")
            baseline = (BEFORE_CHECKOUT
                        .replace(" ${user.phoneNumber}", "")
                        .replace("\n    phone: user.phoneNumber,", ""))
            assert "user.phoneNumber" not in baseline, \
                "the baseline fix no longer removes both references -- the fixture " \
                "moved and this helper is stale"
            with open(checkout, "w") as fh:
                fh.write(baseline)

        target = os.path.join(work, *case["target"].split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as fh:
            fh.write(case["after"])

        return validate("typescript", work).state.value
    except Exception as exc:                      # noqa: BLE001
        return f"error: {type(exc).__name__}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list) -> int:
    with_compiler = "--compiler" in argv

    print("=" * 78)
    print("NEGATIVE FIX CORPUS -- every entry was once called VALID")
    print("=" * 78)

    failures = []
    not_historical = []
    layer_counts = {}
    compiler_says = {}

    for case in CORPUS:
        # 1. Prove the case is genuinely historical: the OLD validator must have
        #    said VALID. If it says otherwise, this entry is a strawman and the
        #    corpus is being padded.
        was_valid, err = _historical_validate(case["after"], case["language"])
        if not was_valid:
            not_historical.append(
                f"{case['id']}: the deleted validator REJECTED this ({err}), so it "
                f"is not one of the false VALIDs and does not belong in this corpus")

        # 2. Prove the current stack blocks it.
        result = _run_stack(case)
        if not result["blocked"]:
            failures.append(f"{case['id']}: {result['detail']}")
        else:
            layer_counts[result["layer"]] = layer_counts.get(result["layer"], 0) + 1

        # 3. The declared blocking layer must be the one that actually fired.
        if result["blocked"] and result["layer"] != case["blocked_by"]:
            failures.append(
                f"{case['id']}: declared blocked_by={case['blocked_by']!r} but "
                f"{result['layer']!r} is what actually stopped it -- a layer changed "
                f"behaviour and another covered for it")

        mark = "BLOCKED" if result["blocked"] else "*** ESCAPED ***"
        print(f"\n  {case['id']}")
        print(f"    then   VALID  (bracket matching had no objection)"
              if was_valid else f"    then   rejected -- NOT HISTORICAL")
        print(f"    now    {mark} by {result['layer'] or 'nothing'}")
        print(f"           {result['detail'][:96]}")
        if case["compiler_note"]:
            print(f"    note   {case['compiler_note'][:96]}")

        if with_compiler:
            state = _consult_compiler(case)
            compiler_says[state] = compiler_says.get(state, 0) + 1
            print(f"    tsc    {state}")

    print("\n" + "-" * 78)
    print(f"  cases                {len(CORPUS)}")
    print(f"  blocked              {len(CORPUS) - len(failures)}")
    for layer, n in sorted(layer_counts.items()):
        print(f"    by {layer:<16} {n}")
    if with_compiler:
        print("  what the compiler alone would have said:")
        for state, n in sorted(compiler_says.items()):
            note = "  <- would have SHIPPED" if state == "VALID" else ""
            print(f"    {state:<20} {n}{note}")

    if not_historical:
        print(f"\n  {len(not_historical)} ENTRY(S) NOT HISTORICAL:")
        for msg in not_historical:
            print(f"      {msg}")
    if failures:
        print(f"\n  {len(failures)} FAILURE(S):")
        for msg in failures:
            print(f"      {msg}")

    if failures or not_historical:
        return 1

    print("\n  all six remain blocked, each by the layer that claims to stop it.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
