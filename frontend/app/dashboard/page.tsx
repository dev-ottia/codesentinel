'use client';

import { useState } from 'react';
import { SSEMonitor, type AnalysisEvent } from '@/components/dashboard/sse-monitor';
import { PRList }        from '@/components/dashboard/pr-list';
import { StatsBar }      from '@/components/dashboard/stats-bar';
import { BackendHealth } from '@/components/dashboard/backend-health';

export default function DashboardPage() {
  const [latestEvent, setLatestEvent] = useState<AnalysisEvent | null>(null);

  return (
    <div className="min-h-screen bg-background">
      {/* Top nav */}
      <header className="border-b border-border bg-card/50 backdrop-blur sticky top-0 z-10">
        <div className="container mx-auto px-6 py-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <span className="text-lg font-bold text-foreground">CodeSentinel</span>
            <span className="text-xs text-muted-foreground bg-muted px-2 py-0.5 rounded-full">
              Dashboard
            </span>
          </div>
          <a
            href="https://github.com"
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-2 rounded-md border border-border bg-card px-3 py-1.5 text-sm text-foreground hover:bg-accent transition-colors"
          >
            <svg className="h-4 w-4" fill="currentColor" viewBox="0 0 24 24" aria-hidden="true">
              <path fillRule="evenodd" d="M12 2C6.477 2 2 6.484 2 12.017c0 4.425 2.865 8.18 6.839 9.504.5.092.682-.217.682-.483 0-.237-.008-.868-.013-1.703-2.782.605-3.369-1.343-3.369-1.343-.454-1.158-1.11-1.466-1.11-1.466-.908-.62.069-.608.069-.608 1.003.07 1.531 1.032 1.531 1.032.892 1.53 2.341 1.088 2.91.832.092-.647.35-1.088.636-1.338-2.22-.253-4.555-1.113-4.555-4.951 0-1.093.39-1.988 1.029-2.688-.103-.253-.446-1.272.098-2.65 0 0 .84-.27 2.75 1.026A9.564 9.564 0 0112 6.844c.85.004 1.705.115 2.504.337 1.909-1.296 2.747-1.027 2.747-1.027.546 1.379.202 2.398.1 2.651.64.7 1.028 1.595 1.028 2.688 0 3.848-2.339 4.695-4.566 4.943.359.309.678.92.678 1.855 0 1.338-.012 2.419-.012 2.747 0 .268.18.58.688.482A10.019 10.019 0 0022 12.017C22 6.484 17.522 2 12 2z" clipRule="evenodd" />
            </svg>
            Connect GitHub
          </a>
        </div>
      </header>

      <main className="container mx-auto px-6 py-8">
        {/* Stats row */}
        <StatsBar />

        {/* Main grid */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* PR list — 2/3 width, receives SSE events for instant updates */}
          <section className="lg:col-span-2">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-base font-semibold text-foreground">Pull Requests</h2>
              <span className="text-xs text-muted-foreground">Live · 30s fallback poll</span>
            </div>
            <PRList latestEvent={latestEvent} />
          </section>

          {/* Sidebar */}
          <aside className="space-y-4">
            <div className="rounded-lg border border-border bg-card p-4">
              <h2 className="text-sm font-semibold text-foreground mb-3">Live Events</h2>
              <SSEMonitor onAnalysisEvent={setLatestEvent} />
            </div>

            <div className="rounded-lg border border-border bg-card p-4">
              <h2 className="text-sm font-semibold text-foreground mb-2">Test Webhook</h2>
              <p className="text-xs text-muted-foreground mb-2">From the project root:</p>
              <pre className="text-xs bg-muted/50 rounded p-2 text-muted-foreground overflow-x-auto">
                python test_webhook.py
              </pre>
            </div>

            {/* Client-side health — no hydration mismatch */}
            <BackendHealth />
          </aside>
        </div>
      </main>
    </div>
  );
}
