# python × openapi × remove_field

The **fourth** production-ready cell, and the one that was impossible this morning.

## What actually unblocked it

The capability registry listed this cell as blocked on nothing but a fixture. A
fixture written against the old handler would have **failed**, because
`_remove_field_python` was five context-free regexes that returned the file
untouched while reporting success. Measured on this fixture's own consumer:

```
input                                       output
f"{user.full_name} ... {user.phone_number}" UNCHANGED
"phone": user.phone_number,                 UNCHANGED

reported: "Removed references to field 'phone_number' (2 lines affected).
           Cleaned: struct/class declarations, accessor methods, function
           params, object literals, and direct field access patterns for python."
```

Two false claims in one string: nothing was removed, and the five named categories
were a fixed list appended to every success. `changed` was True only because a
blank-line run collapsed — which is the dangerous part, because the pipeline then
believes a fix exists and only the validator stands between that and a PR.

Two of the five regexes were **destructive rather than merely useless**: one stripped
a function parameter — the shape `ts_codemod` explicitly refuses because it breaks
every caller — and one deleted the whole line containing a subscript, taking the
assignment and any call on it. No test noticed, because there was no Python fixture
until the validator was wired.

So: "blocked on a fixture" is the registry naming the next thing to try, not a
promise it passes. Writing `app/py_codemod.py` is what unblocked this cell. The
fixture was the cheapest part.

## The inversion, in the other direction

| cell | declaration | usages | Ripple edits |
|---|---|---|---|
| `change_field_type` (both languages) | **stale** | correct | the declaration |
| `remove_field` (this cell) | correct | **stale** | the usages |

`src/models.py` is regenerated from `user.after.yaml` and is already right, so the
stale references live in `src/checkout.py`. The test byte-compares `models.py` —
touching it would mean the codemod went after the declaration instead of the usages.
Same relationship as the TypeScript `remove-field` cell.

## What it proves

Measured 2026-08-23 by `test_e2e_python_openapi_remove_field` against the real
toolchain — docker, `python:3.11-alpine`, `typecheck_exit: 0`:

```
before fix   INVALID, exactly 2 attr-defined errors, 0 annotation gaps
after fix    VALID
edits        f-string interpolation + dict-literal entry, in src/checkout.py only
untouched    src/models.py, src/orders.py, src/__init__.py, mypy.ini,
             requirements-dev.txt (byte-compared)
```

The test also asserts what the old handler got wrong, specifically: that
`user.phone_number` is genuinely absent afterwards, that the explanation names the
shapes actually removed rather than the fabricated category list, that the output
still parses, and that PEP 8's two blank lines between top-level defs survive.

## Three hazards

**1. mypy's reach depends on the consumer's annotations.** Every def here is
annotated. An unannotated one would make the verdict `UNABLE_TO_VALIDATE` — mypy
could not see the type of `user`, so both attribute errors would be invisible. The
test asserts `mypy_annotation_gaps == 0` before the fix for exactly that reason.

**2. Only some shapes are removable.** Both references here are shapes the codemod
edits. A parameter, an aliased local, an unpacking, a keyword argument or a subscript
would be **refused** — correctly — and this cell would not qualify. Refusal is the
feature, not a gap.

**3. The blank-line normaliser used to reformat the whole file.**
`_clean_blank_lines` claimed to collapse 3+ blank lines to 2; its regex collapsed
them to one. PEP 8 wants two between top-level definitions, so every Python fix
arrived as a diff touching every function boundary. Fixed to match its own
docstring.

## Where this leaves the matrix

```
proven cells   4      typescript/openapi/{remove_field, change_field_type}
                      python/openapi/{change_field_type, remove_field}
validators     3 of 3 wired (typescript, python, go)
Go             wired, zero cells proven -- no Go fixture exists yet
```

`python type_removed` is still broken the same way `remove_field` was: it reports
success while leaving the type name in place. `field_renamed` and
`change_field_type` work.
