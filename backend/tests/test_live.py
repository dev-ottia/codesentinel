"""
Live integration tests — require the full Docker stack running.

Run with:  pytest tests/test_live.py -v -m live
Skip in CI: these are excluded from the default test run.

The SSE endpoint uses an infinite streaming generator. The only reliable way
to test it outside a real HTTP server is to hit the live backend directly.
"""
import pytest
import httpx


pytestmark = pytest.mark.live  # skip unless -m live is passed


def test_sse_stream_live():
    """
    Verify /stream returns 200 + text/event-stream against the live backend.
    Reads the first chunk (connected event) then closes.
    """
    with httpx.stream("GET", "http://localhost:8000/stream", timeout=5.0) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        for chunk in resp.iter_bytes():
            assert b"connected" in chunk
            break  # one chunk is enough


def test_health_live():
    """Verify /health returns ok + db/redis connected against live backend."""
    resp = httpx.get("http://localhost:8000/health", timeout=5.0)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] == "connected"
    assert body["redis"] == "connected"
