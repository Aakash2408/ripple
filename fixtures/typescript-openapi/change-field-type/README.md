# typescript × openapi × change_field_type

The **second** production-ready cell. It exists to show that the bar set by the
`remove-field` fixture is repeatable, and that repeating it costs a fixture
rather than a subsystem.

## The change

`User.phoneNumber` goes from `type: integer` to `type: string` between
`spec/user.before.yaml` (v1.0.0) and `spec/user.after.yaml` (v1.1.0).

This is a realistic breaking change rather than a contrived one: `integer` was
always the wrong type for a phone number — it cannot hold a leading zero or a
leading `+`, so every non-NANP number was already being corrupted. Fixing the
type is correct and breaks every consumer that believed it.

## The inversion that makes this cell different

In the `remove-field` fixture, the type declaration is correct (regenerated
without the field) and the **usages** are stale, so Ripple edits the usages.

Here it is the other way round:

| | declaration | usages | Ripple edits |
|---|---|---|---|
| `remove_field` | correct | stale | usages |
| `change_field_type` | **stale** | **correct** | **declaration** |

`src/contact.ts` calls `.trim()` and `.startsWith("+")` on `phoneNumber`. Both
are already right for the new contract. They fail to compile only because
`src/types.ts` still declares `phoneNumber: number`.

So the file that **errors** is not the file that gets **fixed** — and the fixture
asserts `src/contact.ts` is byte-identical afterwards. That is the whole claim of
this cell: repair by correcting the declaration, without touching the code that
was never wrong.

`src/types.ts` is hand-written, not generated. If it were generated, a codegen
re-run would fix the type and Ripple would have no job. Plenty of real consumers
hand-maintain their interfaces, and those are the ones that go stale.

## Two hazards this fixture is shaped around

**1. `change_field_type` annotates instead of editing for unmapped types.**
`app/fix_templates.py` refuses to write a contract type name into source it has
no native mapping for — that behaviour exists because doing so once produced
`phoneNumber: int32;` in TypeScript and `public int32 PhoneNumber` in C#,
confident diffs that could not build. `integer → string` maps to
`number → string`, checked with `native_type()` before this fixture was written.
An unmapped pair would leave a comment, the file would still not compile, and the
cell would correctly fail to qualify.

**2. `_change_type_typescript` is field-blind.** It rewrites every
`: <old_type>` in the file and never reads `field_name`. `src/types.ts`
therefore declares exactly **one** number-typed field. A second would be
silently retyped and the minimal-diff assertion would fail.

The second one is a real limitation of the codemod, not of the fixture. A
consumer with two number-typed fields in the same file would get an over-broad
edit. It is recorded in `expected.json` under `hazards` rather than worked
around, because the fixture's job is to prove one cell, not to hide a defect that
will matter for the next one.

## What it proves, and what it cost

Measured 2026-08-23 by `test_e2e_typescript_openapi_change_field_type` against
the real toolchain — docker backend, `typecheck_exit: 0`:

```
before fix   FAIL, exactly 2 errors, both in src/contact.ts
after fix    PASS
edit         src/types.ts only, one annotation
untouched    src/contact.ts, src/orders.ts, tsconfig.json, package.json (byte-compared)
```

Cost: **no new validator, no new codemod, no new detection.** TypeScript's
validator was already wired and `change_field_type` already had a handler. The
only new artefacts were this fixture and its test.

31 further TypeScript mechanical cells are blocked on nothing else.
