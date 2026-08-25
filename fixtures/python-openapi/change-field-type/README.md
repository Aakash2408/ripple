# python × openapi × change_field_type

The **third** production-ready cell, and the first outside TypeScript. Its job is to
prove the newly wired **Python validator**, so the operation is held constant with
the TypeScript `change-field-type` cell: a failure here is attributable to
validation rather than to a codemod nobody had exercised.

## Why the validator is mypy and not what the registry declared

`capability_claims.VALIDATORS` declared Python as `compileall + optional pytest`,
and its own note already recorded the problem. That was measured rather than
assumed:

```
compileall,  consumer reads a DELETED field      exit 0     -> blind
mypy,        consumer annotated                  exit 1     -> "User has no attribute phone_number"
mypy,        consumer NOT annotated, same break  exit 0     -> also blind
```

A validator that cannot fail on a genuinely broken consumer is not a validator, and
wiring `compileall` would have granted AUTO on evidence of nothing.

## The property Python has that TypeScript does not

`tsc` reads the project's own compiler config and typechecks everything it covers.
**mypy's reach depends on the consumer's annotation coverage.** The third row above
is the whole problem: an exit 0 from mypy means either "the code is fine" *or* "I
could not see the types". Collapsing those into `VALID` is the
absence-of-evidence-as-evidence defect this codebase keeps rediscovering.

So the runner passes `--disallow-untyped-defs` and splits the result three ways by
error code:

| mypy reports | verdict |
|---|---|
| any error that is **not** `no-untyped-def` | `INVALID` — a real type error |
| **only** `no-untyped-def` errors | `UNABLE_TO_VALIDATE` — cannot see enough |
| no errors at all | `VALID` |

A partially annotated consumer therefore cannot reach AUTO. That is the correct
conservative answer, not a limitation to be worked around, and
`test_python_validator_refuses_to_pass_an_unannotated_consumer` holds it in place by
stripping annotations off *this* fixture and asserting the verdict degrades.

Note the precedence: a real type error outranks an annotation gap. The first draft
of that guard test stripped only one of the two functions and got `INVALID` —
correctly, because the other function still errored. Isolating the gap path required
removing every real error first.

## Docker only, and why

`choose_backend()`'s host fallback probes for a **node** binary. A node install is
no evidence that python or mypy exist, so accepting `host` here would run an unknown
toolchain. The runner refuses, which keeps the degraded host path TypeScript-only.

The venv also has to live *inside* the mounted volume — `pip` installs into the
container's own site-packages by default, which the read-only check container does
not have. The first draft did exactly that and mypy was simply not found in phase
two. `.ripple-venv` is the Python equivalent of `node_modules`, and the runner
`--exclude`s it, because otherwise `mypy .` typechecks mypy's own dependencies and
reports thousands of third-party errors as the consumer's.

## What it proves

Measured 2026-08-23 by `test_e2e_python_openapi_change_field_type` against the real
toolchain — docker, `python:3.11-alpine`, `typecheck_exit: 0`:

```
before fix   INVALID, exactly 2 errors, both attr-defined in src/contact.py
after fix    VALID
edit         src/models.py only, ONE line
untouched    src/contact.py, src/orders.py, src/__init__.py, mypy.ini,
             requirements-dev.txt (byte-compared)
```

## Three hazards, all measured

**1. Annotation dependence** — covered above.

**2. `_change_type_python` is field-blind.** It rewrites every occurrence of the old
annotation and never reads `field_name`. `src/models.py` declares exactly one
int-annotated field; a second would be silently retyped.

**3. The codemod does not respect comments.** The first draft of this fixture wrote
the annotation pattern out literally in its docstring, and the codemod reported *"3
type annotation(s) updated"* for a one-field change — two of them inside the
comment. The TypeScript sibling had the identical flaw and was fixed at the same
time; there the rewritten comment became self-contradictory. Both fixtures now
*describe* the annotation instead of quoting it, and the test asserts exactly one
line differs so a comment rewrite fails the cell rather than inflating it.

## The cell this is NOT

`python/openapi/remove_field` would have mirrored the first cell, and it is **not
production-ready**: the Python `remove_field` codemod returns the file with every
reference still present while reporting *"Removed references to field
'phone_number' (2 lines affected)"*. It also collapses blank lines, so it produces a
spurious diff while achieving nothing — the same shape as the Stage 4 TypeScript
baseline that forced `app/ts_codemod.py` to be written. `python type_removed` is
broken the same way. `field_renamed` and `field_type_changed` genuinely transform.

Wiring the validator took `validate_ok` from 63 cells to 126 and left 33 Python
mechanical cells blocked on nothing but a fixture — but for `remove_field` and
`type_removed` the codemod is the real blocker, and a fixture there would expose
that rather than close it.
