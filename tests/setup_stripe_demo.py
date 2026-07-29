#!/usr/bin/env python3
"""
ripple/tests/setup_stripe_demo.py

Creates a realistic multi-repo demo simulating Stripe-like API + multiple SDK consumers.

Creates:
  • Aakash2408/ripple-payments-api      (OpenAPI spec for a Payments API)
  • Aakash2408/ripple-sdk-python        (Python SDK consumer)
  • Aakash2408/ripple-sdk-node          (Node/TypeScript SDK consumer)
  • Aakash2408/ripple-sdk-java          (Java SDK consumer)

Then pushes a breaking change and runs Ripple to create PRs in all 3 SDKs.

Usage:
    export GITHUB_TOKEN=ghp_xxx
    python3 tests/setup_stripe_demo.py              # Create repos + push code
    python3 tests/setup_stripe_demo.py --break      # Push breaking change + create PRs
    python3 tests/setup_stripe_demo.py --cleanup    # Delete demo repos
"""

import json
import os
import sys
import ssl
import base64
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError

GITHUB_USER = "Aakash2408"
GITHUB_API = "https://api.github.com"

API_REPO = f"{GITHUB_USER}/ripple-payments-api"
SDK_PYTHON = f"{GITHUB_USER}/ripple-sdk-python"
SDK_NODE = f"{GITHUB_USER}/ripple-sdk-node"
SDK_JAVA = f"{GITHUB_USER}/ripple-sdk-java"

ALL_REPOS = [API_REPO, SDK_PYTHON, SDK_NODE, SDK_JAVA]

# SSL fix for Amazon dev desktop
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def gh(method, path, data=None):
    token = os.environ.get("GITHUB_TOKEN", "")
    url = f"{GITHUB_API}{path}"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    body = json.dumps(data).encode() if data else None
    if body: headers["Content-Type"] = "application/json"
    req = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=15, context=CTX) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        msg = e.read().decode()[:200] if hasattr(e, 'read') else ""
        if e.code == 422 and "already exists" in msg:
            return {"exists": True}
        if e.code == 404:
            return None
        print(f"  ⚠️ {e.code}: {msg}")
        return None


def create_repo(name, desc):
    print(f"  Creating {GITHUB_USER}/{name}...")
    return gh("POST", "/user/repos", {"name": name, "description": desc, "private": False, "auto_init": True})


def push(repo, path, content, msg):
    print(f"  → {repo}/{path}")
    encoded = base64.b64encode(content.encode()).decode()
    existing = gh("GET", f"/repos/{repo}/contents/{path}")
    data = {"message": msg, "content": encoded}
    if existing and isinstance(existing, dict) and "sha" in existing:
        data["sha"] = existing["sha"]
    return gh("PUT", f"/repos/{repo}/contents/{path}", data)


# === API Spec ===

SPEC_V1 = '''openapi: "3.0.3"
info:
  title: Payments API
  version: "1.0.0"
  description: Process payments, refunds, and subscriptions
paths:
  /v1/payments:
    post:
      summary: Create a payment
      operationId: createPayment
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required:
                - amount
                - currency
                - customer_id
              properties:
                amount:
                  type: integer
                  description: Amount in cents
                currency:
                  type: string
                  description: ISO 4217 currency code
                customer_id:
                  type: string
                  description: Customer identifier
                description:
                  type: string
                  description: Payment description (optional)
                metadata:
                  type: object
                  description: Key-value metadata (optional)
      responses:
        "201":
          description: Payment created
  /v1/payments/{id}:
    get:
      summary: Retrieve a payment
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: string
      responses:
        "200":
          description: Payment details
  /v1/refunds:
    post:
      summary: Create a refund
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required:
                - payment_id
              properties:
                payment_id:
                  type: string
                amount:
                  type: integer
                  description: Partial refund amount (optional, defaults to full)
      responses:
        "201":
          description: Refund created
'''

SPEC_V2 = '''openapi: "3.0.3"
info:
  title: Payments API
  version: "2.0.0"
  description: Process payments, refunds, and subscriptions. Now requires idempotency key.
paths:
  /v1/payments:
    post:
      summary: Create a payment
      operationId: createPayment
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required:
                - amount
                - currency
                - customer_id
                - idempotency_key
              properties:
                amount:
                  type: integer
                  description: Amount in cents
                currency:
                  type: string
                  description: ISO 4217 currency code
                customer_id:
                  type: string
                  description: Customer identifier
                idempotency_key:
                  type: string
                  description: Unique key to prevent duplicate payments (UUID recommended)
                description:
                  type: string
                  description: Payment description (optional)
                metadata:
                  type: object
                  description: Key-value metadata (optional)
      responses:
        "201":
          description: Payment created
  /v1/payments/{id}:
    get:
      summary: Retrieve a payment
      parameters:
        - name: id
          in: path
          required: true
          schema:
            type: string
      responses:
        "200":
          description: Payment details
  /v1/refunds:
    post:
      summary: Create a refund
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required:
                - payment_id
                - idempotency_key
              properties:
                payment_id:
                  type: string
                idempotency_key:
                  type: string
                  description: Unique key to prevent duplicate refunds
                amount:
                  type: integer
                  description: Partial refund amount (optional, defaults to full)
      responses:
        "201":
          description: Refund created
'''

# === SDK Consumers ===

PYTHON_SDK = '''"""Payments SDK — Python client for the Payments API."""

import requests
from typing import Optional

BASE_URL = "https://api.payments.example.com"


class PaymentsClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {api_key}"

    def create_payment(
        self,
        amount: int,
        currency: str,
        customer_id: str,
        description: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        """Create a new payment."""
        payload = {
            "amount": amount,
            "currency": currency,
            "customer_id": customer_id,
        }
        if description:
            payload["description"] = description
        if metadata:
            payload["metadata"] = metadata

        response = self.session.post(f"{BASE_URL}/v1/payments", json=payload)
        response.raise_for_status()
        return response.json()

    def get_payment(self, payment_id: str) -> dict:
        """Retrieve a payment by ID."""
        response = self.session.get(f"{BASE_URL}/v1/payments/{payment_id}")
        response.raise_for_status()
        return response.json()

    def create_refund(self, payment_id: str, amount: Optional[int] = None) -> dict:
        """Create a refund for a payment."""
        payload = {"payment_id": payment_id}
        if amount is not None:
            payload["amount"] = amount

        response = self.session.post(f"{BASE_URL}/v1/refunds", json=payload)
        response.raise_for_status()
        return response.json()
'''

NODE_SDK = '''// Payments SDK — TypeScript client for the Payments API

import axios, { AxiosInstance } from "axios";

const BASE_URL = "https://api.payments.example.com";

interface CreatePaymentParams {
  amount: number;
  currency: string;
  customer_id: string;
  description?: string;
  metadata?: Record<string, string>;
}

interface CreateRefundParams {
  payment_id: string;
  amount?: number;
}

export class PaymentsClient {
  private client: AxiosInstance;

  constructor(apiKey: string) {
    this.client = axios.create({
      baseURL: BASE_URL,
      headers: { Authorization: `Bearer ${apiKey}` },
    });
  }

  async createPayment(params: CreatePaymentParams): Promise<any> {
    const response = await this.client.post("/v1/payments", {
      amount: params.amount,
      currency: params.currency,
      customer_id: params.customer_id,
      description: params.description,
      metadata: params.metadata,
    });
    return response.data;
  }

  async getPayment(paymentId: string): Promise<any> {
    const response = await this.client.get(`/v1/payments/${paymentId}`);
    return response.data;
  }

  async createRefund(params: CreateRefundParams): Promise<any> {
    const response = await this.client.post("/v1/refunds", {
      payment_id: params.payment_id,
      amount: params.amount,
    });
    return response.data;
  }
}
'''

JAVA_SDK = '''package com.payments.sdk;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;

/**
 * Payments SDK - Java client for the Payments API.
 */
public class PaymentsClient {

    private static final String BASE_URL = "https://api.payments.example.com";
    private final HttpClient client = HttpClient.newHttpClient();
    private final String apiKey;

    public PaymentsClient(String apiKey) {
        this.apiKey = apiKey;
    }

    public String createPayment(int amount, String currency, String customerId) throws Exception {
        String json = String.format(
            "{\\"amount\\": %d, \\"currency\\": \\"%s\\", \\"customer_id\\": \\"%s\\"}",
            amount, currency, customerId
        );

        HttpRequest request = HttpRequest.newBuilder()
            .uri(URI.create(BASE_URL + "/v1/payments"))
            .header("Content-Type", "application/json")
            .header("Authorization", "Bearer " + apiKey)
            .POST(HttpRequest.BodyPublishers.ofString(json))
            .build();

        HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
        return response.body();
    }

    public String getPayment(String paymentId) throws Exception {
        HttpRequest request = HttpRequest.newBuilder()
            .uri(URI.create(BASE_URL + "/v1/payments/" + paymentId))
            .header("Authorization", "Bearer " + apiKey)
            .GET()
            .build();

        HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
        return response.body();
    }

    public String createRefund(String paymentId, Integer amount) throws Exception {
        String json;
        if (amount != null) {
            json = String.format("{\\"payment_id\\": \\"%s\\", \\"amount\\": %d}", paymentId, amount);
        } else {
            json = String.format("{\\"payment_id\\": \\"%s\\"}", paymentId);
        }

        HttpRequest request = HttpRequest.newBuilder()
            .uri(URI.create(BASE_URL + "/v1/refunds"))
            .header("Content-Type", "application/json")
            .header("Authorization", "Bearer " + apiKey)
            .POST(HttpRequest.BodyPublishers.ofString(json))
            .build();

        HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
        return response.body();
    }
}
'''


def setup():
    print("\n🌊 RIPPLE STRIPE-LIKE DEMO SETUP")
    print("=" * 55)

    # Create repos
    create_repo("ripple-payments-api", "Demo: Payments API with OpenAPI spec (Ripple demo)")
    time.sleep(3)
    create_repo("ripple-sdk-python", "Demo: Python SDK for Payments API (Ripple demo)")
    time.sleep(2)
    create_repo("ripple-sdk-node", "Demo: Node.js/TypeScript SDK for Payments API (Ripple demo)")
    time.sleep(2)
    create_repo("ripple-sdk-java", "Demo: Java SDK for Payments API (Ripple demo)")
    time.sleep(3)

    # Push spec
    print("\n📄 Pushing API spec (v1)...")
    push(API_REPO, "openapi.yaml", SPEC_V1, "Initial Payments API spec v1.0.0")

    # Push SDKs
    print("\n🐍 Pushing Python SDK...")
    push(SDK_PYTHON, "payments/client.py", PYTHON_SDK, "Add Payments SDK client")
    push(SDK_PYTHON, "README.md", "# Payments Python SDK\n\nPython client for the Payments API.\n\n```python\nfrom payments.client import PaymentsClient\nclient = PaymentsClient('sk_test_xxx')\nclient.create_payment(amount=2000, currency='usd', customer_id='cus_123')\n```\n", "Add README")

    print("\n📦 Pushing Node SDK...")
    push(SDK_NODE, "src/client.ts", NODE_SDK, "Add Payments SDK client")
    push(SDK_NODE, "README.md", "# Payments Node SDK\n\nTypeScript client for the Payments API.\n\n```typescript\nimport { PaymentsClient } from './src/client';\nconst client = new PaymentsClient('sk_test_xxx');\nawait client.createPayment({ amount: 2000, currency: 'usd', customer_id: 'cus_123' });\n```\n", "Add README")

    print("\n☕ Pushing Java SDK...")
    push(SDK_JAVA, "src/main/java/com/payments/sdk/PaymentsClient.java", JAVA_SDK, "Add Payments SDK client")
    push(SDK_JAVA, "README.md", "# Payments Java SDK\n\nJava client for the Payments API.\n\n```java\nPaymentsClient client = new PaymentsClient(\"sk_test_xxx\");\nclient.createPayment(2000, \"usd\", \"cus_123\");\n```\n", "Add README")

    print(f"\n{'='*55}")
    print("✅ Setup complete!")
    print(f"   API:    https://github.com/{API_REPO}")
    print(f"   Python: https://github.com/{SDK_PYTHON}")
    print(f"   Node:   https://github.com/{SDK_NODE}")
    print(f"   Java:   https://github.com/{SDK_JAVA}")
    print(f"\n   Next: python3 tests/setup_stripe_demo.py --break")
    print(f"{'='*55}")


def push_breaking_change():
    """Push v2 spec (adds idempotency_key) and create fix PRs."""
    print("\n🌊 RIPPLE DEMO — Breaking Change!")
    print("=" * 55)
    print("\n💥 Pushing v2 spec (adds required 'idempotency_key')...")
    push(API_REPO, "openapi.yaml", SPEC_V2,
         "feat!: Require idempotency_key for all mutations\n\nBREAKING: POST /v1/payments and POST /v1/refunds now require 'idempotency_key'")

    print("\n🔧 Running Ripple to fix consumers...")

    # For each SDK, generate fix and create PR
    sdk_repos = [
        (SDK_PYTHON, "payments/client.py", "python"),
        (SDK_NODE, "src/client.ts", "typescript"),
        (SDK_JAVA, "src/main/java/com/payments/sdk/PaymentsClient.java", "java"),
    ]

    for repo, file_path, lang in sdk_repos:
        print(f"\n  Processing {repo}...")

        # Fetch current file
        file_data = gh("GET", f"/repos/{repo}/contents/{file_path}")
        if not file_data or "content" not in file_data:
            print(f"    ⚠️ Could not fetch {file_path}")
            continue

        content = base64.b64decode(file_data["content"]).decode()

        # Generate fix (simple template: add idempotency_key parameter)
        fixed = _add_idempotency_key(content, lang)
        if fixed == content:
            print(f"    ⚠️ No fix generated")
            continue

        # Create branch + PR
        repo_data = gh("GET", f"/repos/{repo}")
        default_branch = repo_data.get("default_branch", "main")
        ref_data = gh("GET", f"/repos/{repo}/git/ref/heads/{default_branch}")
        base_sha = ref_data["object"]["sha"]

        branch = "ripple/fix-idempotency-key"
        gh("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": base_sha})

        push_data = {
            "message": "fix: Add required 'idempotency_key' to payment and refund calls\n\nThe Payments API now requires an idempotency_key for all mutation endpoints.\nThis prevents duplicate charges from network retries.\n\nAutomatically generated by Ripple.",
            "content": base64.b64encode(fixed.encode()).decode(),
            "branch": branch,
            "sha": file_data["sha"],
        }
        gh("PUT", f"/repos/{repo}/contents/{file_path}", push_data)

        pr = gh("POST", f"/repos/{repo}/pulls", {
            "title": "fix: Add required 'idempotency_key' for duplicate payment prevention",
            "body": f"## 🌊 Ripple — Automated API Change Propagation\n\n### Breaking Change\n\n| Field | Value |\n|---|---|\n| **Source** | `{API_REPO}` |\n| **Endpoints** | `POST /v1/payments`, `POST /v1/refunds` |\n| **Change** | Added required field `idempotency_key` |\n| **Reason** | Prevent duplicate payments from network retries |\n\n### What changed\n\nThe Payments API v2.0.0 now requires an `idempotency_key` (string, UUID recommended) for all mutation endpoints. Without this field, requests will be rejected with `400 Bad Request`.\n\n### Fix applied\n\nAdded `idempotency_key` parameter to `createPayment()` and `createRefund()` methods.\n\n---\n*Auto-generated by [Ripple](https://github.com/{GITHUB_USER}/ripple)*",
            "head": branch,
            "base": default_branch,
        })

        if pr and "html_url" in pr:
            print(f"    ✅ PR: {pr['html_url']}")
        else:
            print(f"    ⚠️ PR result: {pr}")

    print(f"\n{'='*55}")
    print("🌊 DEMO COMPLETE!")
    print(f"{'='*55}")


def _add_idempotency_key(code, lang):
    """Add idempotency_key parameter to payment/refund methods."""
    if lang == "python":
        # Add parameter to create_payment
        code = code.replace(
            "def create_payment(\n        self,\n        amount: int,\n        currency: str,\n        customer_id: str,",
            "def create_payment(\n        self,\n        amount: int,\n        currency: str,\n        customer_id: str,\n        idempotency_key: str,"
        )
        # Add to payload
        code = code.replace(
            '        payload = {\n            "amount": amount,\n            "currency": currency,\n            "customer_id": customer_id,\n        }',
            '        payload = {\n            "amount": amount,\n            "currency": currency,\n            "customer_id": customer_id,\n            "idempotency_key": idempotency_key,\n        }'
        )
        # Add to create_refund
        code = code.replace(
            "def create_refund(self, payment_id: str, amount: Optional[int] = None) -> dict:",
            "def create_refund(self, payment_id: str, idempotency_key: str, amount: Optional[int] = None) -> dict:"
        )
        code = code.replace(
            '        payload = {"payment_id": payment_id}',
            '        payload = {"payment_id": payment_id, "idempotency_key": idempotency_key}'
        )

    elif lang == "typescript":
        # Add to interface
        code = code.replace(
            "interface CreatePaymentParams {\n  amount: number;\n  currency: string;\n  customer_id: string;",
            "interface CreatePaymentParams {\n  amount: number;\n  currency: string;\n  customer_id: string;\n  idempotency_key: string;"
        )
        code = code.replace(
            "interface CreateRefundParams {\n  payment_id: string;",
            "interface CreateRefundParams {\n  payment_id: string;\n  idempotency_key: string;"
        )
        # Add to payload
        code = code.replace(
            "      customer_id: params.customer_id,\n      description: params.description,",
            "      customer_id: params.customer_id,\n      idempotency_key: params.idempotency_key,\n      description: params.description,"
        )
        code = code.replace(
            "      payment_id: params.payment_id,\n      amount: params.amount,",
            "      payment_id: params.payment_id,\n      idempotency_key: params.idempotency_key,\n      amount: params.amount,"
        )

    elif lang == "java":
        # Add parameter
        code = code.replace(
            "public String createPayment(int amount, String currency, String customerId) throws Exception {",
            "public String createPayment(int amount, String currency, String customerId, String idempotencyKey) throws Exception {"
        )
        code = code.replace(
            '{"amount": %d, "currency": "%s", "customer_id": "%s"}',
            '{"amount": %d, "currency": "%s", "customer_id": "%s", "idempotency_key": "%s"}'
        )
        code = code.replace(
            "amount, currency, customerId",
            "amount, currency, customerId, idempotencyKey"
        )
        # Refund
        code = code.replace(
            "public String createRefund(String paymentId, Integer amount) throws Exception {",
            "public String createRefund(String paymentId, String idempotencyKey, Integer amount) throws Exception {"
        )

    return code


def cleanup():
    print("\n🧹 Cleaning up demo repos...")
    for repo in ALL_REPOS:
        print(f"  Deleting {repo}...")
        gh("DELETE", f"/repos/{repo}")
    print("  Done.")


if __name__ == "__main__":
    if "--cleanup" in sys.argv:
        cleanup()
    elif "--break" in sys.argv:
        push_breaking_change()
    else:
        setup()
