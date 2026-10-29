---
name: ripple-check
description: Before committing or opening a PR, find code OUTSIDE the diff that the change may break -- unchanged lines that branch on the same value, call an edited function, or describe changed behaviour, plus consumers of changed API contracts. Use after editing code and before /ship, /review, git commit, or opening a pull request, or when the user says "ripple check", "what does this break", or "check impact".
---

# ripple-check

A diff-scoped review cannot see code the diff does not touch. The bug that
ships is often in an UNCHANGED file that depended on what you just changed.
`ripple impact` finds those candidates deterministically -- no model call --
and writes them to `.ripple/impact.md`. Your job is to resolve each one.

## Run it

```bash
ripple impact
```

- Compares the working tree (including uncommitted and untracked files)
  against the merge base of `origin/main` (falls back to `origin/master`,
  `main`, `master`). Pass `--base <ref>` to choose.
- Exit `0` = ran. Exit `2` = could not run: read the message, fix it (wrong
  directory, base does not resolve), and re-run. **Never treat exit 2 as
  "clean".**
- Report: `.ripple/impact.md` (read this) and `.ripple/impact.json`. The
  directory ignores itself; do not edit or commit it.

If `ripple` is not on PATH, it was not installed: say so and stop. Do not
substitute your own search and call it a Ripple check.

## Resolve every HIGH and MEDIUM

For each item, open `path:line` AND the line it cites as `Triggered by`, then
decide one of:

1. **Needs a change** -- the unchanged code now handles a case wrong. Fix it
   in this change. (gstack #2141: the validator started accepting typeless
   PDFs; the unchanged submit helper still sent typeless files as
   `text/plain`. That helper was the fix.)
2. **Unaffected, with a reason** -- one sentence naming WHY, e.g. "branches on
   `user.type`, a different object". A reason, not "looks fine".
3. **Cannot tell** -- say so, and say what would settle it (a test, a type
   check, the owner).

What the kinds mean:

| kind | the rule | what to check |
|---|---|---|
| `same_predicate` | an unchanged line branches on the same property your change branches on | can the NEW values your change lets through reach this branch? |
| `caller` | an unchanged line calls a function whose body you edited | does it rely on the old behaviour (return value, side effect, what it accepts)? |
| `stale_message` | unchanged user-facing text near changed logic shares its words | does the sentence still describe what the code now does? |
| contract consumer | a changed API spec removed/renamed/retyped something this file references | update the reference, or the spec change is breaking |

LOW items: skim them. Fix any stale message that is now wrong; ignore the
rest without comment unless one is clearly real.

## Then

- Re-run `ripple impact` after your fixes. Your fixes are part of the change
  now, so the candidates can shift.
- Tell the user, briefly: how many candidates, which you changed and why,
  which you judged unaffected (one line each), and which you could not decide.

## Never

- Never say "nothing breaks" or "Ripple verified this". The report says what
  was searched. "No candidates found in N files" is the strongest claim
  available.
- Never skip a HIGH because the code "wasn't part of the task". That is
  exactly the class of bug this exists for.
- Consumers in OTHER repositories are not searched. If the report lists a
  contract change, say that plainly.
