"""ARQ background worker — CodeSentinel.
Publishes SSE events after each status transition so the browser updates instantly.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.core.arq_pool import REDIS_SETTINGS
from app.core.config import settings
from app.db.models import PullRequest, PRStatus
from app.db.session import AsyncSessionLocal
from app.services.analysis import analyse_pull_request
from app.api.sse import publish_pr_event

logger = logging.getLogger(__name__)


async def analyse_pr(ctx: dict[str, Any], pr_db_id: str) -> dict:
    """
    ARQ task: run the full analysis pipeline for one pull request.

    SSE events published:
      1. status_change  (analyzing)   — immediately when job starts
      2. analysis_complete            — when pipeline finishes successfully
      3. analysis_failed              — if pipeline raises an exception
    """
    logger.info("▶ Worker picked up PR db_id=%s", pr_db_id)

    async with AsyncSessionLocal() as db:
        pr = (await db.execute(
            select(PullRequest).where(PullRequest.id == pr_db_id)
        )).scalar_one_or_none()

        if not pr:
            logger.error("PR db_id=%s not found", pr_db_id)
            return {"error": "record_not_found", "pr_db_id": pr_db_id}

        # ── ANALYZING ────────────────────────────────────────────────────────
        pr.status = PRStatus.ANALYZING
        await db.commit()
        await publish_pr_event(pr_db_id, "analyzing")
        logger.info("PR %s → analyzing", pr_db_id)

        try:
            analysis = await analyse_pull_request(
                repo_full_name=pr.repo_full_name,
                pr_number=pr.pr_number,
                head_sha=pr.head_sha,
                base_sha=pr.base_sha or "",
                pr_url=pr.pr_url or "",
                ollama_url=settings.OLLAMA_URL,
                model=settings.OLLAMA_MODEL,
                redis=ctx.get("redis"),
            )

            # ── COMPLETED ────────────────────────────────────────────────────
            pr.status = PRStatus.COMPLETED
            pr.analysis_result = analysis
            await db.commit()
            await publish_pr_event(pr_db_id, "completed", analysis)
            logger.info("✅ Analysis complete pr_id=%s severity=%s findings=%d",
                        pr_db_id, analysis.get("severity"), len(analysis.get("findings", [])))

        except Exception as exc:
            logger.error("❌ Analysis failed pr_id=%s: %s", pr_db_id, exc, exc_info=True)

            # ── FAILED ───────────────────────────────────────────────────────
            pr.status = PRStatus.FAILED
            pr.analysis_result = {"error": str(exc), "summary": f"Analysis failed: {exc}"}
            await db.commit()
            await publish_pr_event(pr_db_id, "failed", pr.analysis_result)

        final_status = pr.status.value

    logger.info("◀ Finished PR db_id=%s → %s", pr_db_id, final_status)
    return {"pr_db_id": pr_db_id, "status": final_status}


class WorkerSettings:
    functions      = [analyse_pr]
    redis_settings = REDIS_SETTINGS
    max_tries      = 3
    job_timeout    = 600
    keep_result    = 3600
