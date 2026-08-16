from __future__ import annotations
"""
ripple/app/tls.py

One TLS context builder that keeps certificate verification ON.

WHY THIS EXISTS
---------------
webhook.py previously did this, under a comment calling it an "SSL fix":

    SSL_CTX = ssl.create_default_context()
    SSL_CTX.check_hostname = False
    SSL_CTX.verify_mode = ssl.CERT_NONE

That does not fix TLS, it disables it -- every GitHub API call would accept
any certificate from any host. It was tolerable-ish when the only credential
in flight was a single personal token. It is not tolerable now: those same
calls carry GitHub App INSTALLATION tokens, which grant write access to a
customer's repositories. An attacker able to intercept the connection could
capture a token scoped to someone else's code.

The actual problem being worked around is a missing CA bundle, not a broken
certificate. Some hosts (including this dev desktop's bundled interpreter)
default to a CA file that lacks the public roots needed for Let's Encrypt
hosts such as *.up.railway.app, while `curl` succeeds because it reads the
system bundle. So: find a usable bundle instead of turning verification off.

Resolution order:
  1. RIPPLE_CA_BUNDLE           explicit override
  2. certifi                    the standard public root bundle
  3. common system bundle paths
  4. Python's defaults

RIPPLE_INSECURE_SSL=1 still exists as an emergency escape hatch, but it is
opt-in, and callers log loudly when it is active so an insecure deployment
cannot be silent.
"""

import os
import ssl

_SYSTEM_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",   # debian/ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",     # rhel/amazon linux
    "/etc/ssl/certs/ca-bundle.crt",
    "/etc/ssl/cert.pem",                    # alpine/macos
)


def insecure_requested() -> bool:
    return os.environ.get("RIPPLE_INSECURE_SSL", "").strip().lower() in (
        "1", "true", "yes",
    )


def resolve_ca_bundle() -> str:
    """Return a path to a usable CA bundle, or '' to use Python defaults."""
    explicit = os.environ.get("RIPPLE_CA_BUNDLE", "").strip()
    if explicit and os.path.exists(explicit):
        return explicit

    try:
        import certifi
        path = certifi.where()
        if path and os.path.exists(path):
            return path
    except Exception:
        pass

    for path in _SYSTEM_BUNDLES:
        if os.path.exists(path):
            return path

    return ""


def make_ssl_context() -> ssl.SSLContext:
    """Build a verifying SSL context, falling back across CA bundles.

    Verification stays enabled unless RIPPLE_INSECURE_SSL is explicitly set.
    """
    if insecure_requested():
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    bundle = resolve_ca_bundle()
    if bundle:
        try:
            return ssl.create_default_context(cafile=bundle)
        except Exception:
            pass

    return ssl.create_default_context()


def describe() -> dict:
    """Diagnostic summary of the active TLS configuration."""
    return {
        "verification": "DISABLED (RIPPLE_INSECURE_SSL)" if insecure_requested()
        else "enabled",
        "ca_bundle": resolve_ca_bundle() or "(python defaults)",
    }
