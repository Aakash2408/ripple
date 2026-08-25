"""Both functions are ALREADY CORRECT for the new contract -- a phone number is a
string, so stripping it and testing its prefix is exactly right.

They fail mypy today only because models.py still annotates the field as `int`. Fix
the declaration and both errors disappear without either function being touched,
which is the claim this cell exists to prove.

Every function here is annotated. That is load-bearing, not stylistic: the runner
passes --disallow-untyped-defs, and an unannotated def would make the whole result
UNABLE_TO_VALIDATE rather than VALID -- because mypy cannot see the types it would
need, and a pass it could not have earned is worth nothing.
"""

from src.models import User


def normalise_phone(user: User) -> str:
    return user.phone_number.strip()


def is_international(user: User) -> bool:
    return user.phone_number.startswith("+")
