# typescript x openapi x remove_field -- the JUDGMENT case

Deliberately not built in Stage 4. Recorded so it is not mistaken for an oversight.

The sibling fixture (`../remove-field/`) covers references that are *mechanically*
removable: a display placeholder and an outbound payload key. Dropping them cannot
change behaviour, because the field no longer exists upstream.

This case is different:

```ts
export function notifyTarget(user: User): string {
  const phone = user.phoneNumber;
  return phone ? `sms:${phone}` : `mailto:${user.email}`;
}
```

Here the code *branches* on the value. Removing the reference forces a decision no
transformation can make: does this function now always email, always fail, or take
a new parameter? That is a `JUDGMENT` operation wearing a `remove_field` label, and
the honest terminal state is `HUMAN_ACTION_REQUIRED` -- not a fix.

Building it belongs with the work that makes `PARTIAL` a first-class outcome, not
with the push to make one cell `AUTO`. Keeping it out of `../remove-field/consumer`
is deliberate: sharing a tsconfig would make `tsc` fail there forever, so a
perfectly correct mechanical fix could never demonstrate a passing compile.
