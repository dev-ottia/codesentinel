'use client';

import { useEffect, useState } from 'react';

interface HealthData {
  status: string;
  version: string;
  db: string;
  redis: string;
}

export function BackendHealth() {
  const [health, setHealth] = useState<HealthData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const check = async () => {
      try {
        const res = await fetch('/api/backend/health', { cache: 'no-store' });
        if (res.ok) setHealth(await res.json());
      } catch { /* offline */ }
      finally { setLoading(false); }
    };
    check();
    const interval = setInterval(check, 30_000);
    return () => clearInterval(interval);
  }, []);

  if (loading) {
    return (
      <div className="rounded-lg border border-border bg-card p-4">
        <h2 className="text-sm font-semibold text-foreground mb-3">Backend</h2>
        <div className="h-4 w-24 bg-muted/50 rounded animate-pulse" />
      </div>
    );
  }

  const healthy = health?.status === 'ok';

  return (
    <div className="rounded-lg border border-border bg-card p-4 space-y-2">
      <h2 className="text-sm font-semibold text-foreground">Backend</h2>
      <div className="flex items-center gap-2">
        <span className={`h-2 w-2 rounded-full ${healthy ? 'bg-green-400 animate-pulse-dot' : 'bg-red-400'}`} />
        <span className="text-xs text-muted-foreground">
          {healthy ? `API v${health?.version}` : 'Offline'}
        </span>
      </div>
      {healthy && health && (
        <div className="grid grid-cols-2 gap-1 text-xs">
          <span className="text-muted-foreground">DB</span>
          <span className={health.db === 'connected' ? 'text-green-400' : 'text-red-400'}>{health.db}</span>
          <span className="text-muted-foreground">Redis</span>
          <span className={health.redis === 'connected' ? 'text-green-400' : 'text-red-400'}>{health.redis}</span>
        </div>
      )}
    </div>
  );
}
