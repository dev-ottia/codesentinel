'use client';

import { useEffect, useState, useCallback } from 'react';
import Link from 'next/link';
import type { AnalysisEvent } from '@/components/dashboard/sse-monitor';

interface PullRequest {
  id: string;
  repo: string;
  pr_number: string;
  head_sha: string;
  title: string;
  author: string;
  pr_url: string;
  status: 'queued' | 'analyzing' | 'completed' | 'failed';
  created_at: string;
  updated_at: string;
  analysis_result?: {
    severity?: string;
    risk_score?: number;
    findings?: unknown[];
  } | null;
}

const STATUS_STYLES: Record<PullRequest['status'], string> = {
  queued:    'bg-yellow-500/15 text-yellow-400 border-yellow-500/30',
  analyzing: 'bg-blue-500/15 text-blue-400 border-blue-500/30 animate-pulse',
  completed: 'bg-green-500/15 text-green-400 border-green-500/30',
  failed:    'bg-red-500/15 text-red-400 border-red-500/30',
};

const SEV_COLOURS: Record<string, string> = {
  critical: 'text-red-400',
  high:     'text-orange-400',
  medium:   'text-yellow-400',
  low:      'text-blue-400',
  info:     'text-muted-foreground',
  unknown:  'text-muted-foreground',
};

function timeAgo(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60_000);
  if (mins < 1)  return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24)  return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

interface PRListProps {
  latestEvent?: AnalysisEvent | null;
}

export function PRList({ latestEvent }: PRListProps) {
  const [prs, setPRs]         = useState<PullRequest[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError]     = useState<string | null>(null);

  const fetchPRs = useCallback(async () => {
    try {
      const res = await fetch('/api/backend/api/webhooks/prs?limit=20');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setPRs(data.items ?? []);
      setError(null);
    } catch {
      setError('Could not reach backend. Is it running?');
    } finally {
      setLoading(false);
    }
  }, []);

  // Initial load + 30s fallback poll
  useEffect(() => {
    fetchPRs();
    const interval = setInterval(fetchPRs, 30_000);
    return () => clearInterval(interval);
  }, [fetchPRs]);

  // SSE-driven instant refresh — update the changed row in-place
  useEffect(() => {
    if (!latestEvent?.pr_id) return;
    setPRs((prev) =>
      prev.map((pr) =>
        pr.id === latestEvent.pr_id
          ? {
              ...pr,
              status: latestEvent.status as PullRequest['status'],
              analysis_result: latestEvent.type === 'analysis_complete'
                ? {
                    severity:   latestEvent.severity,
                    risk_score: latestEvent.risk_score,
                    findings:   Array(latestEvent.findings ?? 0).fill(null),
                  }
                : pr.analysis_result,
            }
          : pr
      )
    );
  }, [latestEvent]);

  if (loading) {
    return (
      <div className="space-y-2">
        {[...Array(3)].map((_, i) => (
          <div key={i} className="h-16 rounded-lg bg-muted/30 animate-pulse" />
        ))}
      </div>
    );
  }

  if (error) {
    return (
      <div className="rounded-lg border border-destructive/30 bg-destructive/10 p-4 text-sm text-destructive flex items-center justify-between">
        <span>{error}</span>
        <button onClick={fetchPRs}
          className="text-xs border border-destructive/30 rounded px-2 py-1 hover:bg-destructive/20 transition-colors">
          Retry
        </button>
      </div>
    );
  }

  if (prs.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-border p-8 text-center text-muted-foreground text-sm">
        No pull requests yet.
        <br />
        <span className="text-xs mt-1 block">Run <code className="bg-muted px-1 rounded">python test_webhook.py</code> to send a test event.</span>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {prs.map((pr) => {
        const findings = pr.analysis_result?.findings?.length ?? 0;
        const severity = pr.analysis_result?.severity;
        const risk     = pr.analysis_result?.risk_score;

        return (
          <Link
            key={pr.id}
            href={`/dashboard/pr/${pr.id}`}
            className="flex items-start justify-between rounded-lg border border-border bg-card p-4 hover:bg-accent/40 transition-colors group"
          >
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 mb-1">
                <span className="text-xs text-muted-foreground font-mono truncate">{pr.repo}</span>
                <span className="text-xs text-muted-foreground">#{pr.pr_number}</span>
              </div>
              <p className="text-sm font-medium text-foreground group-hover:text-primary transition-colors truncate">
                {pr.title}
              </p>
              <div className="flex items-center gap-3 mt-1 flex-wrap">
                <span className="text-xs text-muted-foreground">by {pr.author}</span>
                <span className="text-xs text-muted-foreground font-mono">{pr.head_sha.slice(0, 7)}</span>
                <span className="text-xs text-muted-foreground">{timeAgo(pr.updated_at)}</span>
                {pr.status === 'completed' && severity && (
                  <span className={`text-xs font-medium ${SEV_COLOURS[severity] ?? ''}`}>
                    {severity}
                  </span>
                )}
                {pr.status === 'completed' && findings > 0 && (
                  <span className="text-xs text-muted-foreground">{findings} finding{findings !== 1 ? 's' : ''}</span>
                )}
                {pr.status === 'completed' && risk !== undefined && (
                  <span className="text-xs text-muted-foreground">risk {risk}/100</span>
                )}
              </div>
            </div>

            <div className="flex items-center gap-2 ml-3 shrink-0">
              <span className={`rounded border px-2 py-0.5 text-xs font-medium ${STATUS_STYLES[pr.status]}`}>
                {pr.status}
              </span>
              <span className="text-muted-foreground text-xs opacity-0 group-hover:opacity-100 transition-opacity">→</span>
            </div>
          </Link>
        );
      })}
    </div>
  );
}
