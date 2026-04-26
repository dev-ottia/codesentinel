'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';

// ── Types ─────────────────────────────────────────────────────────────────────

interface Finding {
  tool: string;
  rule_id: string;
  severity: string;
  confidence: string;
  message: string;
  filename: string;
  line_start: number;
  line_end: number;
  code: string;
  cwe: string[];
  owasp: string[];
  fix_advice: string;
  llm_suggestion?: string;
  fix_diff?: string;
  source?: string;
}

interface LLMSuggestion {
  filename: string;
  line: number;
  severity: string;
  issue: string;
  suggestion: string;
  fix_diff: string;
}

interface AnalysisResult {
  summary: string;
  severity: string;
  risk_score: number;
  findings: Finding[];
  suggestions: LLMSuggestion[];
  diff_stats?: {
    total_files: number;
    total_additions: number;
    total_deletions: number;
    total_changes: number;
    file_list: { filename: string; status: string; additions: number; deletions: number; has_patch: boolean }[];
  };
  static?: {
    tools_run: string[];
    tools_failed: string[];
    highest_severity: string;
    total_findings: number;
    by_severity: Record<string, number>;
    duration_seconds: number;
  };
  ai?: {
    summary: string;
    risk_score: number;
    security_notes: string[];
    code_quality_notes: string[];
    skipped: boolean;
    model: string;
    duration_seconds: number;
  };
  diff_fetched: boolean;
  diff_error?: string;
  model_used: string;
}

interface PR {
  id: string;
  repo: string;
  pr_number: string;
  head_sha: string;
  base_sha: string;
  title: string;
  author: string;
  pr_url: string;
  status: string;
  created_at: string;
  updated_at: string;
  analysis_result: AnalysisResult | null;
}

// ── Severity helpers ──────────────────────────────────────────────────────────

const SEV_COLOURS: Record<string, string> = {
  critical: 'bg-red-500/20 text-red-400 border-red-500/40',
  high:     'bg-orange-500/20 text-orange-400 border-orange-500/40',
  medium:   'bg-yellow-500/20 text-yellow-400 border-yellow-500/40',
  low:      'bg-blue-500/20 text-blue-400 border-blue-500/40',
  info:     'bg-muted/40 text-muted-foreground border-border',
  unknown:  'bg-muted/40 text-muted-foreground border-border',
};

const SEV_DOT: Record<string, string> = {
  critical: 'bg-red-400',
  high:     'bg-orange-400',
  medium:   'bg-yellow-400',
  low:      'bg-blue-400',
  info:     'bg-muted-foreground',
  unknown:  'bg-muted-foreground',
};

function SeverityBadge({ severity }: { severity: string }) {
  return (
    <span className={`inline-flex items-center gap-1.5 rounded border px-2 py-0.5 text-xs font-medium ${SEV_COLOURS[severity] ?? SEV_COLOURS.unknown}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${SEV_DOT[severity] ?? 'bg-muted-foreground'}`} />
      {severity.toUpperCase()}
    </span>
  );
}

// ── Risk gauge ────────────────────────────────────────────────────────────────

function RiskGauge({ score }: { score: number }) {
  const colour = score >= 80 ? '#f87171' : score >= 60 ? '#fb923c' : score >= 40 ? '#facc15' : '#4ade80';
  const pct    = Math.min(100, Math.max(0, score));

  return (
    <div className="flex flex-col items-center gap-1">
      <div className="relative h-20 w-20">
        <svg viewBox="0 0 36 36" className="h-20 w-20 -rotate-90">
          <circle cx="18" cy="18" r="15.9" fill="none" stroke="hsl(var(--muted))" strokeWidth="3" />
          <circle
            cx="18" cy="18" r="15.9" fill="none"
            stroke={colour} strokeWidth="3"
            strokeDasharray={`${pct} ${100 - pct}`}
            strokeLinecap="round"
          />
        </svg>
        <span className="absolute inset-0 flex items-center justify-center text-lg font-bold text-foreground">
          {score}
        </span>
      </div>
      <span className="text-xs text-muted-foreground">Risk Score</span>
    </div>
  );
}

// ── Diff viewer ───────────────────────────────────────────────────────────────

function DiffLine({ line }: { line: string }) {
  const isAdd = line.startsWith('+') && !line.startsWith('+++');
  const isDel = line.startsWith('-') && !line.startsWith('---');
  const isHdr = line.startsWith('@@');
  const bg = isAdd ? 'bg-green-500/10 text-green-300' :
             isDel ? 'bg-red-500/10 text-red-300'     :
             isHdr ? 'bg-blue-500/10 text-blue-400'   :
             'text-muted-foreground';
  return (
    <div className={`flex gap-2 px-3 py-0.5 font-mono text-xs leading-relaxed ${bg}`}>
      <span className="w-4 shrink-0 select-none opacity-50">
        {isAdd ? '+' : isDel ? '−' : isHdr ? '' : ' '}
      </span>
      <span className="break-all">{isHdr ? line : line.slice(1)}</span>
    </div>
  );
}

function FileDiff({ file }: { file: { filename: string; status: string; additions: number; deletions: number; patch?: string } }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-lg border border-border overflow-hidden">
      <button
        className="w-full flex items-center justify-between px-4 py-2 bg-card hover:bg-accent/40 transition-colors text-left"
        onClick={() => setOpen((o) => !o)}
      >
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-xs font-mono text-foreground truncate">{file.filename}</span>
          <span className={`text-xs px-1.5 rounded ${file.status === 'added' ? 'bg-green-500/20 text-green-400' : file.status === 'removed' ? 'bg-red-500/20 text-red-400' : 'bg-muted/40 text-muted-foreground'}`}>
            {file.status}
          </span>
        </div>
        <div className="flex items-center gap-3 shrink-0 ml-2">
          <span className="text-xs text-green-400">+{file.additions}</span>
          <span className="text-xs text-red-400">−{file.deletions}</span>
          <span className="text-muted-foreground text-xs">{open ? '▲' : '▼'}</span>
        </div>
      </button>
      {open && (
        <div className="border-t border-border overflow-x-auto bg-background max-h-96 overflow-y-auto">
          {file.patch
            ? file.patch.split('\n').map((line, i) => <DiffLine key={i} line={line} />)
            : <p className="px-4 py-3 text-xs text-muted-foreground">No patch available (binary file).</p>
          }
        </div>
      )}
    </div>
  );
}

// ── Finding card ──────────────────────────────────────────────────────────────

function FindingCard({ finding }: { finding: Finding }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-lg border border-border bg-card overflow-hidden">
      <button className="w-full flex items-start gap-3 p-4 text-left hover:bg-accent/40 transition-colors" onClick={() => setOpen((o) => !o)}>
        <SeverityBadge severity={finding.severity} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-xs font-mono text-muted-foreground">{finding.rule_id}</span>
            <span className="text-xs text-muted-foreground bg-muted px-1.5 rounded">{finding.tool}</span>
            {finding.cwe.map((c) => <span key={c} className="text-xs text-blue-400 bg-blue-500/10 px-1.5 rounded">{c}</span>)}
          </div>
          <p className="text-sm text-foreground mt-1">{finding.message}</p>
          <p className="text-xs text-muted-foreground mt-0.5 font-mono">{finding.filename}:{finding.line_start}</p>
        </div>
        <span className="text-muted-foreground text-xs shrink-0">{open ? '▲' : '▼'}</span>
      </button>
      {open && (
        <div className="border-t border-border p-4 space-y-3 animate-fade-in">
          {finding.code && (
            <div>
              <p className="text-xs text-muted-foreground mb-1 font-medium">Offending code</p>
              <pre className="text-xs bg-muted/30 rounded p-3 overflow-x-auto text-muted-foreground font-mono">{finding.code}</pre>
            </div>
          )}
          {(finding.fix_advice || finding.llm_suggestion) && (
            <div>
              <p className="text-xs text-muted-foreground mb-1 font-medium">Recommendation</p>
              <p className="text-xs text-foreground leading-relaxed">{finding.llm_suggestion || finding.fix_advice}</p>
            </div>
          )}
          {finding.fix_diff && (
            <div>
              <p className="text-xs text-muted-foreground mb-1 font-medium">Suggested fix</p>
              <div className="rounded bg-background border border-border overflow-x-auto">
                {finding.fix_diff.split('\n').map((line, i) => <DiffLine key={i} line={line} />)}
              </div>
            </div>
          )}
          {finding.owasp.length > 0 && (
            <div className="flex gap-2 flex-wrap">
              {finding.owasp.map((o) => <span key={o} className="text-xs text-orange-400 bg-orange-500/10 px-2 py-0.5 rounded border border-orange-500/20">{o}</span>)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function PRDetailPage({ params }: { params: { id: string } }) {
  const [pr, setPR]           = useState<PR | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError]     = useState<string | null>(null);
  const [tab, setTab]         = useState<'findings' | 'diff' | 'ai'>('findings');

  useEffect(() => {
    let interval: ReturnType<typeof setInterval>;

    const load = async () => {
      try {
        const res = await fetch(`/api/backend/api/webhooks/prs/${params.id}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data: PR = await res.json();
        setPR(data);
        // Poll while in-progress
        if (data.status === 'queued' || data.status === 'analyzing') {
          interval = setInterval(async () => {
            const r = await fetch(`/api/backend/api/webhooks/prs/${params.id}`);
            if (r.ok) { const d: PR = await r.json(); setPR(d); if (d.status !== 'queued' && d.status !== 'analyzing') clearInterval(interval); }
          }, 3000);
        }
      } catch (e: unknown) {
        setError(e instanceof Error ? e.message : 'Failed to load PR');
      } finally {
        setLoading(false);
      }
    };
    load();
    return () => { if (interval) clearInterval(interval); };
  }, [params.id]);

  if (loading) return (
    <div className="min-h-screen bg-background flex items-center justify-center">
      <div className="text-center space-y-3">
        <div className="h-8 w-8 border-2 border-primary border-t-transparent rounded-full animate-spin mx-auto" />
        <p className="text-sm text-muted-foreground">Loading analysis…</p>
      </div>
    </div>
  );

  if (error || !pr) return (
    <div className="min-h-screen bg-background flex items-center justify-center">
      <div className="text-center space-y-3">
        <p className="text-destructive text-sm">{error ?? 'PR not found'}</p>
        <Link href="/dashboard" className="text-xs text-primary hover:underline">← Back to dashboard</Link>
      </div>
    </div>
  );

  const result   = pr.analysis_result;
  const findings = result?.findings ?? [];
  const files    = result?.diff_stats?.file_list ?? [];
  const aiData   = result?.ai;

  const STATUS_STYLES: Record<string, string> = {
    queued:    'bg-yellow-500/15 text-yellow-400 border-yellow-500/30',
    analyzing: 'bg-blue-500/15 text-blue-400 border-blue-500/30 animate-pulse',
    completed: 'bg-green-500/15 text-green-400 border-green-500/30',
    failed:    'bg-red-500/15 text-red-400 border-red-500/30',
  };

  return (
    <div className="min-h-screen bg-background">
      <header className="border-b border-border bg-card/50 backdrop-blur sticky top-0 z-10">
        <div className="container mx-auto px-6 py-3 flex items-center gap-3">
          <Link href="/dashboard" className="text-xs text-muted-foreground hover:text-foreground transition-colors">← Dashboard</Link>
          <span className="text-muted-foreground/40">|</span>
          <span className="text-sm font-medium text-foreground truncate">{pr.title}</span>
          <span className={`ml-auto shrink-0 rounded border px-2 py-0.5 text-xs font-medium ${STATUS_STYLES[pr.status] ?? ''}`}>{pr.status}</span>
        </div>
      </header>

      <main className="container mx-auto px-6 py-6 space-y-6">
        {/* PR header */}
        <div className="rounded-xl border border-border bg-card p-5">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <div className="flex items-center gap-2 mb-1">
                <span className="text-xs text-muted-foreground font-mono">{pr.repo}</span>
                <span className="text-xs text-muted-foreground">#{pr.pr_number}</span>
                {pr.pr_url && <a href={pr.pr_url} target="_blank" rel="noopener noreferrer" className="text-xs text-primary hover:underline">↗ GitHub</a>}
              </div>
              <h1 className="text-lg font-semibold text-foreground">{pr.title}</h1>
              <div className="flex items-center gap-4 mt-2 text-xs text-muted-foreground flex-wrap">
                <span>by <span className="text-foreground">{pr.author}</span></span>
                <span className="font-mono">{pr.head_sha.slice(0, 7)}</span>
                {result?.diff_stats && (
                  <>
                    <span className="text-green-400">+{result.diff_stats.total_additions}</span>
                    <span className="text-red-400">−{result.diff_stats.total_deletions}</span>
                    <span>{result.diff_stats.total_files} file(s)</span>
                  </>
                )}
              </div>
            </div>
            {result && result.risk_score !== undefined && <RiskGauge score={result.risk_score} />}
          </div>

          {result?.summary && (
            <p className="mt-4 text-sm text-muted-foreground leading-relaxed border-t border-border pt-4">{result.summary}</p>
          )}

          {result && (
            <div className="mt-4 grid grid-cols-2 sm:grid-cols-4 gap-3">
              {[
                { label: 'Severity', value: result.severity ?? 'unknown' },
                { label: 'Findings', value: findings.length },
                { label: 'Files',    value: result.diff_stats?.total_files ?? '—' },
                { label: 'Model',    value: (result.model_used ?? '—').split(':')[0] },
              ].map((s) => (
                <div key={s.label} className="rounded-lg bg-muted/30 border border-border p-3">
                  <p className="text-xs text-muted-foreground">{s.label}</p>
                  <p className="text-sm font-semibold text-foreground mt-0.5">{String(s.value)}</p>
                </div>
              ))}
            </div>
          )}

          {(pr.status === 'queued' || pr.status === 'analyzing') && (
            <div className="mt-4 flex items-center gap-3 rounded-lg bg-blue-500/10 border border-blue-500/20 p-3">
              <div className="h-4 w-4 border-2 border-blue-400 border-t-transparent rounded-full animate-spin shrink-0" />
              <p className="text-sm text-blue-400">
                {pr.status === 'queued' ? 'Queued — waiting for worker…' : 'Analysing — fetching diff and running security tools…'}
              </p>
            </div>
          )}
        </div>

        {/* Tabs — only show when analysis is done */}
        {result && (
          <div>
            <div className="flex gap-1 border-b border-border mb-4">
              {([
                { key: 'findings', label: `Findings (${findings.length})` },
                { key: 'diff',     label: `Diff (${files.length} files)` },
                { key: 'ai',       label: 'AI Review' },
              ] as const).map((t) => (
                <button key={t.key} onClick={() => setTab(t.key)}
                  className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${tab === t.key ? 'border-primary text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground'}`}>
                  {t.label}
                </button>
              ))}
            </div>

            {/* Findings */}
            {tab === 'findings' && (
              <div className="space-y-3">
                {findings.length === 0 ? (
                  <div className="rounded-lg border border-dashed border-border p-8 text-center text-muted-foreground text-sm">
                    {result.diff_fetched ? '✅ No security issues found.' : result.diff_error ? `⚠️ ${result.diff_error}` : 'No findings available.'}
                  </div>
                ) : (
                  <>
                    {result.static?.by_severity && (
                      <div className="flex gap-2 flex-wrap mb-2">
                        {Object.entries(result.static.by_severity).filter(([, v]) => v > 0)
                          .sort(([a], [b]) => ['critical','high','medium','low','info'].indexOf(a) - ['critical','high','medium','low','info'].indexOf(b))
                          .map(([sev, count]) => (
                            <span key={sev} className={`rounded border px-2 py-0.5 text-xs font-medium ${SEV_COLOURS[sev]}`}>{count} {sev}</span>
                          ))}
                        {result.static.tools_run.length > 0 && (
                          <span className="text-xs text-muted-foreground self-center">via {result.static.tools_run.join(' + ')} · {result.static.duration_seconds}s</span>
                        )}
                      </div>
                    )}
                    {findings.map((f, i) => <FindingCard key={i} finding={f} />)}
                  </>
                )}
              </div>
            )}

            {/* Diff */}
            {tab === 'diff' && (
              <div className="space-y-3">
                {files.length === 0
                  ? <div className="rounded-lg border border-dashed border-border p-8 text-center text-muted-foreground text-sm">No diff data available.</div>
                  : files.map((f) => <FileDiff key={f.filename} file={f as { filename: string; status: string; additions: number; deletions: number; patch?: string }} />)
                }
              </div>
            )}

            {/* AI */}
            {tab === 'ai' && (
              <div className="space-y-4">
                {aiData?.skipped ? (
                  <div className="rounded-lg border border-dashed border-border p-8 text-center text-muted-foreground text-sm">
                    AI analysis unavailable — Ollama offline or not configured.<br />
                    <span className="text-xs mt-1 block">Run: <code className="bg-muted px-1 rounded">ollama serve</code> then <code className="bg-muted px-1 rounded">ollama pull qwen2.5:3b</code></span>
                  </div>
                ) : (
                  <>
                    {aiData?.summary && (
                      <div className="rounded-lg border border-border bg-card p-4">
                        <h3 className="text-sm font-semibold text-foreground mb-2">AI Summary</h3>
                        <p className="text-sm text-muted-foreground leading-relaxed">{aiData.summary}</p>
                        <div className="flex items-center gap-3 mt-3 text-xs text-muted-foreground">
                          <span>Model: <span className="text-foreground">{aiData.model}</span></span>
                          <span>Time: <span className="text-foreground">{aiData.duration_seconds}s</span></span>
                        </div>
                      </div>
                    )}
                    {(aiData?.security_notes ?? []).length > 0 && (
                      <div className="rounded-lg border border-orange-500/20 bg-orange-500/5 p-4">
                        <h3 className="text-sm font-semibold text-orange-400 mb-2">Security Notes</h3>
                        <ul className="space-y-1">
                          {aiData!.security_notes.map((n, i) => (
                            <li key={i} className="text-sm text-muted-foreground flex gap-2"><span className="text-orange-400 shrink-0">⚠</span>{n}</li>
                          ))}
                        </ul>
                      </div>
                    )}
                    {(aiData?.code_quality_notes ?? []).length > 0 && (
                      <div className="rounded-lg border border-border bg-card p-4">
                        <h3 className="text-sm font-semibold text-foreground mb-2">Code Quality</h3>
                        <ul className="space-y-1">
                          {aiData!.code_quality_notes.map((n, i) => (
                            <li key={i} className="text-sm text-muted-foreground flex gap-2"><span className="text-blue-400 shrink-0">💡</span>{n}</li>
                          ))}
                        </ul>
                      </div>
                    )}
                    {(result.suggestions ?? []).length > 0 && (
                      <div className="space-y-3">
                        <h3 className="text-sm font-semibold text-foreground">AI Suggestions</h3>
                        {result.suggestions.map((s, i) => (
                          <div key={i} className="rounded-lg border border-border bg-card p-4 space-y-2">
                            <div className="flex items-center gap-2 flex-wrap">
                              <SeverityBadge severity={s.severity} />
                              <span className="text-xs font-mono text-muted-foreground">{s.filename}:{s.line}</span>
                            </div>
                            <p className="text-sm text-foreground">{s.issue}</p>
                            <p className="text-sm text-muted-foreground leading-relaxed">{s.suggestion}</p>
                            {s.fix_diff && (
                              <div className="rounded bg-background border border-border overflow-x-auto">
                                {s.fix_diff.split('\n').map((line, j) => <DiffLine key={j} line={line} />)}
                              </div>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                  </>
                )}
              </div>
            )}
          </div>
        )}
      </main>
    </div>
  );
}
