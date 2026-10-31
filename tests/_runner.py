"""A dependency-free runner for pytest-style test files.

WHY
---
tests/test_poll.py and tests/test_follow_on_execute.py were written as pytest
classes, and the pre-commit hook and CI run every gated test file as
`python tests/<file>.py`. Neither file had a `__main__` block and pytest is not
installed, so each "run" imported the module, executed zero tests, and exited
0. Two gates reported green for as long as they existed while checking nothing.

The fix is a runner that is FAIL-CLOSED on emptiness: discovering zero tests is
an error, not a pass. A runner that can report success on nothing is the bug
being fixed, so it must not be able to reproduce it.

Supports the subset these files use: module-level `test_*` functions, `Test*`
classes with `test_*` methods, and `setup_method` / `teardown_method`.

Usage, at the bottom of a test file:

    if __name__ == "__main__":
        from _runner import run          # tests/ is sys.path[0] when run directly
        sys.exit(run(globals()))
"""
from __future__ import annotations

import inspect
import traceback


def _collect(namespace: dict) -> list:
    cases = []
    for name, obj in sorted(namespace.items()):
        if name.startswith("test_") and inspect.isfunction(obj):
            cases.append((name, None, obj))
        elif name.startswith("Test") and inspect.isclass(obj):
            for mname, meth in sorted(vars(obj).items()):
                if mname.startswith("test_") and callable(meth):
                    cases.append((f"{name}.{mname}", obj, mname))
    return cases


def run(namespace: dict) -> int:
    cases = _collect(namespace)
    if not cases:
        print("  ERROR  no tests were discovered -- refusing to report a pass")
        return 1
    failed = []
    for label, cls, target in cases:
        try:
            if cls is None:
                target()
            else:
                inst = cls()
                if hasattr(inst, "setup_method"):
                    inst.setup_method()
                try:
                    getattr(inst, target)()
                finally:
                    if hasattr(inst, "teardown_method"):
                        inst.teardown_method()
            print(f"  PASS  {label}")
        except AssertionError as e:
            failed.append(label)
            print(f"  FAIL  {label}\n          {e}")
        except Exception as e:
            failed.append(label)
            print(f"  ERROR {label}\n          {type(e).__name__}: {e}")
            traceback.print_exc(limit=3)
    print(f"\n  {len(cases) - len(failed)}/{len(cases)} passed")
    return 1 if failed else 0
