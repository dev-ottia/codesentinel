"""
Analysis service — orchestrates the full PR review pipeline.

Pipeline:
  ✅ Phase 1.3: fetch_diff()    — GitHub API client with Redis cache
  ✅ Phase 2.1: run_static()    — semgrep + bandit
  ✅ Phase 2.3: run_llm()       — Ollama structured JSON output
  ✅ Phase 2.4: merge/rank      — unified findings, risk score, dedup
"""
from __future__ import annotations

import logging
from typing import Any

from app.services.github_client import (
    GitHubClient,
    GitHubAPIError,
    GitHubAuthError,
    GitHubNotFoundError,
    PRDiff,
)
from app.services.static_analysis import (
    run_static_analysis,
    StaticAnalysisResult,
    SEVERITY_RANK,
)
from app.services.llm_client import run_llm_analysis, build_diff_text, LLMResult

logger = logging.getLogger(__name__)


async def analyse_pull_request(
    repo_full_name: str,
    pr_number: str,
    head_sha: str,
    base_sha: str,
    pr_url: str,
    ollama_url: str,
    model: str,
    redis: Any = None,
) -> dict:
    """
    Orchestrate the full analysis pipeline for one pull request.

    Returns a dict stored as PullRequest.analysis_result.
    All steps are individually fault-tolerant — a failure in one step
    never prevents the others from running.
    """
    logger.info("Starting analysis: %s#%s head=%s", repo_full_name, pr_number, head_sha[:7])

    # ── Step 1: Fetch diff ────────────────────────────────────────────────────
    diff: PRDiff | None = None
    diff_error: str | None = None

    try:
        async with GitHubClient(redis=redis) as gh:
            diff = await gh.get_pr_diff(
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                head_sha=head_sha,
                base_sha=base_sha,
            )
        logger.info(
            "Diff fetched: %d files, %d changes (cached=%s)",
            len(diff.files), diff.total_changes, diff.from_cache,
        )
    except GitHubAuthError as exc:
        diff_error = f"Auth error: {exc}"
        logger.error(diff_error)
    except GitHubNotFoundError as exc:
        diff_error = f"Not found: {exc}"
        logger.error(diff_error)
    except GitHubAPIError as exc:
        diff_error = f"API error: {exc}"
        logger.error(diff_error)
    except Exception as exc:
        diff_error = f"Unexpected: {exc}"
        logger.error(diff_error, exc_info=True)

    diff_stats = _build_diff_stats(diff) if diff else {}

    # ── Step 2: Static analysis ───────────────────────────────────────────────
    static: StaticAnalysisResult | None = None

    if diff:
        try:
            static = await run_static_analysis(diff)
            logger.info(
                "Static: %d findings, severity=%s, tools=%s",
                len(static.findings), static.highest_severity, static.tools_run,
            )
        except Exception as exc:
            logger.error("Static analysis failed: %s", exc, exc_info=True)

    static_dict = static.to_dict() if static else {
        "findings": [], "tools_run": [], "tools_failed": [],
        "files_analysed": 0, "error": "diff unavailable" if not diff else "static analysis error",
    }

    # ── Step 3: LLM analysis ──────────────────────────────────────────────────
    llm: LLMResult | None = None

    if diff:
        try:
            diff_text      = build_diff_text(diff.files)
            static_findings = static_dict.get("findings", [])

            llm = await run_llm_analysis(
                diff_text       = diff_text,
                static_findings = static_findings,
                repo_full_name  = repo_full_name,
                pr_number       = pr_number,
                ollama_url      = ollama_url,
                model           = model,
            )
            logger.info(
                "LLM: risk_score=%d, %d suggestions, skipped=%s",
                llm.risk_score, len(llm.suggestions), llm.skipped,
            )
        except Exception as exc:
            logger.error("LLM analysis failed: %s", exc, exc_info=True)

    llm_dict = llm.to_dict() if llm else {
        "summary": "AI analysis not available.",
        "risk_score": 0,
        "suggestions": [],
        "security_notes": [],
        "code_quality_notes": [],
        "skipped": True,
        "error": "diff unavailable" if not diff else None,
    }

    # ── Step 4: Merge and rank findings ───────────────────────────────────────
    # Combine static findings with LLM suggestions into one unified list,
    # deduplicated and sorted by severity.
    all_findings = _merge_findings(
        static_findings = static_dict.get("findings", []),
        llm_suggestions = llm_dict.get("suggestions", []),
    )

    # Overall severity: max of static + LLM risk
    static_sev  = static_dict.get("highest_severity", "unknown")
    llm_risk    = llm_dict.get("risk_score", 0)
    overall_sev = _compute_overall_severity(static_sev, llm_risk)

    summary = _build_summary(diff, diff_error, static, llm)

    logger.info(
        "Analysis complete: %s#%s — severity=%s, risk=%d, findings=%d",
        repo_full_name, pr_number, overall_sev, llm_risk, len(all_findings),
    )

    return {
        "summary":      summary,
        "findings":     all_findings,
        "severity":     overall_sev,
        "risk_score":   llm_risk,
        "suggestions":  llm_dict.get("suggestions", []),
        "static":       static_dict,
        "ai":           llm_dict,
        "model_used":   model,
        "diff_fetched": diff is not None,
        "diff_error":   diff_error,
        "diff_stats":   diff_stats,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _merge_findings(
    static_findings: list[dict],
    llm_suggestions: list[dict],
) -> list[dict]:
    """
    Combine static + LLM findings into one sorted, deduplicated list.

    Deduplication key: (filename, line) — if both tools flag the same location,
    prefer the static finding (more precise) and attach the LLM suggestion text.
    """
    merged: dict[tuple, dict] = {}

    for f in static_findings:
        key = (f.get("filename", ""), f.get("line_start", 0))
        merged[key] = {**f, "source": "static"}

    for s in llm_suggestions:
        key = (s.get("filename", ""), s.get("line", 0))
        if key in merged:
            # Enrich existing static finding with LLM suggestion
            merged[key]["llm_suggestion"] = s.get("suggestion", "")
            merged[key]["fix_diff"]       = s.get("fix_diff", "")
        else:
            merged[key] = {
                "source":       "ai",
                "filename":     s.get("filename", ""),
                "line_start":   s.get("line", 0),
                "line_end":     s.get("line", 0),
                "severity":     s.get("severity", "info"),
                "message":      s.get("issue", ""),
                "llm_suggestion": s.get("suggestion", ""),
                "fix_diff":     s.get("fix_diff", ""),
                "tool":         "llm",
                "rule_id":      "ai-review",
                "cwe":          [],
                "owasp":        [],
            }

    result = list(merged.values())
    result.sort(
        key=lambda f: SEVERITY_RANK.get(f.get("severity", "unknown"), 0),
        reverse=True,
    )
    return result


def _compute_overall_severity(static_sev: str, llm_risk: int) -> str:
    """Derive the overall severity from static analysis + LLM risk score."""
    # Map LLM risk score (0-100) to severity
    if llm_risk >= 80:
        llm_sev = "critical"
    elif llm_risk >= 60:
        llm_sev = "high"
    elif llm_risk >= 40:
        llm_sev = "medium"
    elif llm_risk >= 20:
        llm_sev = "low"
    else:
        llm_sev = "info"

    # Take the higher of static vs LLM severity
    static_rank = SEVERITY_RANK.get(static_sev, 0)
    llm_rank    = SEVERITY_RANK.get(llm_sev, 0)
    return static_sev if static_rank >= llm_rank else llm_sev


def _build_diff_stats(diff: PRDiff) -> dict:
    by_status: dict[str, int] = {}
    for f in diff.files:
        by_status[f.status] = by_status.get(f.status, 0) + 1
    return {
        "total_files":     len(diff.files),
        "total_changes":   diff.total_changes,
        "total_additions": sum(f.additions for f in diff.files),
        "total_deletions": sum(f.deletions for f in diff.files),
        "by_status":       by_status,
        "truncated":       diff.truncated,
        "from_cache":      diff.from_cache,
        "file_list": [
            {
                "filename":  f.filename,
                "status":    f.status,
                "additions": f.additions,
                "deletions": f.deletions,
                "patch":     f.patch,        # ← ADD THIS
                "has_patch": bool(f.patch),
            }
            for f in diff.files
        ],
    }


def _build_summary(
    diff: PRDiff | None,
    diff_error: str | None,
    static: StaticAnalysisResult | None,
    llm: LLMResult | None,
) -> str:
    if diff_error:
        return f"Diff fetch failed: {diff_error}. Analysis skipped."
    if not diff:
        return "No diff available."

    # Prefer LLM summary if available and not skipped
    if llm and not llm.skipped and llm.summary:
        return llm.summary

    parts = [
        f"Analysed {len(diff.files)} changed file(s) "
        f"(+{sum(f.additions for f in diff.files)} "
        f"-{sum(f.deletions for f in diff.files)})."
    ]
    if static:
        if static.findings:
            parts.append(
                f"Static analysis: {len(static.findings)} issue(s) "
                f"(highest: {static.highest_severity})."
            )
        else:
            parts.append("Static analysis: no issues found.")
    if llm and llm.skipped:
        parts.append("AI analysis unavailable (Ollama offline).")

    return " ".join(parts)
