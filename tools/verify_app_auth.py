#!/usr/bin/env python3
"""Verify GitHub App authentication end to end.

Checks each link in the chain separately so a failure tells you WHICH part
is wrong, instead of the whole thing collapsing into "no consumers found":

  1. env config present            (GITHUB_APP_ID + private key)
  2. private key loads             (PEM parses as RSA)
  3. App JWT is accepted           (GET /app  -> proves id+key MATCH)
  4. installations are visible     (GET /app/installations)
  5. installation token exchange   (POST .../access_tokens)
  6. repo scope is authoritative   (GET /installation/repositories)

Step 3 is the one that catches the most common mistake: a valid key paired
with the wrong App ID. Both look fine in isolation.

Usage
    # locally, before configuring Railway
    GITHUB_APP_ID=123456 GITHUB_APP_PRIVATE_KEY_PATH=~/ripple.pem \\
        python3.12 tools/verify_app_auth.py

    # against the deployed service
    python3.12 tools/verify_app_auth.py --remote
"""
from __future__ import annotations

import json
import os
import ssl
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

SSL_CTX = ssl.create_default_context()
RAILWAY = "https://ripple-production-be7f.up.railway.app"


def _api(path: str, bearer: str, token_type: str = "Bearer") -> tuple[int, dict]:
    req = Request(
        "https://api.github.com" + path,
        headers={
            "Authorization": f"{token_type} {bearer}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ripple-verify",
        },
    )
    try:
        with urlopen(req, timeout=20, context=SSL_CTX) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else {})
    except HTTPError as e:
        body = e.read().decode()[:300] if hasattr(e, "read") else ""
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"message": body}


def fail(step: str, detail: str, fix: str) -> int:
    print(f"\n  ❌ FAILED at step: {step}")
    print(f"     {detail}")
    print(f"\n  How to fix:\n     {fix}")
    return 1


def check_remote() -> int:
    """Ask the deployed service whether App auth is live."""
    print("=" * 70)
    print("REMOTE CHECK (deployed service)")
    print("=" * 70)
    try:
        with urlopen(RAILWAY + "/health", timeout=20, context=SSL_CTX) as r:
            print(f"  /health         : {r.read().decode()[:80]}")
    except Exception as e:
        return fail("reach service", str(e), "check the Railway deployment is up")

    try:
        with urlopen(RAILWAY + "/logs/recent", timeout=20, context=SSL_CTX) as r:
            logs = json.loads(r.read().decode()).get("logs", [])
    except Exception as e:
        print(f"  /logs/recent    : unavailable ({e})")
        return 0

    scope = [e for e in logs if e.get("action") == "consumer_scope"]
    auth_fail = [e for e in logs if e.get("action") in
                 ("app_auth_failed", "consumer_scope_error")]

    if auth_fail:
        print(f"\n  ⚠️  {len(auth_fail)} App-auth error(s) in the live log:")
        for e in auth_fail[-3:]:
            print(f"      {e.get('err', e)}")
    if scope:
        last = scope[-1]
        print(f"\n  last consumer_scope mode : {last.get('mode')}")
        print(f"  authoritative            : {last.get('authoritative')}")
        if last.get("mode") == "app_installation":
            print("\n  ✅ live service is using App installation scope")
        else:
            print("\n  ⚠️  live service is NOT using App scope yet")
            print("      set GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY in Railway")
    else:
        print("\n  no consumer_scope events yet -- trigger a push to populate")
    return 0


def main(argv: list) -> int:
    if "--remote" in argv:
        return check_remote()

    from app import github_app_auth as gaa

    print("=" * 70)
    print("GITHUB APP AUTH VERIFICATION")
    print("=" * 70)

    # ---- 1. config present
    app_id = gaa.get_app_id()
    key_pem = gaa.get_private_key()
    print(f"\n  1. config")
    print(f"     GITHUB_APP_ID          : {app_id or '(missing)'}")
    print(f"     private key            : "
          f"{'loaded, ' + str(len(key_pem)) + ' chars' if key_pem else '(missing)'}")
    if not app_id or not key_pem:
        return fail(
            "config",
            "GITHUB_APP_ID and/or the private key are not set.",
            "GITHUB_APP_ID=<numeric id from the App settings page>\n"
            "     GITHUB_APP_PRIVATE_KEY_PATH=/path/to/key.pem   (local)\n"
            "     or GITHUB_APP_PRIVATE_KEY=<full PEM contents>  (Railway)",
        )

    # ---- 2. key loads + 3. JWT accepted
    try:
        jwt_token = gaa.build_app_jwt()
        print(f"\n  2. private key           : parsed OK (RSA)")
    except gaa.AppAuthError as e:
        return fail("private key", str(e),
                    "re-download the .pem from the App settings page; it must be\n"
                    "     an RSA PRIVATE KEY block, unencrypted")

    status, app_info = _api("/app", jwt_token)
    print(f"\n  3. GET /app              : HTTP {status}")
    if status != 200:
        return fail(
            "App JWT rejected",
            f"HTTP {status}: {app_info.get('message', '')[:160]}",
            "This almost always means the App ID does not match the private key.\n"
            "     Confirm the numeric 'App ID' on the App's settings page, and that\n"
            "     the .pem was generated for THAT app.",
        )
    print(f"     app slug               : {app_info.get('slug')}")
    print(f"     app name               : {app_info.get('name')}")
    print(f"     owner                  : {app_info.get('owner', {}).get('login')}")
    print(f"     installs               : {app_info.get('installations_count')}")

    # ---- 4. installations
    status, installs = _api("/app/installations", jwt_token)
    print(f"\n  4. GET /app/installations: HTTP {status}")
    if status != 200 or not isinstance(installs, list):
        return fail("list installations",
                    f"HTTP {status}: {str(installs)[:160]}",
                    "ensure the App is installed on at least one account")
    if not installs:
        return fail("no installations",
                    "the App is not installed anywhere",
                    "install it: https://github.com/apps/ripple-api")

    for inst in installs:
        acct = inst.get("account", {}).get("login", "?")
        print(f"     installation {inst.get('id')} -> {acct} "
              f"({inst.get('repository_selection')})")

    # ---- 5 + 6. token exchange and authoritative scope
    ok = True
    for inst in installs:
        inst_id = inst.get("id")
        acct = inst.get("account", {}).get("login", "?")
        print(f"\n  5. token exchange (installation {inst_id}, {acct})")
        try:
            token = gaa.get_installation_token(inst_id)
            print(f"     token minted           : yes (len={len(token)})")
        except gaa.AppAuthError as e:
            print(f"     ❌ {e}")
            ok = False
            continue

        print(f"  6. authoritative repo scope")
        try:
            repos = gaa.list_installation_repositories(inst_id)
            print(f"     repos granted          : {len(repos)}")
            for r in repos[:12]:
                print(f"       - {r}")
            if len(repos) > 12:
                print(f"       ... and {len(repos) - 12} more")
            if not repos:
                print("     ⚠️  zero repos granted -- grant access to the repos"
                      " Ripple should watch")
                ok = False
        except gaa.AppAuthError as e:
            print(f"     ❌ {e}")
            ok = False

    print("\n" + "=" * 70)
    if ok:
        print("  ✅ App auth fully working -- discovery is now authoritative")
        print("     Set the SAME two vars in Railway to enable it in production.")
    else:
        print("  ⚠️  App auth partially working -- see failures above")
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
