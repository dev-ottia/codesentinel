"""Integration tests — full webhook pipeline including ARQ enqueue."""
import json
from tests.conftest import pr_payload, signed_headers, make_signature


async def test_valid_pr_created_and_enqueued(client, mock_arq):
    """Valid webhook → DB row created → ARQ job enqueued."""
    payload = pr_payload(number=10, sha="aaa111bbb222")
    resp = await client.post("/api/webhooks/github", headers=signed_headers(payload), content=payload)

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["action_taken"] == "created"
    assert "db_record_id" in body
    assert "job_id" in body
    assert body["job_enqueued"] is True

    # Verify ARQ received exactly one job
    assert len(mock_arq.jobs) == 1
    job = mock_arq.jobs[0]
    assert job["func"] == "analyse_pr"
    assert job["args"][0] == body["db_record_id"]


async def test_duplicate_webhook_updates_same_row(client, mock_arq):
    """Same repo+pr+sha sent twice → same DB row, both enqueue (ARQ deduplicates)."""
    payload = pr_payload(number=20, sha="dup000111222")

    r1 = await client.post("/api/webhooks/github", headers=signed_headers(payload), content=payload)
    r2 = await client.post("/api/webhooks/github", headers=signed_headers(payload), content=payload)

    assert r1.json()["db_record_id"] == r2.json()["db_record_id"]
    assert r1.json()["action_taken"] == "created"
    assert r2.json()["action_taken"] == "updated"


async def test_ignored_action_not_enqueued(client, mock_arq):
    """action=closed must be ignored — no DB row, no ARQ job."""
    initial_jobs = len(mock_arq.jobs)
    data = {
        "action": "closed",
        "number": 99,
        "repository": {"full_name": "org/repo"},
        "pull_request": {
            "head": {"sha": "closedsha"},
            "base": {"sha": "baseclosed"},
            "title": "closing",
            "user": {"login": "user"},
            "html_url": "https://github.com/org/repo/pull/99",
        },
    }
    payload = json.dumps(data, separators=(",", ":")).encode()
    resp = await client.post(
        "/api/webhooks/github",
        headers=signed_headers(payload),
        content=payload,
    )
    assert resp.status_code == 202
    assert resp.json()["status"] == "ignored"
    assert len(mock_arq.jobs) == initial_jobs  # nothing enqueued


async def test_synchronize_action_enqueued(client, mock_arq):
    """action=synchronize (new commit) must also be accepted and enqueued."""
    payload = pr_payload(action="synchronize", number=30, sha="sync111222333")
    resp = await client.post("/api/webhooks/github", headers=signed_headers(payload), content=payload)
    assert resp.status_code == 202
    assert resp.json()["job_enqueued"] is True


async def test_list_prs_endpoint(client):
    """GET /api/webhooks/prs returns items list."""
    resp = await client.get("/api/webhooks/prs")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert isinstance(body["items"], list)


async def test_get_pr_detail_not_found(client):
    """GET /api/webhooks/prs/{id} with unknown UUID returns 404."""
    import uuid
    resp = await client.get(f"/api/webhooks/prs/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_get_pr_detail_bad_id(client):
    """GET /api/webhooks/prs/{id} with non-UUID returns 400."""
    resp = await client.get("/api/webhooks/prs/not-a-uuid")
    assert resp.status_code == 400
