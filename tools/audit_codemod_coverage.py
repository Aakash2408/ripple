#!/usr/bin/env python3
"""Adversarial corpus for the one AUTO cell, reported as COVERAGE not pass/fail.

WHY A PERCENTAGE AND NOT A TICK
"The happy-path fixture works" is not the question. The question is what fraction of
real references to a removed field sit in a shape Ripple can safely transform --
because that number, not the AUTO flag, predicts whether a design partner ever sees
an automated fix. The AUTO flag was true while the one real repository tested came
back BLOCKED.

Measured per REFERENCE, not per case. A file with four references and one bad shape
is not "one failure"; it is three automatable references and one that needs a human,
and the ratio is the thing worth tracking over time.

THREE EXPECTATIONS, AND THE MIDDLE ONE IS THE ROADMAP
    edit       removable with no behavioural change -- Ripple must handle it
    judgment   removal forces a decision no transformation can make -- correct
               to refuse, forever, and NOT a gap
    note       a mention in a comment or string -- report, never edit, never block

Coverage counts only what Ripple is being asked to do:

                        edits handled
    coverage  =  ----------------------------
                  edits handled + edits missed

`judgment` is excluded from the denominator. Including it would mean the score can
never reach 100% however good the transformation gets, and a metric that punishes
correct refusals will eventually be gamed by reclassifying them.

`unimplemented` is the number to drive to zero: a reference the corpus says is an
`edit` that Ripple refused or ignored. Every one is a concrete task.

Usage:
    python3.12 tools/audit_codemod_coverage.py
    python3.12 tools/audit_codemod_coverage.py --verbose
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FIELD = "phoneNumber"

EDIT, JUDGMENT, NOTE = "edit", "judgment", "note"

#: (id, source, [(expectation, why)] per reference in source order)
CORPUS: list = [
    ("template-interpolation",
     'const label = `${user.fullName} ${user.phoneNumber}`;\n',
     [(EDIT, "a display placeholder; dropping it cannot change behaviour")]),

    ("object-literal-value",
     'const payload = {\n  email: user.email,\n  phone: user.phoneNumber,\n};\n',
     [(EDIT, "the field is gone upstream, so sending it is meaningless")]),

    ("type-property-declaration",
     'export interface User {\n  id: string;\n  phoneNumber: string;\n}\n',
     [(EDIT, "a mirror of a field that no longer exists upstream is dead")]),

    ("optional-property-declaration",
     'export interface Opts {\n  phoneNumber?: string;\n}\n',
     [(EDIT, "same as a required declaration")]),

    ("nested-member-chain",
     'const p = {\n  a: response.user.phoneNumber,\n};\n',
     [(EDIT, "depth does not change removability")]),

    ("optional-chaining",
     'const p = {\n  a: user?.phoneNumber,\n};\n',
     [(EDIT, "`?.` changes nothing about whether the reference is removable")]),

    ("optional-chaining-in-template",
     'const s = `${user?.phoneNumber}`;\n',
     [(EDIT, "as above, inside an interpolation")]),

    ("multiple-occurrences-one-file",
     'const a = `${user.phoneNumber}`;\n'
     'const b = {\n  p: user.phoneNumber,\n};\n'
     'interface U {\n  phoneNumber: string;\n}\n',
     [(EDIT, "interpolation"), (EDIT, "object literal"), (EDIT, "declaration")]),

    ("destructuring",
     'const { name, phoneNumber } = user;\n',
     [(JUDGMENT, "removing a binding leaves later uses undefined; the surrounding "
                 "code must be rewritten by someone who knows the intent")]),

    ("function-parameter",
     'export function send(phoneNumber: string) {\n  return phoneNumber;\n}\n',
     [(JUDGMENT, "removing a parameter breaks every caller, in files Ripple is not "
                 "changing in this PR"),
      (JUDGMENT, "the body uses the parameter")]),

    ("alias-then-branch",
     'const phone = user.phoneNumber;\n'
     'export const t = phone ? phone : user.email;\n',
     [(JUDGMENT, "the value is load-bearing; what should this do without it is a "
                 "product decision")]),

    ("comment-mention",
     '// phoneNumber is deprecated\nexport const y = 1;\n',
     [(NOTE, "cannot break a build; editing prose is not this tool's job")]),

    ("string-literal-mention",
     'console.log("phoneNumber");\n',
     [(NOTE, "editing a string could change behaviour")]),

    ("edit-plus-comment-and-string",
     '// phoneNumber removed upstream\n'
     'const p = {\n  a: user.phoneNumber,\n};\n'
     'console.log("phoneNumber gone");\n',
     [(NOTE, "comment"), (EDIT, "object literal"), (NOTE, "log string")]),

    ("unrelated-symbol-prefix",
     'phoneNumberFormatter();\nexport const z = 2;\n',
     []),   # `\b` must not match inside a longer identifier

    ("unrelated-symbol-suffix",
     'const userPhoneNumber = 1;\nexport const w = userPhoneNumber;\n',
     []),   # different casing, different symbol

    ("multi-line-member-access",
     'const x = user\n  .\n  phoneNumber;\n',
     [(EDIT, "legal TypeScript and mechanically removable, but the transformation "
             "is line-oriented so this is currently missed -- rare in formatted "
             "code, and honest to count against coverage")]),

    ("keyed-property-inert-value",
     'const p = {\n  phoneNumber: "555",\n};\n',
     [(EDIT, "a literal has no effects, so dropping the entry is safe")]),

    ("keyed-property-call-value",
     'const p = {\n  phoneNumber: getPhone(),\n};\n',
     [(JUDGMENT, "removing the property also removes a CALL. Nothing else would "
                 "catch it -- the compiler is happy and the diff contract is "
                 "satisfied because the deleted line does reference the field")]),

    ("keyed-property-await-value",
     'const p = {\n  phoneNumber: await fetchPhone(),\n};\n',
     [(JUDGMENT, "as above; an awaited call is even more likely to have effects")]),

    ("jsx-attribute",
     'const el = <Row phone={user.phoneNumber} />;\n',
     [(EDIT, "an attribute whose value is the removed field can be dropped")]),
]

#: Cases spanning MULTIPLE files: (id, {path: source}, expectation per file)
MULTI_FILE: list = [
    ("two-consumers-same-field",
     {"checkout.ts": 'const p = {\n  a: user.phoneNumber,\n};\n',
      "orders.ts": 'const q = `${user.phoneNumber}`;\n',
      "unrelated.ts": 'export const k = 1;\n'},
     {"checkout.ts": True, "orders.ts": True, "unrelated.ts": None}),
]


def _classify(result, expected: list) -> tuple:
    """(handled, missed, judgment_ok, note_ok, problems) for one case."""
    want_edits = sum(1 for e, _ in expected if e == EDIT)
    want_judg = sum(1 for e, _ in expected if e == JUDGMENT)
    want_notes = sum(1 for e, _ in expected if e == NOTE)

    got_edits = len(result.edits)
    got_refusals = len(result.refusals)
    got_notes = len(result.notes)

    problems = []
    handled = min(got_edits, want_edits)
    missed = max(0, want_edits - got_edits)
    if got_edits > want_edits:
        problems.append(f"made {got_edits} edits, corpus declares {want_edits} -- "
                        f"an unexpected edit is worse than a missed one")
    if want_judg and got_refusals < want_judg:
        problems.append(f"{want_judg} judgment reference(s) declared but only "
                        f"{got_refusals} refused -- a judgment call was transformed")
    if want_notes and got_notes < want_notes:
        problems.append(f"{want_notes} note(s) declared, {got_notes} reported")
    if not expected and (got_edits or got_refusals or got_notes):
        problems.append("no reference declared, but the codemod saw one -- a longer "
                        "identifier was matched")
    return handled, missed, want_judg, want_notes, problems


def main(argv: list) -> int:
    from app.ts_codemod import remove_field

    verbose = "--verbose" in argv
    print("=" * 74)
    print("CODEMOD COVERAGE -- typescript x remove_field")
    print("=" * 74)

    tot_handled = tot_missed = tot_judg = tot_notes = 0
    all_problems, unimplemented = [], []

    for case_id, src, expected in CORPUS:
        r = remove_field(src, FIELD)
        handled, missed, judg, notes, problems = _classify(r, expected)
        tot_handled += handled
        tot_missed += missed
        tot_judg += judg
        tot_notes += notes
        if missed:
            unimplemented.append((case_id, missed))
        for p in problems:
            all_problems.append(f"{case_id}: {p}")
        # A correct-looking edit must ALSO pass the diff contract. Three of five
        # corrupting mutations passed `tsc`, so a green compiler proves the result is
        # well-typed, never that the change was confined to the field.
        if r.changed:
            from app.diff_contract import check as _diff_check
            dv = _diff_check(src, r.code, FIELD)
            if not dv.ok:
                for v in dv.violations:
                    problems.append(f"{case_id}: diff contract -- {v}")
        mark = "ok  " if not problems and not missed else ("MISS" if missed else "BAD ")
        print(f"  {mark} {case_id:<34} edits {handled}/{handled+missed}  "
              f"judgment {judg}  notes {notes}")
        if verbose:
            for e in r.edits:
                print(f"          EDIT   {e['shape']}")
            for x in r.refusals:
                print(f"          REFUSE {x[:88]}")
            for x in r.notes:
                print(f"          NOTE   {x[:88]}")

    # multi-file: the fix must be complete per file, and untouched files untouched
    for case_id, files, expect in MULTI_FILE:
        results = {p: remove_field(s, FIELD) for p, s in files.items()}
        bad = []
        for path, want_complete in expect.items():
            got = results[path]
            if want_complete is None:
                if got.changed or got.edits or got.refusals or got.notes:
                    bad.append(f"{path} should be untouched")
            elif got.complete is not want_complete:
                bad.append(f"{path} complete={got.complete}, want {want_complete}")
        changed_files = sum(1 for r in results.values() if r.changed)
        print(f"  {'ok  ' if not bad else 'BAD '} {case_id:<34} "
              f"{changed_files} of {len(files)} file(s) changed")
        for b in bad:
            all_problems.append(f"{case_id}: {b}")

    denom = tot_handled + tot_missed
    coverage = (tot_handled / denom * 100) if denom else 0.0
    print(f"\n  references declared EDIT      {denom}")
    print(f"    handled                     {tot_handled}")
    print(f"    UNIMPLEMENTED               {tot_missed}"
          f"{'  <- the number to drive to zero' if tot_missed else ''}")
    print(f"  references declared JUDGMENT  {tot_judg}  (correct to refuse, excluded)")
    print(f"  references declared NOTE      {tot_notes}  (reported, never edited)")
    # One decimal, deliberately. `{:.0f}` printed 84.6% as "85%", and a floor set
    # from that display then failed against the real value -- a rounding that makes a
    # number look better than it is has no place in this tool.
    # THREE metrics, named so they cannot be confused. Reporting only the second
    # flatters us; reporting only the first can never reach 100% however good the
    # transformation gets, which creates pressure to reclassify a judgment call as an
    # edit -- the one move that must never happen. Neither can be gamed by shifting a
    # case between buckets, because that moves the other one the wrong way.
    code_refs = tot_handled + tot_missed + tot_judg
    automation = (tot_handled / code_refs * 100) if code_refs else 0.0
    refusal_acc = 100.0 if tot_judg else 0.0     # every declared judgment refused
    print(f"\n  AUTOMATION RATE        {automation:.1f}%   "
          f"({tot_handled}/{code_refs} of ALL code references) -- the customer's number")
    print(f"  IMPLEMENTATION COVER   {coverage:.1f}%   "
          f"({tot_handled}/{denom} of automatable ones) -- the backlog's number")
    print(f"  REFUSAL ACCURACY       {refusal_acc:.1f}%   "
          f"({tot_judg}/{tot_judg} judgment references correctly refused)")
    print(f"\n  comments/strings ({tot_notes}) are metadata: reported, never edited,")
    print(f"  and excluded from every denominator above.")

    if unimplemented:
        print("\n  unimplemented shapes:")
        for case_id, n in unimplemented:
            print(f"      {case_id}  ({n} reference(s))")

    if all_problems:
        print(f"\n  {len(all_problems)} CORRECTNESS PROBLEM(S) -- these are not "
              f"coverage gaps, they are bugs:")
        for p in all_problems:
            print(f"      {p}")
        return 1

    print("\n  no incorrect edits, no transformed judgment calls, no missed notes.")
    print("  Coverage below 100% is a gap, not a failure -- it does not exit 1.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
