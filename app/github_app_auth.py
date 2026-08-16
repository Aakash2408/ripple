from __future__ import annotations
"""
ripple/app/github_app_auth.py

Real GitHub App authentication: App JWT -> installation access token.

WHY THIS EXISTS
---------------
`_get_token()` previously accepted an `installation_id` and threw it away,
returning a single long-lived personal access token from the environment:

    def _get_token(installation_id=None):
        # For now: use personal token.
        return os.environ.get("GITHUB_TOKEN", "")

That one shortcut cascaded into most of the discovery layer's problems:

  * A PAT can see EVERY repo the human owns, so consumer discovery had to
    enumerate `/users/{owner}/repos` -- 75 repos for one demo account.
  * That list included Ripple's own website, whose landing-page copy
    mentions the field names, so Ripple opened a PR against itself.
  * Suppressing that required three heuristics: a self-repo blocklist, a
    fork filter, and a 20-repo cap. The cap silently drops consumers for
    anyone with more repos than that -- the same class of silent false
    negative we removed from the parser.
  * One shared PAT means the service is structurally SINGLE-TENANT and
    holds a long-lived credential that must be rotated by hand.

An installation token fixes all of it at the source: GitHub tells us
exactly which repositories the customer granted access to, the token is
scoped to that installation, it expires in an hour, and it is minted on
demand per installation. The heuristics become unnecessary rather than
merely better tuned.

CONFIGURATION
    GITHUB_APP_ID           the App's numeric id
    GITHUB_APP_PRIVATE_KEY  PEM contents (supports \\n-escaped single-line)
      or
    GITHUB_APP_PRIVATE_KEY_PATH  path to the .pem file

If neither is configured, callers fall back to GITHUB_TOKEN so local
development and single-user self-hosting keep working.
"""

import base64
import json
import os
import ssl
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .tls import make_ssl_context

# Verification stays ON: these calls carry the App JWT and mint installation
# tokens with write access to customer repositories.
SSL_CTX = make_ssl_context()

_API = "https://api.github.com"

# installation_id -> (token, expires_at_epoch)
_token_cache: dict = {}

# Refresh this many seconds before actual expiry
_REFRESH_MARGIN = 300


class AppAuthError(Exception):
    """Raised when App auth is configured but fails.

    Deliberately NOT swallowed: a misconfigured App must be loud, not
    silently degrade to 'no consumers found'.
    """


# ----------------------------------------------------------------- config
def get_app_id() -> str:
    return os.environ.get("GITHUB_APP_ID", "").strip()


def get_private_key() -> str:
    """Return the App private key PEM, or '' if not configured."""
    path = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH", "").strip()
    if path and os.path.exists(path):
        with open(path) as f:
            return f.read()

    raw = os.environ.get("GITHUB_APP_PRIVATE_KEY", "")
    if not raw:
        return ""
    # Env vars commonly carry the PEM with literal \n sequences
    if "\\n" in raw and "-----BEGIN" in raw:
        raw = raw.replace("\\n", "\n")
    return raw.strip()


def is_app_configured() -> bool:
    """True when both App id and private key are present."""
    return bool(get_app_id() and get_private_key())


# -------------------------------------------------------------------- JWT
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def build_app_jwt(app_id: str = "", private_key_pem: str = "") -> str:
    """Build a short-lived RS256 JWT that authenticates AS the App.

    Signed with `cryptography` directly so PyJWT is not a dependency.
    GitHub caps App JWT lifetime at 10 minutes.
    """
    app_id = app_id or get_app_id()
    private_key_pem = private_key_pem or get_private_key()
    if not app_id or not private_key_pem:
        raise AppAuthError("GITHUB_APP_ID / GITHUB_APP_PRIVATE_KEY not configured")

    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError as e:  # pragma: no cover
        raise AppAuthError(f"cryptography is required for App auth: {e}")

    try:
        key = serialization.load_pem_private_key(
            private_key_pem.encode(), password=None
        )
    except Exception as e:
        raise AppAuthError(f"could not load GITHUB_APP_PRIVATE_KEY: {e}")

    if not isinstance(key, rsa.RSAPrivateKey):
        raise AppAuthError("GitHub App private key must be an RSA key")

    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iat": now - 60,        # backdate to tolerate clock skew
        "exp": now + 540,       # 9 minutes, under GitHub's 10 minute cap
        "iss": app_id,
    }

    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    ).encode()

    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return signing_input.decode() + "." + _b64url(signature)


# ------------------------------------------------------------------ calls
def _api(method: str, path: str, bearer: str, token_type: str = "Bearer") -> dict:
    req = Request(
        _API + path,
        headers={
            "Authorization": f"{token_type} {bearer}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ripple-app",
        },
        method=method,
    )
    try:
        with urlopen(req, timeout=15, context=SSL_CTX) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except HTTPError as e:
        body = e.read().decode()[:200] if hasattr(e, "read") else ""
        return {"error": e.code, "message": body}


def get_installation_token(installation_id) -> str:
    """Mint (or reuse) an installation access token.

    Tokens live one hour; cached per installation and refreshed with a
    5 minute margin. Raises AppAuthError on failure rather than returning
    '' -- a silent empty token would surface later as 'no consumers found'
    and be indistinguishable from a healthy no-op.
    """
    if not installation_id:
        raise AppAuthError("installation_id is required for App auth")

    key = str(installation_id)
    cached = _token_cache.get(key)
    if cached and cached[1] - _REFRESH_MARGIN > time.time():
        return cached[0]

    jwt_token = build_app_jwt()
    result = _api(
        "POST", f"/app/installations/{installation_id}/access_tokens", jwt_token
    )
    if "error" in result:
        raise AppAuthError(
            f"installation token exchange failed for {installation_id}: "
            f"{result.get('error')} {result.get('message', '')[:120]}"
        )

    token = result.get("token", "")
    if not token:
        raise AppAuthError(f"no token in exchange response: {str(result)[:120]}")

    # expires_at looks like 2026-08-16T07:00:00Z
    expires_at = time.time() + 3600
    raw_exp = result.get("expires_at", "")
    if raw_exp:
        try:
            from datetime import datetime, timezone
            parsed = datetime.strptime(raw_exp, "%Y-%m-%dT%H:%M:%SZ")
            expires_at = parsed.replace(tzinfo=timezone.utc).timestamp()
        except (ValueError, TypeError):
            pass

    _token_cache[key] = (token, expires_at)
    return token


def list_installation_repositories(installation_id) -> list:
    """The AUTHORITATIVE list of repos this installation can access.

    This is what replaces enumerating every repo the user owns. GitHub
    returns exactly the repositories the customer selected when installing,
    so no cap, fork filter, or self-repo blocklist is required.
    """
    token = get_installation_token(installation_id)

    repos = []
    page = 1
    while page <= 20:  # 20 * 100 = 2000 repos; guard against runaway paging
        result = _api(
            "GET",
            f"/installation/repositories?per_page=100&page={page}",
            token,
            token_type="token",
        )
        if "error" in result:
            raise AppAuthError(
                f"listing installation repositories failed: "
                f"{result.get('error')} {result.get('message', '')[:120]}"
            )
        batch = result.get("repositories", [])
        if not batch:
            break
        for r in batch:
            if not r.get("archived"):
                repos.append(r["full_name"])
        if len(batch) < 100:
            break
        page += 1

    return repos


def invalidate(installation_id=None) -> None:
    """Drop cached tokens (all, or one installation)."""
    if installation_id is None:
        _token_cache.clear()
    else:
        _token_cache.pop(str(installation_id), None)
