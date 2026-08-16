#!/usr/bin/env python3
"""Local end-to-end harness for Ripple's fix -> PR pipeline.

WHY THIS EXISTS
---------------
Debugging this pipeline via Railway costs a full deploy cycle (push + ~45s
build + trigger + poll logs) to surface ONE bug at a time. This runs the
REAL functions from app/webhook.py locally against the REAL GitHub API so
every remaining bug in the fix->PR path surfaces in a single pass.

Fidelity matters: we import the actual webhook module (stubbing only the
web framework, which is irrelevant to this code path) rather than
reimplementing the logic, so what passes here is what runs in production.

USAGE
    python3 tools/local_e2e.py            # read-only: search + fix, NO writes
    python3 tools/local_e2e.py --create-pr  # also opens real PRs on GitHub

--create-pr MUTATES GitHub (creates branches + pull requests).
"""
from __future__ import annotations

import os
import sys
import types


# ---------------------------------------------------------------- token
def load_token() -> str:
    """Read the GitHub token without ever printing it."""
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    if tok:
        return tok
    path = os.path.expanduser("~/.git-credentials")
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if "github.com" in line and "@" in line and ":" in line:
                    # https://user:TOKEN@github.com
                    creds = line.split("//", 1)[1].split("@", 1)[0]
                    if ":" in creds:
                        return creds.split(":", 1)[1]
    except OSError:
        pass
    return ""


# ------------------------------------------------------- fastapi stub
def install_fastapi_stub() -> None:
    """Minimal fastapi/pydantic stand-ins.

    webhook.py only needs these at import time (decorators, exception
    class). None of it participates in the fix->PR logic under test.
    """
    if "fastapi" in sys.modules:
        return

    class HTTPException(Exception):
        def __init__(self, status_code=400, detail=""):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class _Router:
        def __call__(self, *a, **k):
            def deco(fn):
                return fn
            return deco

    class FastAPI:
        def __init__(self, *a, **k):
            self.state = types.SimpleNamespace()

        def get(self, *a, **k):
            return _Router()(*a, **k)

        def post(self, *a, **k):
            return _Router()(*a, **k)

        def put(self, *a, **k):
            return _Router()(*a, **k)

        def delete(self, *a, **k):
            return _Router()(*a, **k)

        def middleware(self, *a, **k):
            return _Router()(*a, **k)

        def on_event(self, *a, **k):
            return _Router()(*a, **k)

        def add_middleware(self, *a, **k):
            return None

        def mount(self, *a, **k):
            return None

        def include_router(self, *a, **k):
            return None

        def exception_handler(self, *a, **k):
            return _Router()(*a, **k)

    class APIRouter:
        def __init__(self, *a, **k):
            self.routes = []

        def get(self, *a, **k):
            return _Router()(*a, **k)

        def post(self, *a, **k):
            return _Router()(*a, **k)

        def put(self, *a, **k):
            return _Router()(*a, **k)

        def delete(self, *a, **k):
            return _Router()(*a, **k)

        def include_router(self, *a, **k):
            return None

    fastapi = types.ModuleType("fastapi")
    fastapi.FastAPI = FastAPI
    fastapi.APIRouter = APIRouter
    fastapi.HTTPException = HTTPException
    fastapi.Request = object
    fastapi.Response = object
    fastapi.BackgroundTasks = object
    fastapi.Header = lambda *a, **k: None
    fastapi.Query = lambda *a, **k: None
    fastapi.Body = lambda *a, **k: None
    fastapi.Depends = lambda *a, **k: None
    sys.modules["fastapi"] = fastapi

    responses = types.ModuleType("fastapi.responses")

    class _Resp:
        def __init__(self, *a, **k):
            pass

    responses.HTMLResponse = _Resp
    responses.JSONResponse = _Resp
    responses.PlainTextResponse = _Resp
    responses.RedirectResponse = _Resp
    responses.StreamingResponse = _Resp
    responses.FileResponse = _Resp
    sys.modules["fastapi.responses"] = responses

    staticfiles = types.ModuleType("fastapi.staticfiles")
    staticfiles.StaticFiles = _Resp
    sys.modules["fastapi.staticfiles"] = staticfiles

    middleware = types.ModuleType("fastapi.middleware")
    sys.modules["fastapi.middleware"] = middleware
    cors = types.ModuleType("fastapi.middleware.cors")
    cors.CORSMiddleware = _Resp
    sys.modules["fastapi.middleware.cors"] = cors

    if "pydantic" not in sys.modules:
        pydantic = types.ModuleType("pydantic")

        class BaseModel:
            def __init__(self, **kw):
                for k, v in kw.items():
                    setattr(self, k, v)

        pydantic.BaseModel = BaseModel
        pydantic.Field = lambda *a, **k: None
        sys.modules["pydantic"] = pydantic

    if "uvicorn" not in sys.modules:
        uvicorn = types.ModuleType("uvicorn")
        uvicorn.run = lambda *a, **k: None
        sys.modules["uvicorn"] = uvicorn


# ------------------------------------------------------------ helpers
def hr(title: str) -> None:
    print(f"\n{'=' * 66}\n{title}\n{'=' * 66}")


def main() -> int:
    create_pr = "--create-pr" in sys.argv

    token = load_token()
    if not token:
        print("❌ No GitHub token found (env GITHUB_TOKEN or ~/.git-credentials)")
        return 2
    os.environ["GITHUB_TOKEN"] = token
    print(f"✅ token loaded (len={len(token)}, prefix={token[:4]}...)")

    install_fastapi_stub()
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    try:
        from app import webhook as wh
    except Exception as e:
        print(f"❌ could not import app.webhook: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return 2
    print("✅ real app.webhook imported (framework stubbed only)")

    SOURCE_REPO = "Aakash2408/user-proto"
    SPEC = "user.proto"

    # ---------------------------------------------------------- 1. diff
    hr("1. DIFF -- detect the breaking change")
    old_proto = """syntax = "proto3";
package user.v1;
message User {
  string id = 1;
  string name = 2;
  string email = 3;
  string phone_number = 4;
}
"""
    new_proto = """syntax = "proto3";
package user.v1;
message User {
  string id = 1;
  string name = 2;
  string email = 3;
}
"""
    changes = wh.diff_proto(old_proto, new_proto, file_path=SPEC)
    if not changes:
        print("❌ no breaking changes detected -- diff engine problem")
        return 1
    for c in changes:
        print(f"  type={c.change_type!r} field={c.field_name!r} "
              f"path={getattr(c, 'path', None)!r} method={getattr(c, 'method', None)!r}")
    change = changes[0]

    # ------------------------------------------------ 2. consumer repos
    hr("2. CONSUMER REPOS")
    repos = wh._find_consumer_repos(SOURCE_REPO, token)
    print(f"  {len(repos)} repos; first 6: {repos[:6]}")
    targets = [r for r in repos
               if r.split("/")[-1] in ("auth-service", "billing-api", "notifications-svc")]
    print(f"  demo repos present: {targets}")
    if len(targets) != 3:
        print("  ⚠️  expected all 3 demo repos in the candidate list")

    # ----------------------------------------------- 3. consumer search
    hr("3. CONSUMER SEARCH (real GitHub code search)")
    found = {}
    for repo in targets:
        files = wh._search_repo_for_consumers(repo, change, token, exclude_path=SPEC)
        found[repo] = files
        print(f"  {repo}: {len(files)} file(s) -> {[f for f, _ in files]}")
    total_files = sum(len(v) for v in found.values())
    if total_files == 0:
        print("\n❌ BLOCKER: search found 0 consumer files. Fix/PR cannot proceed.")
        return 1
    print(f"\n  ✅ {total_files} consumer file(s) found")

    # ------------------------------------------------ 4. fix generation
    hr("4. FIX GENERATION (LLM-free template/RAG)")
    fixes = []
    for repo, files in found.items():
        for path, content in files:
            consumer = wh.ConsumerMatch(
                file_path=path, line_number=0, code_snippet="",
                confidence="high", match_reason="local harness",
                language=wh._detect_lang(path),
            )
            fixed, explanation = wh._generate_fix_with_rag_fallback(
                content, consumer, change, "Aakash2408"
            )
            changed = fixed != content
            print(f"  {repo}/{path}")
            print(f"    lang={wh._detect_lang(path)} changed={changed} src={explanation[:52]!r}")
            if changed:
                removed = [l for l in content.splitlines()
                           if l not in fixed.splitlines() and l.strip()]
                for line in removed[:3]:
                    print(f"      - {line.strip()[:70]}")
                fixes.append((repo, path, fixed))
            else:
                print("      ⚠️  no change produced -- template did not match")
    if not fixes:
        print("\n❌ BLOCKER: no fixes generated.")
        return 1
    print(f"\n  ✅ {len(fixes)} fix(es) generated")

    # ------------------------------------------------- 5. PR creation
    hr("5. PR CREATION")
    if not create_pr:
        print("  SKIPPED (read-only). Re-run with --create-pr to open real PRs.")
        print(f"  Would open {len(fixes)} PR(s):")
        for repo, path, _ in fixes:
            print(f"    - {repo}: {path}")
        return 0

    ok, fail = [], []
    for repo, path, fixed in fixes:
        url = wh._create_fix_pr(
            repo, path, fixed, change, SOURCE_REPO, token,
            confidence=0.85, sources=["grep"],
            reasons=["Local harness verification"], all_predictions=[],
        )
        if url:
            ok.append(url)
            print(f"  ✅ {repo}: {url}")
        else:
            fail.append(repo)
            print(f"  ❌ {repo}: FAILED (see pr_error below)")

    hr("ACTIVITY LOG (pr_error entries reveal the failing step)")
    for entry in wh._activity_log[-25:]:
        if entry.get("action") in ("pr_error", "search_error", "fix_generated", "pr_result"):
            print(f"  {entry}")

    hr("RESULT")
    print(f"  PRs created: {len(ok)}   failed: {len(fail)}")
    for u in ok:
        print(f"    {u}")
    return 0 if ok and not fail else 1


if __name__ == "__main__":
    sys.exit(main())
