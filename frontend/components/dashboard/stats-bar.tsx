'use client';

import { useEffect, useState } from 'react';

interface Stat { label: string; value: string | number; sub?: string }

export function StatsBar() {
  const [stats, setStats] = useState<Stat[]>([
    { label: 'Total PRs',  value: '—' },
    { label: 'Queued',     value: '—' },
    { label: 'Completed',  value: '—' },
    { label: 'Failed',     value: '—' },
  ]);

  useEffect(() => {
    const load = async () => {
      try {
        const res  = await fetch('/api/backend/api/webhooks/prs?limit=100');
        if (!res.ok) return;
        const data = await res.json();
        const items = data.items ?? [];

        const count = (s: string) => items.filter((p: { status: string }) => p.status === s).length;

        setStats([
          { label: 'Total PRs',  value: items.length },
          { label: 'Queued',     value: count('queued'),     sub: 'pending analysis' },
          { label: 'Completed',  value: count('completed'),  sub: 'analysed'         },
          { label: 'Failed',     value: count('failed'),     sub: 'need attention'   },
        ]);
      } catch { /* silently ignore — backend may not be up yet */ }
    };
    load();
  }, []);

  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 mb-6">
      {stats.map((s) => (
        <div key={s.label} className="rounded-lg border border-border bg-card p-4">
          <p className="text-2xl font-bold text-foreground">{s.value}</p>
          <p className="text-xs font-medium text-muted-foreground mt-1">{s.label}</p>
          {s.sub && <p className="text-xs text-muted-foreground/60">{s.sub}</p>}
        </div>
      ))}
    </div>
  );
}
