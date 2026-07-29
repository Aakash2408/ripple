#!/usr/bin/env python3
"""
ripple/tests/setup_github_demo.py

Sets up real GitHub repos for the Ripple demo.
Creates: aakash2408/ripple-demo-api (spec) + aakash2408/ripple-demo-frontend (consumer)
Then runs the full pipeline to create a real PR.

Prerequisites:
    export GITHUB_TOKEN=ghp_your_token_here

Usage:
    python3 tests/setup_github_demo.py           # Create repos + push code
    python3 tests/setup_github_demo.py --run     # Also run Ripple and create PR
    python3 tests/setup_github_demo.py --cleanup # Delete the test repos
"""

import json
import os
import sys
import base64
from urllib.request import Request, urlopen
from urllib.error import HTTPError

GITHUB_USER = "aakash2408"
API_REPO = f"{GITHUB_USER}/ripple-demo-api"
CONSUMER_REPO = f"{GITHUB_USER}/ripple-demo-frontend"
GITHUB_API = "https://api.github.com"


def github_request(method, path, data=None):
    """Make GitHub API request."""
    import ssl
    
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("ERROR: Set GITHUB_TOKEN environment variable first.")
        print("  Go to: https://github.com/settings/tokens/new")
        print("  Scopes needed: repo")
        sys.exit(1)
    
    url = f"{GITHUB_API}{path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    
    body = json.dumps(data).encode() if data else None
    if body:
        headers["Content-Type"] = "application/json"
    
    req = Request(url, data=body, headers=headers, method=method)
    
    # SSL fix for Amazon dev desktops (internal CA doesn't have GitHub certs)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    try:
        with urlopen(req, timeout=15, context=ctx) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        error_body = e.read().decode() if hasattr(e, 'read') else ""
        if e.code == 422 and "already exists" in error_body:
            print(f"  (already exists, skipping)")
            return {"already_exists": True}
        print(f"  ERROR {e.code}: {error_body[:200]}")
        return None


def create_repo(name, description):
    """Create a GitHub repo."""
    print(f"  Creating repo: {GITHUB_USER}/{name}")
    result = github_request("POST", "/user/repos", {
        "name": name,
        "description": description,
        "auto_init": True,
        "private": False,
    })
    return result


def push_file(repo, path, content, message):
    """Create or update a file in a repo."""
    import time
    print(f"  Pushing: {repo}/{path}")
    encoded = base64.b64encode(content.encode()).decode()
    
    # Check if file exists (may not on fresh repos)
    existing_sha = None
    try:
        existing = github_request("GET", f"/repos/{repo}/contents/{path}")
        if existing and isinstance(existing, dict) and "sha" in existing:
            existing_sha = existing["sha"]
    except:
        pass
    
    data = {
        "message": message,
        "content": encoded,
    }
    if existing_sha:
        data["sha"] = existing_sha
    
    result = github_request("PUT", f"/repos/{repo}/contents/{path}", data)
    if not result:
        # Retry once after a brief pause (repo may still be initializing)
        time.sleep(2)
        result = github_request("PUT", f"/repos/{repo}/contents/{path}", data)
    return result


# === File contents ===

API_SPEC_V1 = """openapi: "3.0.3"
info:
  title: Users API
  version: "1.0.0"
  description: User management service
paths:
  /users:
    post:
      summary: Create a new user
      operationId: createUser
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required:
                - name
                - email
              properties:
                name:
                  type: string
                  description: User's full name
                email:
                  type: string
                  format: email
                  description: User's email address
                age:
                  type: integer
                  description: User's age (optional)
      responses:
        "201":
          description: User created successfully
    get:
      summary: List all users
      responses:
        "200":
          description: List of users
"""

API_SPEC_V2 = """openapi: "3.0.3"
info:
  title: Users API
  version: "2.0.0"
  description: User management service — now requires country for compliance
paths:
  /users:
    post:
      summary: Create a new user
      operationId: createUser
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required:
                - name
                - email
                - country
              properties:
                name:
                  type: string
                  description: User's full name
                email:
                  type: string
                  format: email
                  description: User's email address
                country:
                  type: string
                  description: ISO 3166-1 alpha-2 country code (required for GDPR compliance)
                age:
                  type: integer
                  description: User's age (optional)
      responses:
        "201":
          description: User created successfully
    get:
      summary: List all users
      responses:
        "200":
          description: List of users
"""

CONSUMER_CODE = """// src/api/users.ts
// Frontend client for the Users API

import { httpClient } from '../lib/http';

export interface CreateUserRequest {
  name: string;
  email: string;
  age?: number;
}

export interface User {
  id: string;
  name: string;
  email: string;
  age?: number;
  createdAt: string;
}

export async function createUser(data: CreateUserRequest): Promise<User> {
  const response = await httpClient.post('/users', {
    name: data.name,
    email: data.email,
    age: data.age,
  });
  return response.data;
}

export async function listUsers(): Promise<User[]> {
  const response = await httpClient.get('/users');
  return response.data;
}

export async function getUser(id: string): Promise<User> {
  const response = await httpClient.get(`/users/${id}`);
  return response.data;
}
"""

CONSUMER_README = """# Ripple Demo — Frontend Client

This repo demonstrates a consumer of the Users API.

When the Users API adds a new required field (`country`), 
Ripple automatically detects the breaking change, finds this consumer,
generates the fix, and opens a PR.

## The Demo

1. API spec in `ripple-demo-api` adds `country` as required
2. Ripple detects the breaking change
3. Ripple finds this repo calls `POST /users`
4. Ripple generates a fix (adds `country` to the interface + API call)
5. Ripple opens a PR with the fix

**This PR was created automatically by Ripple.**
"""


def setup():
    """Create both repos and push initial code."""
    import time
    
    print("\n🌊 RIPPLE DEMO SETUP")
    print("=" * 50)
    
    # Create API repo
    print("\n📦 Setting up API repo...")
    create_repo("ripple-demo-api", "Demo: API service with OpenAPI spec (for Ripple demo)")
    time.sleep(3)  # Wait for GitHub to initialize the repo
    push_file(API_REPO, "openapi.yaml", API_SPEC_V1, "Initial API spec v1.0.0")
    
    # Create consumer repo  
    print("\n📦 Setting up consumer repo...")
    create_repo("ripple-demo-frontend", "Demo: Frontend consumer of Users API (for Ripple demo)")
    time.sleep(3)  # Wait for GitHub to initialize the repo
    push_file(CONSUMER_REPO, "src/api/users.ts", CONSUMER_CODE, "Add Users API client")
    push_file(CONSUMER_REPO, "README.md", CONSUMER_README, "Add README")
    
    print("\n✅ Setup complete!")
    print(f"   API repo:      https://github.com/{API_REPO}")
    print(f"   Consumer repo: https://github.com/{CONSUMER_REPO}")
    print(f"\n   Next: Push v2 spec and run Ripple:")
    print(f"   python3 tests/setup_github_demo.py --run")


def run_demo():
    """Push v2 spec and run Ripple to create a PR."""
    print("\n🌊 RIPPLE DEMO — Running full pipeline")
    print("=" * 50)
    
    # Push v2 spec (the breaking change)
    print("\n📤 Pushing breaking change (v2 spec with 'country' field)...")
    push_file(API_REPO, "openapi.yaml", API_SPEC_V2, 
              "feat: Add required 'country' field for GDPR compliance\n\nBREAKING CHANGE: POST /users now requires 'country' field")
    
    print("\n🔍 Running Ripple pipeline...")
    
    # Now run Ripple programmatically
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    
    from app.diff_engine import diff_specs, BreakingChange
    from app.consumer_finder import ConsumerMatch
    from app.fix_generator import generate_fix, GeneratedFix
    from app.pr_engine import create_pr, CreatedPR
    
    # Create the breaking change manually (since we don't have local files)
    breaking_change = BreakingChange(
        change_type="added_required_field",
        path="/users",
        method="post",
        field_name="country",
        field_type="string",
        location="request_body",
        severity="breaking",
        description="New required field 'country' added for GDPR compliance.",
    )
    
    # Fetch consumer file from GitHub
    token = os.environ["GITHUB_TOKEN"]
    print("  Fetching consumer code from GitHub...")
    
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    req = Request(
        f"{GITHUB_API}/repos/{CONSUMER_REPO}/contents/src/api/users.ts",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    )
    resp = json.loads(urlopen(req, context=ctx).read().decode())
    consumer_code = base64.b64decode(resp["content"]).decode()
    
    # Generate fix
    print("  Generating fix...")
    consumer = ConsumerMatch(
        file_path="src/api/users.ts",
        line_number=21,
        code_snippet="const response = await httpClient.post('/users', {",
        confidence="high",
        match_reason="HTTP POST call to '/users'",
        language="typescript",
    )
    
    # Use template fix
    from app.fix_generator import _generate_with_template
    fixed_code, explanation = _generate_with_template(consumer_code, consumer, breaking_change)
    
    # Create the PR
    print("  Creating PR...")
    
    # Get default branch
    repo_info = github_request("GET", f"/repos/{CONSUMER_REPO}")
    default_branch = repo_info.get("default_branch", "main")
    
    # Get HEAD sha
    ref_info = github_request("GET", f"/repos/{CONSUMER_REPO}/git/ref/heads/{default_branch}")
    base_sha = ref_info["object"]["sha"]
    
    # Create branch
    branch_name = "ripple/fix-country-users"
    print(f"  Creating branch: {branch_name}")
    github_request("POST", f"/repos/{CONSUMER_REPO}/git/refs", {
        "ref": f"refs/heads/{branch_name}",
        "sha": base_sha,
    })
    
    # Push fixed file
    # Get current file sha
    file_info = github_request("GET", f"/repos/{CONSUMER_REPO}/contents/src/api/users.ts?ref={branch_name}")
    file_sha = file_info["sha"] if file_info else None
    
    push_data = {
        "message": "fix: Add required field 'country' to POST /users\n\nAutomatically generated by Ripple.\nAPI breaking change: 'country' field is now required for GDPR compliance.",
        "content": base64.b64encode(fixed_code.encode()).decode(),
        "branch": branch_name,
    }
    if file_sha:
        push_data["sha"] = file_sha
    
    github_request("PUT", f"/repos/{CONSUMER_REPO}/contents/src/api/users.ts", push_data)
    
    # Create PR
    print("  Opening Pull Request...")
    pr_body = f"""## 🌊 Ripple — Automated API Change Propagation

### Breaking Change Detected

| Field | Value |
|-------|-------|
| **Source** | `{API_REPO}` |
| **Endpoint** | `POST /users` |
| **Change** | Added required field |
| **Field** | `country` (string) |
| **Reason** | GDPR compliance |

### What happened

The Users API (`{API_REPO}`) added `country` as a required field in `POST /users`.
This consumer was calling the endpoint without that field and would break.

### Fix applied

{explanation}

---
*This PR was automatically generated by [Ripple](https://github.com/{GITHUB_USER}/ripple) — self-maintaining APIs.*
"""
    
    pr_result = github_request("POST", f"/repos/{CONSUMER_REPO}/pulls", {
        "title": "fix: Add required 'country' field to POST /users call",
        "body": pr_body,
        "head": branch_name,
        "base": default_branch,
    })
    
    if pr_result and "html_url" in pr_result:
        print(f"\n{'='*50}")
        print(f"  🎉 PR CREATED SUCCESSFULLY!")
        print(f"  {pr_result['html_url']}")
        print(f"{'='*50}")
    else:
        print(f"\n  ⚠️  PR creation response: {pr_result}")


def cleanup():
    """Delete the test repos."""
    print("\n🧹 Cleaning up demo repos...")
    github_request("DELETE", f"/repos/{API_REPO}")
    github_request("DELETE", f"/repos/{CONSUMER_REPO}")
    print("  Done.")


if __name__ == "__main__":
    if "--cleanup" in sys.argv:
        cleanup()
    elif "--run" in sys.argv:
        run_demo()
    else:
        setup()
