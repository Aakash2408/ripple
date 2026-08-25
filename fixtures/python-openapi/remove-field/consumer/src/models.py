"""Generated from ../../spec/user.after.yaml -- do not edit by hand.

`phone_number` is absent here because the spec removed it. That is what turns every
remaining reference in checkout.py into an attribute error, which is the point of
the fixture: a consumer whose types were also stale would prove nothing.

NOTE THE INVERSION versus the change-field-type fixture next door. There, the
declaration was stale and the usages were correct, so Ripple edited this file. Here
the declaration is already correct -- regenerated from the new spec -- and the
USAGES are stale, so Ripple edits checkout.py and this file must come out
byte-identical.
"""

from dataclasses import dataclass


@dataclass
class User:
    id: str
    email: str
    full_name: str
