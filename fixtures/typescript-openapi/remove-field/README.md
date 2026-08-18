# typescript × openapi × remove_field — the golden fixture

A consumer that **genuinely does not compile** until the breaking change is
propagated. Verified, not asserted:

```
tsc --noEmit          ->  2 errors, exit 2      (broken state)
tsc --noEmit          ->  exit 0                (after a correct fix)
```

`src/types.ts` is already regenerated from `spec/user.after.yaml`, which is what
makes the consumer broken rather than merely stale. A fixture whose types still
contain the removed field would compile fine and prove nothing.

`expected.json` is the contract: which file must be found, which two references must
be transformed, that `tsc` must pass afterwards, and that the PR must touch
`src/checkout.ts` and nothing else. `src/orders.ts` exists purely so "touched only
what it needed to" is a falsifiable claim rather than a hope.

## What this fixture measured on the day it was written

Ripple's TypeScript `remove_field` handler produces **code that does not parse**:

```diff
-    phone: user.phoneNumber,
-  };
+    phone: user.};
```

```
tsc: src/checkout.ts(19,17): error TS1003: Identifier expected
```

…while reporting *"Removed all references to field 'phoneNumber' (1 lines affected)"*.

Two distinct defects, both recorded in `expected.json` under `measured`:

| reference | outcome |
|---|---|
| template literal — `` `...${user.phoneNumber}` `` | **not handled.** The access pattern `^\s*\S*\.field\b.*$` only matches a line whose *first* token is the access |
| object literal — `phone: user.phoneNumber,` | **handled incorrectly.** The destructuring cleanup `\b{field}\s*,\s*` strips `phoneNumber,` wherever it appears, including as the tail of a member expression |

The explanation claims success in both cases, so the log cannot tell a no-op from a
corruption.

**Why this has not burned anyone:** `AUTO` is unreachable and every PR is labelled
*"Proposed Fix (human review required)"* with the registry's blockers listed, so a
human reads the diff before merging. The REVIEW trust boundary is doing precisely
the job it was built for — which is the argument for not treating "human review
required" as a defect to eliminate.

**Implication for Stage 6:** the golden cell needs a real syntactic transformation,
not a wider regex. Removing a member expression from an interpolation or an object
literal requires knowing the surrounding syntax. Validation (Stage 5) is necessary
but **not sufficient** — it would correctly reject this output, leaving the cell
`BLOCKED` rather than `AUTO`.

## Running it

Requires a node that can execute. On a glibc 2.26 host the system node fails with
`GLIBC_2.27 not found`; `~/.nvm/versions/node/v16.20.2/bin/node` works.

```bash
cd consumer
npm install --ignore-scripts --no-audit --no-fund
./node_modules/.bin/tsc --noEmit          # expect: 2 errors
```

`--ignore-scripts` from the very first install, deliberately: validating a customer
repo means installing untrusted dependencies, and `postinstall` is the obvious
vector. Better to start with the habit than to retrofit it.
