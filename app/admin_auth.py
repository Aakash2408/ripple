"""
ripple/app/admin_auth.py

ONE admin check, shared by every route that can emit a repository name.

WHY THIS MODULE EXISTS
----------------------
/stats?detail=1 already guarded names behind RIPPLE_ADMIN_TOKEN, and said why
in its own docstring:

    "an endpoint that let any visitor enumerate which private-ish repos other
     people had connected would be a worse problem than the blindness it fixes"

Meanwhile /dashboard rendered `activity.monitored_repos()` into HTML and
/logs/recent returned `_activity_log[-30:]` as JSON -- the same names, the same
event payloads, to anyone who asked, with no check at all. Verified against
production: /logs/recent returned Aakash2408/auth-service, /billing-api and
/notifications-svc unauthenticated.

So the guarantee was not missing, it was being UNDONE by a sibling route. One
path enforcing a rule another path ignores is worse than no rule, because the
enforced one reads as proof the question was handled. The policy now lives in
one function instead of being restated per route, so a new route either calls it
or is caught by the gate in tests/test_regression.py that enumerates the app's
own routes and asserts no unauthenticated one can emit a name.

FAILS CLOSED
------------
An unset RIPPLE_ADMIN_TOKEN refuses (503); it must never mean "no
authentication required". That is the same fail-open shape as defaulting to a
paid model when no provider is configured, and this codebase has now been bitten
by it twice.
"""
from __future__ import annotations

import hmac
import os
from typing import Optional

# Accepted in three places, because one guard serves both machines and a
# browser. The header is canonical (it predates this module, on /stats);
# the cookie is what keeps a browser working after the first visit; the query
# parameter is the only way a human can authenticate a bare page load at all.
HEADER = "X-Ripple-Admin-Token"
COOKIE = "ripple_admin"
QUERY = "token"


class AdminAuthError(Exception):
    """Refusal, carrying the HTTP status the caller should surface.

    Not an HTTPException: app/dashboard.py returns HTML, and raising an
    HTTPException there would answer a browser with a JSON error body. The
    caller decides the content type; this module decides only yes/no and why.
    """

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def configured() -> bool:
    """Is an admin token set at all?

    Read per call rather than captured at import, matching
    experimental.experimental_enabled(): an operator who sets the variable and
    restarts should not have to reason about which module cached the old value.
    """
    return bool(os.environ.get("RIPPLE_ADMIN_TOKEN", ""))


def supplied_token(request) -> str:
    """The token the caller presented, from header, cookie or query string.

    getattr with defaults, not attribute access: callers pass a real FastAPI
    Request in production (which has all three) but a minimal stand-in in
    tests, and a guard that raises AttributeError on a request lacking cookies
    would be a crash, not a refusal. The three channels are each exercised
    independently by a gate in tests/test_regression.py, so these defaults
    cannot quietly become "cookie auth never worked".
    """
    headers = getattr(request, "headers", {}) or {}
    cookies = getattr(request, "cookies", {}) or {}
    params = getattr(request, "query_params", {}) or {}
    return (
        headers.get(HEADER, "")
        or cookies.get(COOKIE, "")
        or params.get(QUERY, "")
    )


def check(request) -> None:
    """Raise AdminAuthError unless the caller is an authenticated admin.

    Order matters: "not configured" is a 503 about the SERVER, while a wrong
    token is a 401 about the CALLER. Collapsing both into 401 would tell an
    operator who simply forgot the variable that their token was wrong.
    """
    expected = os.environ.get("RIPPLE_ADMIN_TOKEN", "")
    if not expected:
        raise AdminAuthError(
            503,
            "this route can expose repository names and requires "
            "RIPPLE_ADMIN_TOKEN to be configured; refusing to serve them "
            "unauthenticated",
        )
    got = supplied_token(request)
    # compare_digest rather than ==, so a wrong token cannot be recovered a
    # byte at a time from response timing.
    if not got or not hmac.compare_digest(got, expected):
        raise AdminAuthError(401, "bad or missing admin token")


def cookie_kwargs(token: str) -> dict:
    """Arguments for set_cookie, so the token need not stay in the URL.

    A query parameter is the only way a human can authenticate a bare page
    load, but it then sits in browser history and in any Referer the page
    sends. Exchanging it for an HttpOnly cookie on first use bounds that to a
    single history entry: HttpOnly keeps it away from page scripts, Secure
    keeps it off plaintext, and SameSite=Lax stops a third-party site
    replaying it cross-origin.
    """
    return {
        "key": COOKIE,
        "value": token,
        "httponly": True,
        "secure": True,
        "samesite": "lax",
        "max_age": 60 * 60 * 24 * 30,
        "path": "/",
    }


def first_party_token(request) -> Optional[str]:
    """A token presented in the QUERY STRING only -- the one worth exchanging.

    A header or cookie caller already has a durable credential and needs no
    cookie set.
    """
    params = getattr(request, "query_params", {}) or {}
    return params.get(QUERY, "") or None
