#!/usr/bin/env python3
"""Prove the validation layer actually distinguishes good code from bad.

NOT A CI GATE, deliberately -- it needs docker or a working node, and a gate that
cannot run is the same defect as a matcher that cannot be reached. Same reasoning as
tools/verify_durability.py: this is an acceptance check you run, and whose result
Stage 6 cites as evidence.

Three cases, and the third is the one that matters:

    broken fixture          -> INVALID   (tsc must reject what does not compile)
    hand-written fix        -> VALID     (tsc must accept what does)
    RIPPLE's own output     -> INVALID   (the validator must reject OUR bad fix)

If the third case ever returns VALID, the validator is decorative: it would be
signing off on the `phone: user.};` that the TypeScript remove_field handler
currently produces.

Usage:
    python3.12 tools/verify_validation.py            # all three cases
    python3.12 tools/verify_validation.py --backend host
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FIXTURE = os.path.join(ROOT, "fixtures", "typescript-openapi", "remove-field",
                       "consumer")


def _prepare(transform=None) -> str:
    """Copy the fixture and optionally rewrite checkout.ts.

    READ BEFORE WRITE, asserted. `open(p, "w").write(transform(open(p).read()))`
    truncates the file before the inner read, so `transform` receives an EMPTY
    string and an empty file typechecks clean -- which produced two false VALIDs the
    first time this proof was run.
    """
    tmp = tempfile.mkdtemp(prefix="ripple-verify-")
    work = os.path.join(tmp, "consumer")
    shutil.copytree(FIXTURE, work,
                    ignore=shutil.ignore_patterns("node_modules", ".git"))
    if transform is not None:
        path = os.path.join(work, "src", "checkout.ts")
        with open(path) as fh:
            original = fh.read()
        new = transform(original)
        assert new != original, "transform was a no-op -- the proof would be vacuous"
        assert new.strip(), "transform produced an EMPTY file"
        with open(path, "w") as fh:
            fh.write(new)
    return work


def _correct(src: str) -> str:
    return (src.replace(" ${user.phoneNumber}", "")
               .replace("\n    phone: user.phoneNumber,", ""))


def _ripple(src: str) -> str:
    from app.fix_templates import apply_fix_template
    fixed, _ = apply_fix_template(code=src, language="typescript",
                                  change_type="removed_field",
                                  field_name="phoneNumber")
    return fixed


def main(argv: list) -> int:
    from app.validation import validate, choose_backend

    backend = ""
    if "--backend" in argv:
        backend = argv[argv.index("--backend") + 1]

    chosen, note = (backend, "explicit") if backend else choose_backend()
    print("=" * 74)
    print("VALIDATION ACCEPTANCE")
    print("=" * 74)
    print(f"\n  backend  {chosen or 'NONE'}")
    print(f"  isolation {note}\n")

    if not chosen:
        print("  FAIL  no backend: docker unreachable and no node on this host can")
        print("        execute. This is UNABLE_TO_VALIDATE, which is not a pass.")
        return 1

    cases = [
        ("broken fixture", None, "INVALID"),
        ("hand-written correct fix", _correct, "VALID"),
        ("Ripple's own generated fix", _ripple, "INVALID"),
    ]
    failures = []
    for label, transform, expected in cases:
        work = _prepare(transform)
        try:
            verdict = validate("typescript", work, backend=backend)
        finally:
            shutil.rmtree(os.path.dirname(work), ignore_errors=True)
        got = verdict.state.value
        ok = got == expected
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<30} {got:<20} "
              f"(expected {expected})")
        for err in verdict.errors[:2]:
            print(f"            {err}")
        if not ok:
            failures.append(f"{label}: expected {expected}, got {got} "
                            f"-- {verdict.reason}")

    if failures:
        print(f"\n  {len(failures)} FAILURE(S):")
        for f in failures:
            print(f"      {f}")
        return 1

    print("\n  the validator accepts correct code, rejects broken code, and rejects")
    print("  RIPPLE'S OWN output -- which is the case that stops it being decorative.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
