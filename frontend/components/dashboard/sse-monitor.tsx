'use client';

import { useEffect, useRef, useState } from 'react';
import { Badge } from '@/components/ui/badge';

export interface AnalysisEvent {
  type: string;
  pr_id?: string;
  status?: string;
  severity?: string;
  findings?: number;
  risk_score?: number;
  summary?: string;
  timestamp?: string;
}

type ConnectionStatus = 'connecting' | 'connected' | 'error';

interface SSEMonitorProps {
  onAnalysisEvent?: (event: AnalysisEvent) => void;
}

const STATUS_CONFIG: Record<ConnectionStatus, { variant: 'default' | 'destructive' | 'secondary'; label: string; dot: string }> = {
  connecting: { variant: 'secondary',   label: 'CONNECTING', dot: 'bg-yellow-400 animate-pulse-dot' },
  connected:  { variant: 'default',     label: 'LIVE',       dot: 'bg-green-400 animate-pulse-dot'  },
  error:      { variant: 'destructive', label: 'OFFLINE',    dot: 'bg-red-400'                      },
};

const EVENT_ICONS: Record<string, string> = {
  connected:        '🔗',
  status_change:    '⚡',
  analysis_complete:'✅',
  analysis_failed:  '❌',
  ping:             '💓',
  error:            '⚠️',
};

export function SSEMonitor({ onAnalysisEvent }: SSEMonitorProps) {
  const [status, setStatus]   = useState<ConnectionStatus>('connecting');
  const [events, setEvents]   = useState<AnalysisEvent[]>([]);
  const retryTimeout          = useRef<ReturnType<typeof setTimeout> | null>(null);
  const sourceRef             = useRef<EventSource | null>(null);

  useEffect(() => {
    const connect = () => {
      setStatus('connecting');
      // Connect to the global SSE stream — receives all PR events
      const es = new EventSource('/api/backend/api/sse/global');
      sourceRef.current = es;

      es.onopen = () => setStatus('connected');

      es.onmessage = (e) => {
        try {
          const data: AnalysisEvent = JSON.parse(e.data);

          setEvents((prev) => [
            { ...data, timestamp: new Date().toLocaleTimeString() },
            ...prev.slice(0, 29),  // keep last 30
          ]);

          // Bubble meaningful events up to the dashboard
          if (data.type !== 'ping' && data.type !== 'connected' && onAnalysisEvent) {
            onAnalysisEvent(data);
          }
        } catch {
          // ignore parse errors
        }
      };

      es.onerror = () => {
        setStatus('error');
        es.close();
        retryTimeout.current = setTimeout(connect, 5000);
      };
    };

    connect();

    return () => {
      sourceRef.current?.close();
      if (retryTimeout.current) clearTimeout(retryTimeout.current);
    };
  }, [onAnalysisEvent]);

  const cfg = STATUS_CONFIG[status];

  return (
    <div className="space-y-3">
      {/* Status badge */}
      <div className="flex items-center gap-2">
        <span className={`h-2 w-2 rounded-full shrink-0 ${cfg.dot}`} />
        <Badge variant={cfg.variant}>{cfg.label}</Badge>
        <span className="text-xs text-muted-foreground truncate">
          {status === 'connected' ? '/api/sse/global' : status === 'error' ? 'Reconnecting in 5s…' : ''}
        </span>
      </div>

      {/* Live event log */}
      <div className="rounded-md border border-border bg-muted/20 h-40 overflow-y-auto">
        {events.length === 0 ? (
          <p className="text-xs text-muted-foreground italic p-3">Waiting for events…</p>
        ) : (
          <div className="p-2 space-y-1">
            {events.map((ev, i) => (
              <div key={i} className="flex gap-2 text-xs animate-fade-in">
                <span className="shrink-0 text-muted-foreground/50">{ev.timestamp}</span>
                <span className="shrink-0">{EVENT_ICONS[ev.type ?? ''] ?? '•'}</span>
                <div className="min-w-0">
                  <span className="text-muted-foreground">{ev.type}</span>
                  {ev.pr_id && (
                    <span className="text-muted-foreground/60 ml-1 font-mono truncate">
                      {ev.pr_id.slice(0, 8)}
                    </span>
                  )}
                  {ev.status && ev.type !== 'ping' && ev.type !== 'connected' && (
                    <span className="ml-1 text-primary/80">→ {ev.status}</span>
                  )}
                  {ev.severity && ev.severity !== 'unknown' && (
                    <span className="ml-1 text-orange-400">({ev.severity})</span>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
