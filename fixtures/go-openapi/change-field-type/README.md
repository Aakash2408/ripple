# go × openapi × change_field_type

The **fifth** production-ready cell, and the first for the Go validator.

## Why this closes a specific gap

Go was the last wired validator with **nothing proven**. Wiring it took `validate_ok`
from 126 cells to 189, and not one of those 63 had been shown to work end to end. A
wired validator with no cell is a claim about a toolchain, not about the product.

## Why not remove_field

Measured before this fixture was written:

```
_remove_field_go(usage_file,  "PhoneNumber")  ->  UNCHANGED
_remove_field_go(struct_file, "PhoneNumber")  ->  field removed
```

For a removal the declaration is regenerated correct and the **usages** are stale, so
the one file the Go handler can edit is the one file that does not need editing.
`go/openapi/remove_field` is therefore not production-ready and a fixture for it
would fail at the build step — correctly.

Only `change_field_type` and `remove_field` are detectable mechanical ops for
`go/openapi`, so `change_field_type` is the only viable Go cell today.

## The hazard that is sharper in Go

`native_type("go", "integer")` is **`int32`**, not `int`. A struct declaring `int`
does not match, and the codemod correctly annotates instead of editing:

> PARTIAL: marked 1 site(s) with RIPPLE-ACTION-REQUIRED … Ripple did NOT transform
> the code: it lacks the information to do so correctly, and guessing would be worse
> than flagging.

That is the right refusal, but it means the cell only works when the declared type
matches. The struct declares the sized type and the spec carries `format: int32`, so
the two agree — which is also what OpenAPI codegen actually produces.

The test asserts `RIPPLE-ACTION-REQUIRED` is **absent** from the output, so the
annotate path cannot pass by accident.

## Third language, same comment hazard

The first draft of this fixture named the type four times in the package doc comment
and got *"5 type annotation(s) updated"* for a one-field change. TypeScript and
Python both did this too. `_change_type_go` is field-blind and does not respect
comments, so the prose now describes the type rather than naming it, and the test
asserts exactly one line differs.

## Go is the strictest of the three validators

Unused imports are compile **errors** here, not warnings. This cell does not trip it
— the retype keeps `strings` in use — but a `remove_field` cell would have to drop
the now-unused import or fail the build. That is the validator earning its keep.

## What it proves

Measured 2026-08-23 by `test_e2e_go_openapi_change_field_type` against the real
toolchain — docker, `golang:1.21-alpine`, `typecheck_exit: 0`:

```
before fix   INVALID, exactly 2 errors, both in contact.go
after fix    VALID
edit         models/user.go only, ONE line
untouched    contact.go, orders.go, go.mod (byte-compared)
```

The module is stdlib-only, so `go mod download` is a no-op and no network is needed —
which matters because a corporate network that intercepts `proxy.golang.org` TLS
would otherwise make this `UNABLE_TO_VALIDATE`. `GOMODCACHE` still lives inside the
mounted volume so a fixture with real dependencies would work the same way; `GOCACHE`
goes to `/tmp` because the check container mounts the source read-only.

## Where the matrix stands

```
proven cells   5      typescript/openapi/{remove_field, change_field_type}
                      python/openapi/{change_field_type, remove_field}
                      go/openapi/change_field_type
validators     3 of 3 wired, 3 of 3 now with at least one proven cell
```

Cost profile: cell 2 needed only a fixture, cell 3 a validator, cell 4 a codemod,
cell 5 only a fixture again. The expensive part of this one was discovering that
Go's `remove_field` handler does not work.
