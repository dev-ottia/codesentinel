#!/usr/bin/env python3
"""
Live webhook test — fires real webhook events at the running backend.

Usage (from project root, with Docker stack running):
    python test_webhook.py

Requires: pip install httpx
"""
import hmac
import hashlib
import json
import sys

try:
    import httpx
except ImportError:
    print("Install httpx first:  pip install httpx")
    sys.exit(1)

WEBHOOK_URL = "http://localhost:8000/api/webhooks/github"
SECRET      = "persieforeign"   # matches GITHUB_APP_WEBHOOK_SECRET in .env

PAYLOADS = [
    {
        "label": "Test 1: New PR (action=opened)",
        "payload": {
            "action": "opened",
            "number": 101,
            "repository": {"full_name": "test-org/demo-repo"},
            "pull_request": {
                "head": {"sha": "abc123def456abc1"},
                "base": {"sha": "base000111222333"},
                "title": "Add user authentication feature",
                "user": {"login": "alice-dev"},
                "html_url": "https://github.com/test-org/demo-repo/pull/101",
            },
        },
    },
    {
        "label": "Test 2: Same PR, synchronize (should update, not duplicate)",
        "payload": {
            "action": "synchronize",
            "number": 101,
            "repository": {"full_name": "test-org/demo-repo"},
            "pull_request": {
                "head": {"sha": "abc123def456abc1"},   # same SHA = same record
                "base": {"sha": "base000111222333"},
                "title": "Add user authentication feature (updated)",
                "user": {"login": "alice-dev"},
                "html_url": "https://github.com/test-org/demo-repo/pull/101",
            },
        },
    },
    {
        "label": "Test 3: Different PR (new record)",
        "payload": {
            "action": "opened",
            "number": 102,
            "repository": {"full_name": "test-org/demo-repo"},
            "pull_request": {
                "head": {"sha": "def456ghi789def4"},
                "base": {"sha": "base444555666777"},
                "title": "Fix SQL injection vulnerability",
                "user": {"login": "bob-security"},
                "html_url": "https://github.com/test-org/demo-repo/pull/102",
            },
        },
    },
]


def sign(payload_bytes: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), payload_bytes, hashlib.sha256).hexdigest()


def send(label: str, payload: dict) -> str | None:
    body = json.dumps(payload, separators=(",", ":")).encode()
    sig  = sign(body)

    print(f"\n📤 {label}")
    try:
        r = httpx.post(
            WEBHOOK_URL,
            headers={
                "Content-Type":         "application/json",
                "X-GitHub-Event":       "pull_request",
                "X-Hub-Signature-256":  sig,
            },
            content=body,
            timeout=10,
        )
        print(f"   Status:  {r.status_code}")
        resp = r.json()
        print(f"   Response: {json.dumps(resp, indent=6)}")
        return resp.get("db_record_id")
    except httpx.ConnectError:
        print("   ❌ Could not connect — is Docker stack running?")
        print("      Run:  docker compose up")
        return None
    except Exception as exc:
        print(f"   ❌ Error: {exc}")
        return None


def main():
    print("🧪 CodeSentinel — Live Webhook Test")
    print("=" * 50)

    # Check health first
    try:
        health = httpx.get("http://localhost:8000/health", timeout=5).json()
        print(f"✅ Backend healthy: db={health.get('db')} redis={health.get('redis')}")
    except Exception:
        print("❌ Backend not reachable — start Docker first:  docker compose up")
        sys.exit(1)

    ids = []
    for item in PAYLOADS:
        db_id = send(item["label"], item["payload"])
        ids.append(db_id)

    print(f"\n📊 Results:")
    print(f"   Test 1 DB ID: {ids[0]}")
    print(f"   Test 2 DB ID: {ids[1]}")
    print(f"   Test 3 DB ID: {ids[2]}")

    if ids[0] and ids[1]:
        same = ids[0] == ids[1]
        print(f"   Idempotency:  {'✅ PASS (same row updated)' if same else '❌ FAIL (duplicate row created)'}")

    print("\n🔍 Check worker logs:  docker compose logs worker -f")
    print("🌐 Open dashboard:     http://localhost:3000/dashboard")


if __name__ == "__main__":
    main()
