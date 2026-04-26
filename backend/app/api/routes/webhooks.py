"""GitHub webhook endpoint — ingest, validate, persist, enqueue.

Pipeline:
  1. Verify HMAC-SHA256 signature (constant-time)
  2. Filter by event type and PR action
  3. Upsert PullRequest row (DB-level idempotency via UniqueConstraint)
  4. Enqueue `analyse_pr` ARQ job (idempotent — ARQ deduplicates by job_id)
  5. Return 202 Accepted immediately

Security hardening:
  - Payload size cap (5 MB)
  - Constant-time HMAC compare
  - Input truncation before DB writes
  - IntegrityError race-condition handling
  - No internal details leaked in 5xx responses
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select, desc
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.core.arq_pool import get_arq_pool
from app.core.config import settings
from app.db.models import PullRequest, PRStatus
from app.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter()

_MAX_PAYLOAD_BYTES = 5 * 1024 * 1024  # 5 MB hard cap


# ── Helpers ───────────────────────────────────────────────────────────────────

def _verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Constant-time HMAC-SHA256 verification."""
    if not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _safe_str(value: object, max_len: int = 255) -> str:
    return str(value or "")[:max_len]


# ── Webhook ingestion ─────────────────────────────────────────────────────────

@router.post("/github", status_code=status.HTTP_202_ACCEPTED)
async def handle_github_webhook(
    request: Request,
    arq: ArqRedis = Depends(get_arq_pool),
):
    """
    Receive a GitHub pull_request webhook, persist it, and enqueue analysis.

    Returns 202 immediately — analysis runs asynchronously in the worker.
    """
    # ── 1. Read & size-check ──────────────────────────────────────────────────
    payload = await request.body()
    if len(payload) > _MAX_PAYLOAD_BYTES:
        logger.warning("Oversized webhook payload (%d bytes) rejected", len(payload))
        raise HTTPException(status_code=413, detail="Payload too large")

    # ── 2. Verify HMAC ────────────────────────────────────────────────────────
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_signature(payload, signature, settings.GITHUB_APP_WEBHOOK_SECRET):
        logger.warning(
            "Invalid webhook signature from %s",
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    # ── 3. Filter event / action ──────────────────────────────────────────────
    event_type = request.headers.get("X-GitHub-Event", "")
    if event_type != "pull_request":
        return {"status": "ignored", "reason": f"event_type={event_type}"}

    try:
        data: dict = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    action = data.get("action", "")
    if action not in {"opened", "synchronize", "reopened"}:
        return {"status": "ignored", "reason": f"action={action}"}

    # ── 4. Extract fields ─────────────────────────────────────────────────────
    repo_full_name = _safe_str(data.get("repository", {}).get("full_name", "unknown/repo"), 255)
    pr_number      = _safe_str(data.get("number"), 20)
    head_sha       = _safe_str(data.get("pull_request", {}).get("head", {}).get("sha", ""), 40)
    pr_title       = _safe_str(data.get("pull_request", {}).get("title", "Untitled"), 500)
    pr_author      = _safe_str(data.get("pull_request", {}).get("user", {}).get("login", "unknown"), 100)
    pr_url         = _safe_str(data.get("pull_request", {}).get("html_url", ""), 500)
    base_sha       = _safe_str(data.get("pull_request", {}).get("base", {}).get("sha", ""), 40)

    if not head_sha:
        raise HTTPException(status_code=400, detail="Missing pull_request.head.sha")

    pr_key = f"{repo_full_name}#{pr_number}@{head_sha[:7]}"
    logger.info("Webhook received: %s action=%s", pr_key, action)

    # ── 5. Upsert PR record ───────────────────────────────────────────────────
    pr_db_id: str
    action_taken: str

    try:
        async with AsyncSessionLocal() as db:
            stmt = select(PullRequest).where(
                PullRequest.repo_full_name == repo_full_name,
                PullRequest.pr_number == pr_number,
                PullRequest.head_sha == head_sha,
            )
            existing = (await db.execute(stmt)).scalar_one_or_none()
            now = datetime.now(timezone.utc)

            if existing:
                existing.status     = PRStatus.QUEUED
                existing.title      = pr_title
                existing.author     = pr_author
                existing.updated_at = now
                pr = existing
                action_taken = "updated"
            else:
                pr = PullRequest(
                    repo_full_name=repo_full_name,
                    pr_number=pr_number,
                    head_sha=head_sha,
                    title=pr_title,
                    author=pr_author,
                    pr_url=pr_url,
                    base_sha=base_sha,
                    status=PRStatus.QUEUED,
                    analysis_result={},
                )
                db.add(pr)
                action_taken = "created"

            await db.commit()
            await db.refresh(pr)
            pr_db_id = str(pr.id)

    except IntegrityError:
        # Race: parallel request already inserted this row — safe to ignore
        logger.warning("Race-condition duplicate for %s — treated as queued", pr_key)
        return {"status": "queued", "pr_key": pr_key, "note": "duplicate handled"}
    except SQLAlchemyError as exc:
        logger.error("DB error for %s: %s", pr_key, exc)
        raise HTTPException(status_code=500, detail="Database error")

    # ── 6. Enqueue analysis job ───────────────────────────────────────────────
    # job_id is deterministic: same PR + SHA always produces the same key.
    # ARQ ignores enqueue calls for a job_id that is already queued/running,
    # giving us Redis-level idempotency on top of the DB UniqueConstraint.
    job_id = f"analyse:{repo_full_name}:{pr_number}:{head_sha}"

    try:
        job = await arq.enqueue_job(
            "analyse_pr",          # function name registered in WorkerSettings
            pr_db_id,              # positional arg passed to the task
            _job_id=job_id,        # deterministic ID → deduplication
        )
        job_enqueued = job is not None  # None = ARQ skipped duplicate
        logger.info(
            "Webhook %s — %s, job %s (%s)",
            pr_key,
            action_taken,
            job_id,
            "enqueued" if job_enqueued else "already queued",
        )
    except Exception as exc:
        # Redis failure must NOT cause a 5xx — the PR is already saved.
        # Log the error; the worker can be retriggered manually or via retry.
        logger.error("Failed to enqueue job for %s: %s", pr_key, exc)
        job_enqueued = False

    return {
        "status": "queued",
        "pr_key": pr_key,
        "db_record_id": pr_db_id,
        "action_taken": action_taken,
        "job_id": job_id,
        "job_enqueued": job_enqueued,
    }


# ── PR list (dashboard) ───────────────────────────────────────────────────────

@router.get("/prs", status_code=status.HTTP_200_OK)
async def list_pull_requests(limit: int = 20, offset: int = 0):
    """Return most-recently-updated pull requests for the dashboard."""
    limit = min(limit, 100)

    try:
        async with AsyncSessionLocal() as db:
            stmt = (
                select(PullRequest)
                .order_by(desc(PullRequest.updated_at))
                .limit(limit)
                .offset(offset)
            )
            prs = (await db.execute(stmt)).scalars().all()
    except SQLAlchemyError as exc:
        logger.error("DB error listing PRs: %s", exc)
        raise HTTPException(status_code=500, detail="Database error")

    return {
        "total": len(prs),
        "items": [
            {
                "id":         str(pr.id),
                "repo":       pr.repo_full_name,
                "pr_number":  pr.pr_number,
                "head_sha":   pr.head_sha,
                "title":      pr.title,
                "author":     pr.author,
                "pr_url":     pr.pr_url,
                "status":     pr.status.value,
                "created_at": pr.created_at.isoformat() if pr.created_at else None,
                "updated_at": pr.updated_at.isoformat() if pr.updated_at else None,
                "analysis_result": pr.analysis_result,
            }
            for pr in prs
        ],
    }


# ── Single PR detail ──────────────────────────────────────────────────────────

@router.get("/prs/{pr_id}", status_code=status.HTTP_200_OK)
async def get_pull_request(pr_id: str):
    """Return full detail for a single PR including analysis result."""
    try:
        import uuid as _uuid
        uid = _uuid.UUID(pr_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid PR id format")

    try:
        async with AsyncSessionLocal() as db:
            pr = (await db.execute(
                select(PullRequest).where(PullRequest.id == uid)
            )).scalar_one_or_none()
    except SQLAlchemyError as exc:
        logger.error("DB error fetching PR %s: %s", pr_id, exc)
        raise HTTPException(status_code=500, detail="Database error")

    if not pr:
        raise HTTPException(status_code=404, detail="PR not found")

    return {
        "id":              str(pr.id),
        "repo":            pr.repo_full_name,
        "pr_number":       pr.pr_number,
        "head_sha":        pr.head_sha,
        "base_sha":        pr.base_sha,
        "title":           pr.title,
        "author":          pr.author,
        "pr_url":          pr.pr_url,
        "status":          pr.status.value,
        "created_at":      pr.created_at.isoformat() if pr.created_at else None,
        "updated_at":      pr.updated_at.isoformat() if pr.updated_at else None,
        "analysis_result": pr.analysis_result,
    }
