"""
Tests for the static analysis engine.

All tool execution is mocked — no real semgrep or bandit installed needed.
Tests cover: patch extraction, JSON parsing, deduplication, severity mapping,
graceful degradation when tools are missing.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.github_client import PRDiff, ChangedFile
from app.services.static_analysis import (
    run_static_analysis,
    StaticAnalysisResult,
    Finding,
    _parse_semgrep,
    _parse_bandit,
    _deduplicate,
    _write_patches,
    _strip_tmpdir,
    _is_analysable,
    _map_semgrep_severity,
    _map_bandit_severity,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_diff(files: list[dict] | None = None) -> PRDiff:
    """Build a minimal PRDiff for testing."""
    if files is None:
        files = [
            {
                "filename": "app/auth.py",
                "status": "modified",
                "additions": 3,
                "deletions": 1,
                "changes": 4,
                "patch": "@@ -1,3 +1,4 @@\n+import os\n+password = os.getenv('SECRET')\n+eval(user_input)\n context line",
                "raw_url": "https://raw.githubusercontent.com/org/repo/abc/app/auth.py",
                "blob_url": "https://github.com/org/repo/blob/abc/app/auth.py",
            }
        ]
    return PRDiff(
        repo_full_name="org/repo",
        pr_number="1",
        head_sha="abc123",
        base_sha="def456",
        total_changes=sum(f["changes"] for f in files),
        files=[ChangedFile(**f) for f in files],
    )


def _semgrep_output(findings: list[dict] | None = None) -> dict:
    """Build a minimal semgrep JSON output."""
    if findings is None:
        findings = [
            {
                "check_id": "python.lang.security.audit.eval-detected.eval-detected",
                "path": "/tmp/codesentinel_test/app/auth.py",
                "start": {"line": 3, "col": 1},
                "end":   {"line": 3, "col": 20},
                "extra": {
                    "severity": "ERROR",
                    "message": "Use of eval() is dangerous",
                    "lines": "eval(user_input)",
                    "metadata": {
                        "cwe": ["CWE-78"],
                        "owasp": ["A03:2021"],
                        "message": "Replace eval() with safer alternatives.",
                    },
                },
            }
        ]
    return {"results": findings, "errors": []}


def _bandit_output(findings: list[dict] | None = None) -> dict:
    if findings is None:
        findings = [
            {
                "test_id": "B102",
                "test_name": "exec_used",
                "issue_severity": "HIGH",
                "issue_confidence": "HIGH",
                "issue_text": "Use of exec detected.",
                "filename": "/tmp/codesentinel_test/app/auth.py",
                "line_number": 5,
                "code": "exec(cmd)",
                "issue_cwe": {"id": "78", "link": ""},
                "more_info": "https://bandit.readthedocs.io/en/latest/plugins/b102_exec_used.html",
            }
        ]
    return {"results": findings, "errors": []}


# ── Unit tests — parsing ──────────────────────────────────────────────────────

def test_parse_semgrep_extracts_findings():
    raw = _semgrep_output()
    findings = _parse_semgrep(raw, "/tmp/codesentinel_test")
    assert len(findings) == 1
    f = findings[0]
    assert f.tool == "semgrep"
    assert f.rule_id == "python.lang.security.audit.eval-detected.eval-detected"
    assert f.severity == "high"
    assert f.line_start == 3
    assert "eval" in f.message.lower()
    assert "CWE-78" in f.cwe


def test_parse_bandit_extracts_findings():
    raw = _bandit_output()
    findings = _parse_bandit(raw, "/tmp/codesentinel_test")
    assert len(findings) == 1
    f = findings[0]
    assert f.tool == "bandit"
    assert f.rule_id == "B102"
    assert f.severity == "high"
    assert f.confidence == "HIGH"
    assert "CWE-78" in f.cwe


def test_parse_semgrep_empty():
    assert _parse_semgrep({"results": [], "errors": []}, "/tmp") == []


def test_parse_bandit_empty():
    assert _parse_bandit({"results": [], "errors": []}, "/tmp") == []


# ── Unit tests — deduplication ────────────────────────────────────────────────

def test_deduplicate_removes_exact_duplicates():
    f = Finding(
        tool="semgrep", rule_id="r1", severity="high", confidence="HIGH",
        message="msg", filename="app/auth.py", line_start=3, line_end=3,
        code="eval(x)", cwe=[], owasp=[], fix_advice="",
    )
    result = _deduplicate([f, f, f])
    assert len(result) == 1


def test_deduplicate_keeps_different_lines():
    def make(line: int) -> Finding:
        return Finding(
            tool="semgrep", rule_id="r1", severity="medium", confidence="HIGH",
            message="msg", filename="app/auth.py", line_start=line, line_end=line,
            code="x", cwe=[], owasp=[], fix_advice="",
        )
    result = _deduplicate([make(1), make(2), make(3)])
    assert len(result) == 3


def test_deduplicate_sorts_by_severity():
    findings = [
        Finding("semgrep", "r1", "low",    "HIGH", "msg", "f.py", 1, 1, "", [], [], ""),
        Finding("semgrep", "r2", "high",   "HIGH", "msg", "f.py", 2, 2, "", [], [], ""),
        Finding("semgrep", "r3", "medium", "HIGH", "msg", "f.py", 3, 3, "", [], [], ""),
    ]
    result = _deduplicate(findings)
    assert result[0].severity == "high"
    assert result[1].severity == "medium"
    assert result[2].severity == "low"


# ── Unit tests — helpers ──────────────────────────────────────────────────────

def test_is_analysable_python():
    assert _is_analysable("app/main.py") is True


def test_is_analysable_skips_images():
    assert _is_analysable("assets/logo.png") is False


def test_is_analysable_skips_node_modules():
    assert _is_analysable("node_modules/lodash/index.js") is False


def test_is_analysable_skips_lock_files():
    assert _is_analysable("package-lock.json") is True   # JSON is analysable
    assert _is_analysable("poetry.lock") is False


def test_map_semgrep_severity():
    assert _map_semgrep_severity("ERROR")   == "high"
    assert _map_semgrep_severity("WARNING") == "medium"
    assert _map_semgrep_severity("INFO")    == "info"
    assert _map_semgrep_severity("UNKNOWN") == "unknown"


def test_map_bandit_severity():
    assert _map_bandit_severity("HIGH")   == "high"
    assert _map_bandit_severity("MEDIUM") == "medium"
    assert _map_bandit_severity("LOW")    == "low"


def test_write_patches_extracts_added_lines(tmp_path):
    files = [
        ChangedFile(
            filename="app/utils.py",
            status="modified",
            additions=2, deletions=1, changes=3,
            patch="@@ -1,2 +1,3 @@\n context\n+new_line_1\n+new_line_2\n-removed",
            raw_url="", blob_url="",
        )
    ]
    written = _write_patches(str(tmp_path), files)
    assert len(written) == 1
    content = Path(written[0]).read_text()
    assert "new_line_1" in content
    assert "new_line_2" in content
    assert "removed" not in content
    assert "context" not in content


# ── Integration tests — full pipeline with mocked subprocesses ────────────────

async def test_run_static_analysis_with_mocked_tools():
    """Full pipeline with mocked semgrep + bandit output."""
    diff = _make_diff()

    with patch("app.services.static_analysis.shutil.which", return_value="/usr/bin/tool"), \
         patch("app.services.static_analysis._run_subprocess") as mock_run:

        mock_run.side_effect = [
            _semgrep_output(),   # first call = semgrep
            _bandit_output(),    # second call = bandit
        ]

        result = await run_static_analysis(diff)

    assert isinstance(result, StaticAnalysisResult)
    assert len(result.findings) == 2      # 1 semgrep + 1 bandit
    assert "semgrep" in result.tools_run
    assert "bandit" in result.tools_run
    assert result.highest_severity == "high"
    assert result.files_analysed == 1


async def test_run_static_analysis_graceful_when_semgrep_missing():
    """When semgrep is missing, pipeline continues with bandit only."""
    diff = _make_diff()

    def which_side_effect(tool: str) -> str | None:
        return None if tool == "semgrep" else "/usr/bin/bandit"

    with patch("app.services.static_analysis.shutil.which", side_effect=which_side_effect), \
         patch("app.services.static_analysis._run_subprocess", return_value=_bandit_output()):

        result = await run_static_analysis(diff)

    assert "semgrep" in result.tools_failed
    assert "bandit" in result.tools_run
    assert len(result.findings) == 1


async def test_run_static_analysis_no_analysable_files():
    """Binary-only diff → skip analysis, return empty result."""
    diff = _make_diff(files=[
        {
            "filename": "assets/image.png",
            "status": "added",
            "additions": 0, "deletions": 0, "changes": 0,
            "patch": "",
            "raw_url": "", "blob_url": "",
        }
    ])
    result = await run_static_analysis(diff)
    assert result.files_analysed == 0
    assert result.findings == []


async def test_static_result_to_dict_structure():
    """to_dict() returns expected keys."""
    diff = _make_diff()

    with patch("app.services.static_analysis.shutil.which", return_value="/usr/bin/tool"), \
         patch("app.services.static_analysis._run_subprocess") as mock_run:
        mock_run.side_effect = [_semgrep_output(), _bandit_output()]
        result = await run_static_analysis(diff)

    d = result.to_dict()
    assert "findings" in d
    assert "tools_run" in d
    assert "highest_severity" in d
    assert "by_severity" in d
    assert "by_file" in d
    assert "total_findings" in d
    assert d["total_findings"] == 2
