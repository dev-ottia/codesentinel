"""Unit tests — health, signature validation."""
from tests.conftest import signed_headers


async def test_health_returns_ok(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("ok", "degraded")
    assert "version" in body
    assert "redis" in body


async def test_invalid_signature_returns_401(client):
    resp = await client.post(
        "/api/webhooks/github",
        headers={"X-Hub-Signature-256": "sha256=badhash"},
        content=b"{}",
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid signature"


async def test_missing_signature_returns_401(client):
    resp = await client.post("/api/webhooks/github", content=b"{}")
    assert resp.status_code == 401


async def test_non_pr_event_ignored(client):
    payload = b'{"ref": "refs/heads/main"}'
    resp = await client.post(
        "/api/webhooks/github",
        headers=signed_headers(payload, event="push"),
        content=payload,
    )
    assert resp.status_code == 202
    assert resp.json()["status"] == "ignored"
