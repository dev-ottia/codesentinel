"""
Tests for the Ollama LLM client.

All HTTP calls are mocked — no real Ollama instance needed.
Tests cover: happy path, JSON parsing, fallback when Ollama is down,
timeout handling, malformed responses, risk score clamping.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from app.services.llm_client import (
    run_llm_analysis,
    LLMResult,
    _parse_llm_response,
    _build_prompt,
    _truncate_diff,
    _clamp,
    build_diff_text,
)
from app.services.github_client import ChangedFile


# ── Fixtures ──────────────────────────────────────────────────────────────────

OLLAMA_URL = "http://localhost:11434"
MODEL      = "qwen2.5:3b"

_VALID_LLM_RESPONSE = {
    "summary":            "This PR adds user authentication. One high-severity SQL injection risk detected.",
    "risk_score":         75,
    "suggestions": [
        {
            "filename":   "app/auth.py",
            "line":       12,
            "severity":   "high",
            "issue":      "SQL query built with string concatenation — SQL injection risk.",
            "suggestion": "Use parameterised queries: cursor.execute('SELECT * FROM users WHERE id = %s', (user_id,))",
            "fix_diff":   "-query = f'SELECT * FROM users WHERE id = {user_id}'\n+cursor.execute('SELECT * FROM users WHERE id = %s', (user_id,))",
        }
    ],
    "security_notes":      ["No CSRF protection on the login endpoint."],
    "code_quality_notes":  ["Consider extracting auth logic into a service layer."],
}


def _ollama_tags_response() -> httpx.Response:
    return httpx.Response(200, json={"models": [{"name": MODEL}]})


def _ollama_generate_response(content: dict) -> httpx.Response:
    return httpx.Response(200, json={"response": json.dumps(content)})


# ── Happy path ────────────────────────────────────────────────────────────────

@respx.mock
async def test_run_llm_analysis_success():
    """Full happy-path: Ollama available, returns structured JSON."""
    respx.get(f"{OLLAMA_URL}/api/tags").mock(return_value=_ollama_tags_response())
    respx.post(f"{OLLAMA_URL}/api/generate").mock(
        return_value=_ollama_generate_response(_VALID_LLM_RESPONSE)
    )

    result = await run_llm_analysis(
        diff_text="--- app/auth.py\n+++ app/auth.py\n+query = f'SELECT * FROM users WHERE id = {user_id}'",
        static_findings=[],
        repo_full_name="org/repo",
        pr_number="42",
        ollama_url=OLLAMA_URL,
        model=MODEL,
    )

    assert isinstance(result, LLMResult)
    assert result.skipped is False
    assert result.risk_score == 75
    assert len(result.suggestions) == 1
    assert result.suggestions[0].severity == "high"
    assert result.suggestions[0].filename == "app/auth.py"
    assert len(result.security_notes) == 1
    assert result.error is None


@respx.mock
async def test_run_llm_analysis_no_findings():
    """LLM returns empty suggestions when no issues found."""
    clean_response = {
        "summary": "This PR looks clean. No security issues detected.",
        "risk_score": 5,
        "suggestions": [],
        "security_notes": [],
        "code_quality_notes": ["Minor: add docstrings to public functions."],
    }
    respx.get(f"{OLLAMA_URL}/api/tags").mock(return_value=_ollama_tags_response())
    respx.post(f"{OLLAMA_URL}/api/generate").mock(
        return_value=_ollama_generate_response(clean_response)
    )

    result = await run_llm_analysis(
        diff_text="+x = 1",
        static_findings=[],
        repo_full_name="org/repo",
        pr_number="1",
        ollama_url=OLLAMA_URL,
        model=MODEL,
    )

    assert result.risk_score == 5
    assert result.suggestions == []
    assert result.skipped is False


# ── Graceful degradation ──────────────────────────────────────────────────────

@respx.mock
async def test_skips_when_ollama_unavailable():
    """When Ollama is unreachable, return skipped=True without crashing."""
    respx.get(f"{OLLAMA_URL}/api/tags").mock(
        side_effect=httpx.ConnectError("Connection refused")
    )

    result = await run_llm_analysis(
        diff_text="+x = 1",
        static_findings=[],
        repo_full_name="org/repo",
        pr_number="1",
        ollama_url=OLLAMA_URL,
        model=MODEL,
    )

    assert result.skipped is True
    assert result.risk_score == 0
    assert "not available" in result.summary.lower()


@respx.mock
async def test_skips_on_timeout():
    """Timeout returns skipped result, not an exception."""
    respx.get(f"{OLLAMA_URL}/api/tags").mock(return_value=_ollama_tags_response())
    respx.post(f"{OLLAMA_URL}/api/generate").mock(
        side_effect=httpx.TimeoutException("timed out")
    )

    result = await run_llm_analysis(
        diff_text="+x = 1",
        static_findings=[],
        repo_full_name="org/repo",
        pr_number="1",
        ollama_url=OLLAMA_URL,
        model=MODEL,
        timeout=30.0,
    )

    assert result.skipped is True
    assert "timed out" in result.summary.lower()


# ── JSON parsing ──────────────────────────────────────────────────────────────

def test_parse_llm_response_clean_json():
    raw = json.dumps(_VALID_LLM_RESPONSE)
    parsed, err = _parse_llm_response(raw)
    assert err is None
    assert parsed["risk_score"] == 75


def test_parse_llm_response_strips_code_fences():
    """LLMs often wrap JSON in markdown code fences."""
    raw = f"```json\n{json.dumps(_VALID_LLM_RESPONSE)}\n```"
    parsed, err = _parse_llm_response(raw)
    assert err is None
    assert parsed["risk_score"] == 75


def test_parse_llm_response_extracts_from_preamble():
    """Handle models that add explanatory text before the JSON."""
    raw = f"Here is my analysis:\n\n{json.dumps(_VALID_LLM_RESPONSE)}\n\nDone."
    parsed, err = _parse_llm_response(raw)
    assert err is None
    assert "summary" in parsed


def test_parse_llm_response_empty():
    parsed, err = _parse_llm_response("")
    assert err is not None
    assert parsed == {}


def test_parse_llm_response_invalid_json():
    parsed, err = _parse_llm_response("{not valid json}")
    assert err is not None


# ── Utilities ─────────────────────────────────────────────────────────────────

def test_clamp_within_range():
    assert _clamp(50, 0, 100) == 50


def test_clamp_below_min():
    assert _clamp(-10, 0, 100) == 0


def test_clamp_above_max():
    assert _clamp(150, 0, 100) == 100


def test_clamp_invalid_type():
    assert _clamp("bad", 0, 100) == 0


def test_truncate_diff_short():
    diff = "+x = 1\n-y = 2"
    assert _truncate_diff(diff) == diff


def test_truncate_diff_long():
    diff = "+" + "x" * 20_000
    result = _truncate_diff(diff)
    assert "truncated" in result
    assert len(result) < len(diff)


def test_build_diff_text():
    files = [
        ChangedFile(
            filename="app/main.py", status="modified",
            additions=1, deletions=0, changes=1,
            patch="@@ -1 +1 @@\n+x = 1",
            raw_url="", blob_url="",
        ),
        ChangedFile(
            filename="app/utils.py", status="added",
            additions=2, deletions=0, changes=2,
            patch="@@ -0,0 +1,2 @@\n+def foo():\n+    pass",
            raw_url="", blob_url="",
        ),
    ]
    text = build_diff_text(files)
    assert "app/main.py" in text
    assert "app/utils.py" in text
    assert "+x = 1" in text


def test_build_diff_text_skips_empty_patches():
    files = [
        ChangedFile(
            filename="image.png", status="added",
            additions=0, deletions=0, changes=0,
            patch="",  # binary file — no patch
            raw_url="", blob_url="",
        ),
    ]
    text = build_diff_text(files)
    assert text == ""


def test_build_prompt_contains_required_sections():
    prompt = _build_prompt(
        diff_text="+x = eval(user_input)",
        static_findings=[{
            "severity": "high",
            "rule_id": "eval-detected",
            "filename": "app/main.py",
            "line_start": 1,
            "message": "Use of eval is dangerous",
        }],
        repo_full_name="org/repo",
        pr_number="42",
    )
    assert "org/repo" in prompt
    assert "#42" in prompt
    assert "eval-detected" in prompt
    assert "JSON" in prompt
    assert "risk_score" in prompt


# ── to_dict output ────────────────────────────────────────────────────────────

@respx.mock
async def test_llm_result_to_dict_structure():
    respx.get(f"{OLLAMA_URL}/api/tags").mock(return_value=_ollama_tags_response())
    respx.post(f"{OLLAMA_URL}/api/generate").mock(
        return_value=_ollama_generate_response(_VALID_LLM_RESPONSE)
    )

    result = await run_llm_analysis(
        diff_text="+x = 1",
        static_findings=[],
        repo_full_name="org/repo",
        pr_number="1",
        ollama_url=OLLAMA_URL,
        model=MODEL,
    )

    d = result.to_dict()
    assert "summary" in d
    assert "risk_score" in d
    assert "suggestions" in d
    assert "security_notes" in d
    assert "code_quality_notes" in d
    assert "duration_seconds" in d
    assert "skipped" in d
