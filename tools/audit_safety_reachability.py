#!/usr/bin/env python3
"""Is each safety layer actually REACHABLE from the production entry point?

WHAT THIS FOUND, IN ITS OWN AUTHOR'S WORK
Stage 3 reported wiring the diff contract "into the pipeline so it gates AUTO".
Stage 4 built a six-case corpus asserting no historical bad fix can reach AUTO.
Both were true of a test harness. Neither was true of production:

    app/diff_contract.py   imported by tests/ and tools/ ONLY
    app/validation.py      only describe_backend() is imported, by a health endpoint

The transformation IS wired (fix_templates -> ts_codemod). The two layers that
VERIFY it are not. In the request path, `pr_level()` decides AUTO from build-time
registry evidence and `_create_fix_pr()` runs, without the compiler or the diff
contract ever seeing the generated code.

The consequence is exact rather than vague. Of the six corpus cases, five are also
rejected by `tsc` on their own merits, so a production run would catch them anyway.
One is not: `known_bad_fix_003`, the half-fix that keeps a function parameter, which
`tsc` accepts as VALID. It is blocked ONLY by the diff contract. So the single case
that justified building the diff layer is the single case that layer cannot catch
where it matters.

This is the defect class this repository keeps rediscovering -- built, tested,
CI-gated, unreachable -- and it has now appeared at the level of the safety layers
themselves.

WHY THIS IS A GATE AND NOT A NOTE
A note about this lasted one stage. The rule the frozen-surface gate encodes applies
here too: an unenforced fact decays. So reachability is DECLARED, with a reason and a
named consequence, and the gate fails when reality diverges from the declaration in
EITHER direction:

    a declared-reachable layer becomes unreachable   -> regression, fail
    a declared-unreachable layer becomes reachable   -> fail, so that wiring a layer
                                                        forces someone to delete the
                                                        consequence text and state
                                                        what is now true

The second direction is the unusual one and it is deliberate. Wiring a safety layer
is good news that must not land silently, because the corpus and the coverage audit
both make claims that change meaning the moment it happens.

REACHABILITY IS TRANSITIVE, FROM THE ENTRY POINT
Computed as a closure from app/webhook.py. "Imported by something in app/" is not
enough: app/diff_contract.py imports app/ts_codemod, and if mere membership counted,
an unreachable module would launder its own dependencies into looking reachable.
"""
from __future__ import annotations

import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "app")

ENTRY = "webhook"

#: Every safety-relevant module, its role, and whether production can reach it.
#: `consequence` is REQUIRED when reachable is False -- "it is not wired" is not a
#: finding until someone states what that costs.
LAYERS = {
    "ts_codemod": {
        "role": "the transformation itself -- decides what is safe to edit and "
                "refuses the rest",
        "reachable": True,
        "consequence": None,
    },
    "diff_contract": {
        "role": "diff correctness -- did the change touch ONLY references to the "
                "removed field. Wired at fix_templates._remove_field_typescript, "
                "immediately after the codemod; on violation the ORIGINAL code is "
                "returned so a bad patch cannot propagate",
        "reachable": True,
        "consequence": None,
    },
    "validation": {
        "role": "the compiler -- does the patched project typecheck. Wired at "
                "webhook._validate_fix_against_tree, and app/routing.pr_level() now "
                "grants AUTO only when THIS patch returns VALID",
        "reachable": True,
        "consequence": None,
    },
    "repo_workspace": {
        "role": "fetches the repository TREE, so a compiler sees a project rather "
                "than an orphan file. Wired at webhook._fetch_consumer_tree -- one "
                "tree per consumer repository, LRU of one",
        "reachable": True,
        "consequence": None,
    },
}

#: Imports that exist only to report state, and cannot gate anything. Counting these
#: as "reachable" would let a health endpoint make a safety layer look wired.
#:
#: DEGRADED_OPT_IN was added here after /health/capability began importing it to name
#: the env var in its hint. That import made `validation` report as REACHABLE and the
#: gate failed -- correctly. A constant used to compose a sentence is not a call into
#: the validator, and the distinction is the whole point: REPORTED is not WIRED.
REPORTING_ONLY = {("validation", "describe_backend"),
                  ("validation", "DEGRADED_OPT_IN")}


def _module_imports(path: str) -> set:
    """Local `app.*` modules imported by one file, with reporting-only ones dropped."""
    with open(path) as fh:
        tree = ast.parse(fh.read(), filename=path)

    me = os.path.splitext(os.path.basename(path))[0]
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[-1]
            names = {a.name for a in node.names}
            if not mod:
                # `from . import activity` -- node.module is None and the NAMES are
                # the modules. The first version read node.module, got "", and
                # skipped the statement, so this entire import form was INVISIBLE to
                # the gate -- and app/webhook.py uses it (`from . import activity as
                # _activity`), so the blind spot was in the file being scanned.
                #
                # Found by mutation: wiring repo_workspace with this form did NOT
                # fail the gate. A gate that cannot see a real import is worse than
                # no gate, because it reports "unreachable" with confidence.
                #
                # No reporting-only exemption applies here: importing a whole module
                # grants access to everything in it, which is wiring by any measure.
                found.update(names)
                continue
            if all((mod, n) in REPORTING_ONLY for n in names):
                continue
            found.add(mod)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[-1])

    found.discard(me)
    return {m for m in found
            if os.path.isfile(os.path.join(APP, f"{m}.py"))}


def _reachable_from_entry() -> set:
    """Transitive closure of app modules reachable from the production entry point."""
    seen, stack = {ENTRY}, [ENTRY]
    while stack:
        mod = stack.pop()
        path = os.path.join(APP, f"{mod}.py")
        if not os.path.isfile(path):
            continue
        for dep in _module_imports(path):
            if dep not in seen:
                seen.add(dep)
                stack.append(dep)
    return seen


#: Safety-relevant code whose reachability is a FUNCTION-level question.
#:
#: WHY MODULE GRANULARITY IS NOT ENOUGH
#: app/fix_generator.py is imported by app/webhook.py for other reasons, so LAYERS
#: would report it REACHABLE -- and always would have, including for the months the
#: LLM branch inside it was dead. Measured:
#:
#:     app/webhook.py   generate_fix imported: True   referenced anywhere: False
#:     app/cli.py       calls generate_fixes -> generate_fix   the ONLY live route
#:
#: The gate `if use_llm and _llm_key()` lives inside generate_fix(), so setting an
#: API key in production changed nothing, and a real webhook run confirmed it: every
#: fix source was [template] or [RAG/template], never [llm]. A module-level gate is
#: structurally blind to that, which is why the coarseness -- not the wiring -- was
#: the defect. Reachability here is computed from the CALL GRAPH, reusing the one in
#: audit_pipeline_governance rather than writing a second: this repository's dominant
#: failure is one concept implemented twice.
FUNCTION_LAYERS = {
    ("fix_generator", "_generate_with_llm"): {
        "role": "the ONLY path on which customer source code leaves the machine. "
                "Reached from webhook._generate_fix_with_rag_fallback via "
                "generate_fix() -- never directly, because the diff contract that "
                "verifies an LLM patch lives in generate_fix and calling the "
                "generator straight would bypass it. Gated on a CONFIGURED BACKEND: "
                "with none, nothing is attempted and no source leaves the process",
        "reachable": True,
        "consequence": None,
        #: The path is declared HOP BY HOP, each hop module-qualified.
        #:
        #: The first version asked "is the bare name `_generate_with_llm` in the
        #: transitive closure of bare callee names?" and a MUTATION EXPOSED IT: with
        #: the wiring deleted the gate still passed. Cause -- app/capabilities.py:227
        #: also defines `generate_fix`, and the governance audit's call graph stores
        #: callees as BARE names, so reaching that unrelated function expanded
        #: fix_generator.generate_fix too and the LLM branch looked reachable no
        #: matter what production did.
        #:
        #: A gate that cannot be made to fail is not a gate. Each hop is now checked
        #: inside the CALLER'S OWN MODULE, so a same-named function elsewhere cannot
        #: satisfy it.
        "path": (
            ("webhook", "_generate_fix_with_rag_fallback"),
            ("fix_generator", "generate_fix"),
            ("fix_generator", "_generate_with_llm"),
        ),
    },
}


def _calls_within(module: str, caller: str, callee: str) -> bool:
    """Does `module.caller()` contain a call to `callee`, by name?

    Deliberately per-module. Resolving imports fully is a bigger job than this gate
    needs, but scoping the question to ONE function in ONE file removes the
    cross-module bare-name collision that made the first version unfalsifiable.
    """
    path = os.path.join(APP, f"{module}.py")
    if not os.path.isfile(path):
        return False
    tree = ast.parse(open(path).read(), filename=path)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == caller), None)
    if fn is None:
        return False
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
            if name == callee:
                return True
        # `from .fix_generator import generate_fix as _generate_fix` inside the
        # function body renames the callee; follow the alias so an aliased import
        # does not read as "not wired". Same blind spot the module-level scanner
        # had with `from . import x`.
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == callee and a.asname:
                    for sub in ast.walk(fn):
                        if isinstance(sub, ast.Call):
                            g = sub.func
                            nm = g.id if isinstance(g, ast.Name) else getattr(g, "attr", "")
                            if nm == a.asname:
                                return True
    return False


def _function_reachable(target: tuple, spec: dict) -> bool:
    """Every declared hop must really call the next one, in the caller's module."""
    hops = spec.get("path") or ()
    if len(hops) < 2:
        raise ValueError(f"{target}: 'path' needs at least two hops to verify")
    for (mod, caller), (_next_mod, callee) in zip(hops, hops[1:]):
        if not _calls_within(mod, caller, callee):
            return False
    return True


def main(argv: list) -> int:
    reachable = _reachable_from_entry()

    print("=" * 78)
    print(f"SAFETY LAYER REACHABILITY  (transitive from app/{ENTRY}.py)")
    print("=" * 78)

    failures = []
    for name, spec in sorted(LAYERS.items()):
        actually = name in reachable
        declared = spec["reachable"]
        ok = actually == declared

        state = "REACHABLE" if actually else "unreachable"
        print(f"\n  app/{name}.py")
        print(f"    role       {spec['role']}")
        print(f"    production {state}   (declared {'reachable' if declared else 'unreachable'})")

        if not declared:
            if not spec["consequence"]:
                failures.append(
                    f"{name}: declared unreachable with no `consequence` -- "
                    f"'not wired' is not a finding until the cost is stated")
            else:
                print(f"    cost       {spec['consequence'][:150]}")

        if not ok and actually:
            failures.append(
                f"{name}: is NOW REACHABLE from production but is declared "
                f"unreachable. This is good news that must not land silently -- the "
                f"negative corpus and the coverage audit both make claims that change "
                f"meaning once this layer runs in the request path. Update the "
                f"declaration and delete the `consequence` text.")
        elif not ok and not actually:
            failures.append(
                f"{name}: declared REACHABLE but production cannot reach it -- a "
                f"safety layer was disconnected. This is the regression this gate "
                f"exists for.")

    print("\n" + "-" * 78)
    print(f"  app modules reachable from {ENTRY}   {len(reachable)}")
    wired = sum(1 for n in LAYERS if n in reachable)
    print(f"  safety layers wired                  {wired} of {len(LAYERS)}")

    # --- function-level layers ------------------------------------------------
    # Same two-directional rule as above. A branch inside an imported module is
    # invisible to module granularity, so it is declared and checked separately.
    entry_fn = f"app/{ENTRY}.py:github_webhook"
    for (mod, fn), spec in sorted(FUNCTION_LAYERS.items()):
        try:
            actually = _function_reachable((mod, fn), spec)
        except Exception as exc:                                # noqa: BLE001
            failures.append(
                f"{mod}.{fn}: the declared path could not be verified ({exc}), so "
                f"function-level reachability is UNKNOWN. Absence of evidence is "
                f"not evidence -- this is a failure, not a pass.")
            continue
        declared = spec["reachable"]
        state = "REACHABLE" if actually else "unreachable"
        print(f"\n  app/{mod}.py:{fn}()")
        print(f"    role       {spec['role'][:150]}")
        print(f"    production {state}   (declared "
              f"{'reachable' if declared else 'unreachable'})")
        if not declared and not spec["consequence"]:
            failures.append(
                f"{mod}.{fn}: declared unreachable with no `consequence` -- "
                f"'not wired' is not a finding until the cost is stated")
        elif not declared:
            print(f"    cost       {spec['consequence'][:150]}")
        if actually != declared:
            failures.append(
                f"{mod}.{fn}: declared "
                f"{'reachable' if declared else 'unreachable'} but production "
                f"{'cannot reach it' if declared else 'NOW reaches it'}. Update the "
                f"declaration -- module granularity cannot see this, so nothing "
                f"else will tell you.")

    if failures:
        print(f"\n  {len(failures)} FAILURE(S):")
        for msg in failures:
            print(f"      {msg}")
        return 1

    print("\n  reachability matches the declaration, and every gap names its cost.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
