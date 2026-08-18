"""Which platform surfaces are live. One predicate, consulted by all of them.

WHY
For the 30-day push, GitHub is Ripple. Everything else is experimental. But
"experimental" written in a README is a label, and these routes were LIVE: opening
real merge requests, and bypassing both the routing decision and the outcome funnel,
so a breaking change on GitLab or Bitbucket could still terminate in silence.

WHY ALL ELEVEN ROUTES AND NOT JUST THE TWO WEBHOOKS
Disabling only /webhook/gitlab and /webhook/bitbucket would have introduced a NEW
silent failure: a user could still complete /auth/gitlab, see /auth/gitlab/status
report a connection, register via /setup/gitlab/register -- and then nothing would
ever happen, with nothing saying why. A half-disabled platform is worse than a live
one, because the product appears to be working. So the whole surface is gated from
here: webhooks, OAuth start and callback, status, and setup.

DEFAULT IS OFF, AND RE-ENABLING IS EXPLICIT
The asymmetry is deliberate. A flag defaulting to ON that someone must remember to
turn off is how a temporary decision becomes permanent. Set
RIPPLE_ENABLE_EXPERIMENTAL_PLATFORMS=1 to bring them back, which is visible in the
deploy config rather than implicit in the code.

This is not a judgement that the adapters are bad work. GitLab, Bitbucket,
Phabricator, Gerrit and CRUX all function. They are switched off because seven
integrations that open unvalidated PRs are worth less than one that opens a
validated PR, and attention is the scarce resource for the next 30 days.
"""
from __future__ import annotations

import os

from fastapi.responses import JSONResponse

#: Platforms switched off for the duration of the push, and why the route exists
#: at all rather than being deleted: the adapters are working code we intend to
#: return to, and deleting them would lose the OAuth/webhook wiring.
EXPERIMENTAL_PLATFORMS = ("gitlab", "bitbucket")

_ENV_FLAG = "RIPPLE_ENABLE_EXPERIMENTAL_PLATFORMS"


def experimental_enabled() -> bool:
    """False unless explicitly enabled. Read per-request on purpose.

    Not cached at import: an operator flipping the env var and restarting should not
    have to reason about whether some module captured the old value, and this is not
    a hot path -- these routes are meant to return immediately.
    """
    return os.environ.get(_ENV_FLAG, "") == "1"


def experimental_disabled(platform: str, surface: str = "") -> JSONResponse:
    """The stated refusal. 501, with the reason and the way to reverse it.

    501 rather than 404 because the route exists and the capability exists; it is
    deliberately not implemented *right now*. 404 would imply the integration was
    never built, which is untrue and would send an operator hunting for a
    deployment problem.
    """
    where = f" ({surface})" if surface else ""
    return JSONResponse(
        status_code=501,
        content={
            "error": "platform_disabled",
            "platform": platform,
            "surface": surface or None,
            "reason": (
                f"The {platform} integration{where} is switched off. For the "
                f"current push, GitHub is the only production surface: Ripple can "
                f"detect breaking changes across 10 contract types but has proven "
                f"zero fix-generating combinations end to end, and the "
                f"{platform} path additionally bypasses the routing decision and "
                f"the outcome funnel -- so a breaking change there could terminate "
                f"in silence rather than in a stated outcome."
            ),
            "what_still_works": "GitHub App (/webhook) and read-only analysis (/dry-run)",
            "to_re_enable": f"set {_ENV_FLAG}=1",
        },
    )
