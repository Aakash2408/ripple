"""THIS FILE IS THE FIX TARGET. Two stale references, both in shapes app/py_codemod.py
can remove with no behavioural change:

    f-string interpolation   the whole `{...}` goes; a display string loses a value
                             that no longer exists upstream
    dict-literal entry       the whole entry goes; sending a field the contract
                             deleted is meaningless

Every function is annotated. That is load-bearing rather than stylistic: the Python
runner passes --disallow-untyped-defs, and an unannotated def would make the verdict
UNABLE_TO_VALIDATE instead of VALID -- mypy would not be able to see the type of
`user`, and the attribute errors below would become invisible.
"""

from src.models import User


def format_contact(user: User) -> str:
    return f"{user.full_name} <{user.email}> {user.phone_number}"


def to_crm_payload(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "phone": user.phone_number,
    }
