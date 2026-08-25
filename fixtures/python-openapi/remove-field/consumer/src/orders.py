"""Unrelated to the breaking change, and deliberately free of any mention of the
removed field. If Ripple touches this file the fix is not minimal and the PR is not
reviewable, so the test byte-compares it.
"""

from src.models import User


def order_label(user: User, order_id: str) -> str:
    return f"{order_id} for {user.email}"
