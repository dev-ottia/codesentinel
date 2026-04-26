"""
Static analysis engine — semgrep + bandit.

Strategy:
  1. Extract changed file patches from the PRDiff into a temp directory.
  2. Run semgrep (rules: p/python, p/security-audit, p/owasp-top-ten) with --json.
  3. Run bandit (-r, --format json) on the same files.
  4. Parse both outputs into a unified Finding schema.
  5. Deduplicate findings by (tool, file, line, rule_id).
  6. Return structured results including per-file breakdown.

Graceful degradation:
  - If semgrep is not installed → skip, log warning, return empty findings.
  - If bandit is not installed  → skip, log warning, return empty findings.
  - If a tool times out (>30s)  → skip that tool, log warning.
  - Individual tool failures never crash the pipeline.

Installation (inside Docker — add to Dockerfile):
  pip install semgrep bandit
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from app.services.github_client import PRDiff, ChangedFile

logger = logging.getLogger(__name__)

# ── Types ─────────────────────────────────────────────────────────────────────

Severity = Literal["critical", "high", "medium", "low", "info", "unknown"]

SEVERITY_RANK: dict[str, int] = {
    "critical": 5,
    "high":     4,
    "medium":   3,
    "low":      2,
    "info":     1,
    "unknown":  0,
}


@dataclass
class Finding:
    """A single security / quality finding from any analysis tool."""
    tool:        str            # "semgrep" | "bandit"
    rule_id:     str            # e.g. "python.lang.security.audit.hardcoded-password"
    severity:    Severity
    confidence:  str            # "HIGH" | "MEDIUM" | "LOW"
    message:     str
    filename:    str            # relative path within the repo
    line_start:  int
    line_end:    int
    code:        str            # snippet of offending code
    cwe:         list[str]      # e.g. ["CWE-798"]
    owasp:       list[str]      # e.g. ["A2:2021"]
    fix_advice:  str            # human-readable remediation hint


@dataclass
class StaticAnalysisResult:
    """Aggregated output of all static analysis tools."""
    findings:        list[Finding]     = field(default_factory=list)
    tools_run:       list[str]         = field(default_factory=list)
    tools_failed:    list[str]         = field(default_factory=list)
    files_analysed:  int               = 0
    duration_seconds: float            = 0.0
    error:           str | None        = None

    @property
    def highest_severity(self) -> Severity:
        if not self.findings:
            return "unknown"
        return max(self.findings, key=lambda f: SEVERITY_RANK[f.severity]).severity

    def to_dict(self) -> dict:
        return {
            "findings":          [_finding_to_dict(f) for f in self.findings],
            "tools_run":         self.tools_run,
            "tools_failed":      self.tools_failed,
            "files_analysed":    self.files_analysed,
            "duration_seconds":  round(self.duration_seconds, 2),
            "highest_severity":  self.highest_severity,
            "total_findings":    len(self.findings),
            "by_severity":       _count_by_severity(self.findings),
            "by_file":           _count_by_file(self.findings),
            "error":             self.error,
        }


# ── Public entry point ────────────────────────────────────────────────────────

async def run_static_analysis(diff: PRDiff) -> StaticAnalysisResult:
    """
    Run semgrep + bandit against the changed files in a PR diff.

    Writes patches to a temporary directory, runs tools, cleans up.
    Safe to call concurrently — each call gets its own tmpdir.
    """
    import time
    start = time.monotonic()

    # Only analyse files with patches (skip binary / empty diffs)
    analysable = [f for f in diff.files if f.patch and _is_analysable(f.filename)]

    if not analysable:
        logger.info("No analysable files in diff — skipping static analysis")
        return StaticAnalysisResult(
            files_analysed=0,
            duration_seconds=time.monotonic() - start,
        )

    result = StaticAnalysisResult(files_analysed=len(analysable))

    # Write patches to temp dir
    tmpdir = tempfile.mkdtemp(prefix="codesentinel_")
    try:
        file_paths = _write_patches(tmpdir, analysable)

        # Run tools concurrently
        semgrep_task = _run_semgrep(tmpdir, file_paths)
        bandit_task  = _run_bandit(tmpdir, file_paths)
        semgrep_out, bandit_out = await asyncio.gather(
            semgrep_task, bandit_task, return_exceptions=True
        )

        # Parse semgrep
        if isinstance(semgrep_out, Exception):
            logger.warning("Semgrep failed: %s", semgrep_out)
            result.tools_failed.append("semgrep")
        else:
            findings = _parse_semgrep(semgrep_out, tmpdir)
            result.findings.extend(findings)
            result.tools_run.append("semgrep")
            logger.info("Semgrep: %d findings", len(findings))

        # Parse bandit
        if isinstance(bandit_out, Exception):
            logger.warning("Bandit failed: %s", bandit_out)
            result.tools_failed.append("bandit")
        else:
            findings = _parse_bandit(bandit_out, tmpdir)
            result.findings.extend(findings)
            result.tools_run.append("bandit")
            logger.info("Bandit: %d findings", len(findings))

        # Deduplicate
        result.findings = _deduplicate(result.findings)

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    result.duration_seconds = time.monotonic() - start
    logger.info(
        "Static analysis complete: %d findings in %.1fs (tools: %s)",
        len(result.findings), result.duration_seconds, result.tools_run,
    )
    return result


# ── Tool runners ──────────────────────────────────────────────────────────────

async def _run_semgrep(tmpdir: str, file_paths: list[str]) -> dict:
    """Run semgrep with security rules. Returns parsed JSON output."""
    if not shutil.which("semgrep"):
        raise RuntimeError("semgrep not installed")

    cmd = [
        "semgrep",
        "--config", "p/python",
        "--config", "p/security-audit",
        "--config", "p/owasp-top-ten",
        "--json",
        "--quiet",
        "--timeout", "25",
        "--max-memory", "512",
        tmpdir,
    ]
    return await _run_subprocess(cmd, timeout=30, tool="semgrep")


async def _run_bandit(tmpdir: str, file_paths: list[str]) -> dict:
    """Run bandit recursive scan. Returns parsed JSON output."""
    if not shutil.which("bandit"):
        raise RuntimeError("bandit not installed")

    # Only run bandit on Python files
    py_files = [p for p in file_paths if p.endswith(".py")]
    if not py_files:
        return {"results": [], "errors": []}

    cmd = [
        "bandit",
        "--recursive",
        "--format", "json",
        "--quiet",
        *py_files,
    ]
    return await _run_subprocess(cmd, timeout=30, tool="bandit")


async def _run_subprocess(cmd: list[str], timeout: int, tool: str) -> dict:
    """Execute a subprocess, capture stdout, parse JSON. Raises on failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        raise RuntimeError(f"{tool} timed out after {timeout}s")
    except FileNotFoundError:
        raise RuntimeError(f"{tool} binary not found")

    # Both semgrep and bandit exit non-zero when they find issues — that's fine.
    # Only raise if stdout is empty or unparseable.
    output = stdout.decode("utf-8", errors="replace").strip()
    if not output:
        if proc.returncode not in (0, 1):
            err = stderr.decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"{tool} exited {proc.returncode}: {err}")
        return {}  # no findings

    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{tool} produced invalid JSON: {exc}")


# ── Parsers ───────────────────────────────────────────────────────────────────

def _parse_semgrep(raw: dict, tmpdir: str) -> list[Finding]:
    findings: list[Finding] = []
    for r in raw.get("results", []):
        severity = _map_semgrep_severity(r.get("extra", {}).get("severity", "WARNING"))
        meta     = r.get("extra", {}).get("metadata", {})
        findings.append(Finding(
            tool       = "semgrep",
            rule_id    = r.get("check_id", "unknown"),
            severity   = severity,
            confidence = "HIGH",
            message    = r.get("extra", {}).get("message", ""),
            filename   = _strip_tmpdir(r.get("path", ""), tmpdir),
            line_start = r.get("start", {}).get("line", 0),
            line_end   = r.get("end",   {}).get("line", 0),
            code       = r.get("extra", {}).get("lines", ""),
            cwe        = meta.get("cwe", []) if isinstance(meta.get("cwe"), list) else [],
            owasp      = meta.get("owasp", []) if isinstance(meta.get("owasp"), list) else [],
            fix_advice = meta.get("message", ""),
        ))
    return findings


def _parse_bandit(raw: dict, tmpdir: str) -> list[Finding]:
    findings: list[Finding] = []
    for r in raw.get("results", []):
        severity   = _map_bandit_severity(r.get("issue_severity", "LOW"))
        confidence = r.get("issue_confidence", "LOW")
        cwe_raw    = r.get("issue_cwe", {})
        cwe        = [f"CWE-{cwe_raw.get('id', '')}"] if cwe_raw else []
        findings.append(Finding(
            tool       = "bandit",
            rule_id    = r.get("test_id", "unknown"),
            severity   = severity,
            confidence = confidence,
            message    = r.get("issue_text", ""),
            filename   = _strip_tmpdir(r.get("filename", ""), tmpdir),
            line_start = r.get("line_number", 0),
            line_end   = r.get("line_number", 0),
            code       = r.get("code", ""),
            cwe        = cwe,
            owasp      = [],
            fix_advice = r.get("more_info", ""),
        ))
    return findings


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_analysable(filename: str) -> bool:
    """Only analyse source code files — skip images, lock files, etc."""
    skip_exts = {
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
        ".lock", ".sum", ".mod",
        ".min.js", ".min.css",
        ".pb", ".pkl", ".bin", ".whl",
    }
    skip_dirs = {"node_modules", ".venv", "venv", "dist", "build", "__pycache__"}
    p = Path(filename)
    if any(part in skip_dirs for part in p.parts):
        return False
    suffix = "".join(p.suffixes).lower()
    return suffix not in skip_exts


def _write_patches(tmpdir: str, files: list[ChangedFile]) -> list[str]:
    """
    Write the added lines from each patch into the temp directory.

    We extract only the '+' lines from the unified diff — this gives semgrep
    and bandit the new code introduced by the PR without the deleted context.
    """
    written: list[str] = []
    for cf in files:
        dest = Path(tmpdir) / cf.filename
        dest.parent.mkdir(parents=True, exist_ok=True)

        # Extract added lines from the unified diff patch
        added_lines: list[str] = []
        for line in cf.patch.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added_lines.append(line[1:])  # strip the leading '+'

        if added_lines:
            dest.write_text("\n".join(added_lines), encoding="utf-8")
            written.append(str(dest))

    return written


def _strip_tmpdir(path: str, tmpdir: str) -> str:
    """Convert absolute tmpdir path back to relative repo path."""
    tmpdir = tmpdir.rstrip("/\\")
    path   = path.replace("\\", "/")
    tmpdir = tmpdir.replace("\\", "/")
    if path.startswith(tmpdir + "/"):
        return path[len(tmpdir) + 1:]
    return path


def _map_semgrep_severity(raw: str) -> Severity:
    return {
        "ERROR":   "high",
        "WARNING": "medium",
        "INFO":    "info",
    }.get(raw.upper(), "unknown")


def _map_bandit_severity(raw: str) -> Severity:
    return {
        "HIGH":   "high",
        "MEDIUM": "medium",
        "LOW":    "low",
    }.get(raw.upper(), "unknown")


def _deduplicate(findings: list[Finding]) -> list[Finding]:
    """Remove duplicate findings (same tool + file + line + rule)."""
    seen: set[tuple] = set()
    unique: list[Finding] = []
    for f in findings:
        key = (f.tool, f.filename, f.line_start, f.rule_id)
        if key not in seen:
            seen.add(key)
            unique.append(f)
    # Sort: highest severity first
    unique.sort(key=lambda f: SEVERITY_RANK[f.severity], reverse=True)
    return unique


def _finding_to_dict(f: Finding) -> dict:
    return {
        "tool":       f.tool,
        "rule_id":    f.rule_id,
        "severity":   f.severity,
        "confidence": f.confidence,
        "message":    f.message,
        "filename":   f.filename,
        "line_start": f.line_start,
        "line_end":   f.line_end,
        "code":       f.code,
        "cwe":        f.cwe,
        "owasp":      f.owasp,
        "fix_advice": f.fix_advice,
    }


def _count_by_severity(findings: list[Finding]) -> dict[str, int]:
    counts: dict[str, int] = {s: 0 for s in SEVERITY_RANK}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return counts


def _count_by_file(findings: list[Finding]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.filename] = counts.get(f.filename, 0) + 1
    return counts
