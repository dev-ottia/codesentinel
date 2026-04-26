"""
Ollama LLM client for CodeSentinel.

Responsibilities:
  - Send PR diff + static findings to a local Ollama model
  - Enforce structured JSON output via a strict prompt schema
  - Timeout handling (configurable, default 120s)
  - Graceful fallback when Ollama is unavailable
  - Token budget management (truncate large diffs to fit context window)

Supported models (tested):
  - qwen2.5:3b     (fast, ~2GB RAM, good for small diffs)
  - qwen2.5:7b     (better quality, ~4GB RAM)
  - llama3.1:8b    (best quality, ~5GB RAM)
  - codellama:7b   (code-focused)

Output schema (JSON):
{
  "summary": str,                    -- 2-3 sentence executive summary
  "risk_score": int,                 -- 0-100 overall risk score
  "suggestions": [                   -- ordered by priority
    {
      "filename": str,
      "line": int,
      "severity": "critical"|"high"|"medium"|"low"|"info",
      "issue": str,                  -- what the problem is
      "suggestion": str,             -- specific fix recommendation
      "fix_diff": str,               -- optional unified diff of the fix
    }
  ],
  "security_notes": [str],           -- general security observations
  "code_quality_notes": [str],       -- non-security improvement suggestions
}
"""
from __future__ import annotations

import json
import logging
import textwrap
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_DEFAULT_TIMEOUT   = 120.0   # seconds — LLM inference can be slow on CPU
_MAX_DIFF_CHARS    = 12_000  # truncate diff sent to LLM to stay within context
_MAX_FINDINGS_SENT = 10      # send at most 10 static findings to avoid prompt bloat


# ── Types ─────────────────────────────────────────────────────────────────────

@dataclass
class LLMSuggestion:
    filename:   str
    line:       int
    severity:   str
    issue:      str
    suggestion: str
    fix_diff:   str = ""


@dataclass
class LLMResult:
    summary:             str
    risk_score:          int                          # 0-100
    suggestions:         list[LLMSuggestion]          = field(default_factory=list)
    security_notes:      list[str]                    = field(default_factory=list)
    code_quality_notes:  list[str]                    = field(default_factory=list)
    model:               str                          = ""
    duration_seconds:    float                        = 0.0
    error:               str | None                   = None
    skipped:             bool                         = False   # True if Ollama unavailable

    def to_dict(self) -> dict:
        return {
            "summary":            self.summary,
            "risk_score":         self.risk_score,
            "suggestions":        [
                {
                    "filename":   s.filename,
                    "line":       s.line,
                    "severity":   s.severity,
                    "issue":      s.issue,
                    "suggestion": s.suggestion,
                    "fix_diff":   s.fix_diff,
                }
                for s in self.suggestions
            ],
            "security_notes":     self.security_notes,
            "code_quality_notes": self.code_quality_notes,
            "model":              self.model,
            "duration_seconds":   round(self.duration_seconds, 2),
            "error":              self.error,
            "skipped":            self.skipped,
        }


# ── Public entry point ────────────────────────────────────────────────────────

async def run_llm_analysis(
    diff_text: str,
    static_findings: list[dict],
    repo_full_name: str,
    pr_number: str,
    ollama_url: str,
    model: str,
    timeout: float = _DEFAULT_TIMEOUT,
) -> LLMResult:
    """
    Send the PR diff and static findings to Ollama and parse structured output.

    Gracefully degrades if Ollama is unreachable — returns a skipped result
    so the pipeline continues with static-only analysis.

    Args:
        diff_text:        Combined unified diff of all changed files.
        static_findings:  List of Finding dicts from static_analysis.
        repo_full_name:   "owner/repo"
        pr_number:        PR number string.
        ollama_url:       Ollama base URL (e.g. http://localhost:11434).
        model:            Model name (e.g. "qwen2.5:3b").
        timeout:          HTTP timeout in seconds.

    Returns:
        LLMResult with structured suggestions and notes.
    """
    import time
    start = time.monotonic()

    # ── Check Ollama availability ─────────────────────────────────────────────
    if not await _is_ollama_available(ollama_url, timeout=5.0):
        logger.warning("Ollama not reachable at %s — skipping LLM analysis", ollama_url)
        return LLMResult(
            summary="AI analysis skipped — Ollama not available.",
            risk_score=0,
            model=model,
            skipped=True,
            duration_seconds=time.monotonic() - start,
        )

    # ── Build prompt ──────────────────────────────────────────────────────────
    prompt = _build_prompt(diff_text, static_findings, repo_full_name, pr_number)
    logger.info(
        "Sending prompt to Ollama (%s) — %d chars, %d static findings",
        model, len(prompt), len(static_findings),
    )

    # ── Call Ollama ───────────────────────────────────────────────────────────
    raw_response: str = ""
    try:
        raw_response = await _call_ollama(
            ollama_url=ollama_url,
            model=model,
            prompt=prompt,
            timeout=timeout,
        )
    except httpx.TimeoutException:
        error = f"Ollama timed out after {timeout}s"
        logger.warning(error)
        return LLMResult(
            summary="AI analysis timed out.",
            risk_score=0,
            model=model,
            error=error,
            skipped=True,
            duration_seconds=time.monotonic() - start,
        )
    except Exception as exc:
        error = f"Ollama request failed: {exc}"
        logger.error(error)
        return LLMResult(
            summary="AI analysis failed.",
            risk_score=0,
            model=model,
            error=error,
            skipped=True,
            duration_seconds=time.monotonic() - start,
        )

    # ── Parse JSON response ───────────────────────────────────────────────────
    duration = time.monotonic() - start
    logger.info("Ollama responded in %.1fs", duration)

    parsed, parse_error = _parse_llm_response(raw_response)
    if parse_error:
        logger.warning("LLM JSON parse failed: %s — raw: %s...", parse_error, raw_response[:200])

    result = LLMResult(
        summary            = parsed.get("summary", "No summary provided."),
        risk_score         = _clamp(parsed.get("risk_score", 0), 0, 100),
        suggestions        = [
            LLMSuggestion(
                filename   = s.get("filename", ""),
                line       = int(s.get("line", 0)),
                severity   = s.get("severity", "info"),
                issue      = s.get("issue", ""),
                suggestion = s.get("suggestion", ""),
                fix_diff   = s.get("fix_diff", ""),
            )
            for s in parsed.get("suggestions", [])
            if isinstance(s, dict)
        ],
        security_notes     = parsed.get("security_notes", []),
        code_quality_notes = parsed.get("code_quality_notes", []),
        model              = model,
        duration_seconds   = duration,
        error              = parse_error,
    )

    logger.info(
        "LLM analysis complete: risk_score=%d, %d suggestions",
        result.risk_score, len(result.suggestions),
    )
    return result


# ── Prompt engineering ────────────────────────────────────────────────────────

def _build_prompt(
    diff_text: str,
    static_findings: list[dict],
    repo_full_name: str,
    pr_number: str,
) -> str:
    """
    Build a structured prompt that instructs the LLM to return valid JSON only.

    Key design decisions:
    - JSON-only instruction repeated 3x — models comply more reliably.
    - Static findings provided as context so LLM can reference them.
    - Diff truncated to _MAX_DIFF_CHARS to fit within context window.
    - Temperature is set to 0 in the API call for deterministic output.
    """
    truncated_diff = _truncate_diff(diff_text)
    findings_text  = _format_findings_for_prompt(static_findings[:_MAX_FINDINGS_SENT])

    return textwrap.dedent(f"""
        You are CodeSentinel, an expert security engineer reviewing a GitHub pull request.
        Your task: analyse the diff and static analysis findings, then respond with ONLY
        valid JSON — no markdown, no explanation, no code fences, just the JSON object.

        PULL REQUEST: {repo_full_name} #{pr_number}

        STATIC ANALYSIS FINDINGS (from semgrep + bandit):
        {findings_text}

        DIFF (changed lines only, + = added, - = removed):
        {truncated_diff}

        Respond with ONLY this JSON structure (no other text):
        {{
          "summary": "2-3 sentence executive summary of the PR's security and quality posture",
          "risk_score": <integer 0-100, where 0=no risk, 100=critical>,
          "suggestions": [
            {{
              "filename": "<file path>",
              "line": <line number>,
              "severity": "<critical|high|medium|low|info>",
              "issue": "<concise description of the problem>",
              "suggestion": "<specific actionable fix recommendation>",
              "fix_diff": "<optional: unified diff showing the fix>"
            }}
          ],
          "security_notes": ["<general security observation>"],
          "code_quality_notes": ["<non-security improvement suggestion>"]
        }}

        Rules:
        - Output ONLY the JSON object. No markdown. No explanation.
        - risk_score must be an integer between 0 and 100.
        - severity must be one of: critical, high, medium, low, info.
        - If no issues found, return empty arrays and risk_score 0.
        - Focus on security vulnerabilities, not style issues.
    """).strip()


def _truncate_diff(diff_text: str) -> str:
    """Truncate diff to fit within LLM context window."""
    if len(diff_text) <= _MAX_DIFF_CHARS:
        return diff_text
    truncated = diff_text[:_MAX_DIFF_CHARS]
    return truncated + f"\n\n[... diff truncated at {_MAX_DIFF_CHARS} chars ...]"


def _format_findings_for_prompt(findings: list[dict]) -> str:
    """Format static findings as a numbered list for the prompt."""
    if not findings:
        return "None"
    lines = []
    for i, f in enumerate(findings, 1):
        lines.append(
            f"{i}. [{f.get('severity','?').upper()}] {f.get('rule_id','?')} "
            f"in {f.get('filename','?')}:{f.get('line_start','?')} — {f.get('message','')}"
        )
    return "\n".join(lines)


# ── Ollama HTTP calls ─────────────────────────────────────────────────────────

async def _is_ollama_available(ollama_url: str, timeout: float = 5.0) -> bool:
    """Ping Ollama's /api/tags endpoint to check availability."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{ollama_url.rstrip('/')}/api/tags")
            return resp.status_code == 200
    except Exception:
        return False


async def _call_ollama(
    ollama_url: str,
    model: str,
    prompt: str,
    timeout: float,
) -> str:
    """
    Call Ollama's /api/generate endpoint and return the response text.

    Uses stream=False for simplicity — waits for the full response.
    Temperature=0 for deterministic JSON output.
    """
    url = f"{ollama_url.rstrip('/')}/api/generate"
    payload = {
        "model":   model,
        "prompt":  prompt,
        "stream":  False,
        "format":  "json",       # Ollama's JSON mode — forces valid JSON output
        "options": {
            "temperature": 0.1,  # near-deterministic
            "top_p":       0.9,
            "num_ctx":     4096, # context window
        },
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data.get("response", "")


# ── Response parsing ──────────────────────────────────────────────────────────

def _parse_llm_response(raw: str) -> tuple[dict, str | None]:
    """
    Parse the LLM's response into a structured dict.

    Handles common model quirks:
    - Response wrapped in markdown code fences (```json ... ```)
    - Leading/trailing whitespace
    - Extra text before/after the JSON object

    Returns (parsed_dict, error_message_or_None).
    """
    if not raw or not raw.strip():
        return {}, "Empty response from LLM"

    text = raw.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(
            line for line in lines
            if not line.strip().startswith("```")
        ).strip()

    # Find the first { and last } to extract the JSON object
    start = text.find("{")
    end   = text.rfind("}")
    if start == -1 or end == -1:
        return {}, f"No JSON object found in response: {text[:200]}"

    json_str = text[start : end + 1]

    try:
        parsed = json.loads(json_str)
        return parsed, None
    except json.JSONDecodeError as exc:
        return {}, f"JSON parse error: {exc}"


# ── Utilities ─────────────────────────────────────────────────────────────────

def _clamp(value: Any, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return lo


def build_diff_text(files: list) -> str:
    """
    Combine patch text from all ChangedFile objects into a single diff string.
    Called by the analysis pipeline before passing to the LLM.
    """
    parts: list[str] = []
    for f in files:
        if f.patch:
            parts.append(f"--- {f.filename}\n+++ {f.filename}\n{f.patch}")
    return "\n\n".join(parts)
