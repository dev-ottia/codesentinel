# CodeSentinel — Architecture & Design Decisions

## System Overview

CodeSentinel is an event-driven, async-first platform for automated PR security review.
It follows a clear separation between ingestion (FastAPI), processing (ARQ worker), and
presentation (Next.js + SSE), connected by Redis as the message bus.

---

## C4 Context Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        External Systems                         │
│                                                                 │
│  ┌────────────┐      webhooks      ┌──────────────────────────┐ │
│  │   GitHub   │ ────────────────▶  │    CodeSentinel API      │ │
│  │            │ ◀────────────────  │    (FastAPI / Python)    │ │
│  │  PR events │   PR diff (REST)   └──────────────────────────┘ │
│  └────────────┘                              │                  │
│                                              │ SSE              │
│  ┌────────────┐                    ┌─────────▼──────────────┐   │
│  │   Ollama   │ ◀────────────────  │  CodeSentinel          │   │
│  │  (local    │   prompt + diff    │  Dashboard (Next.js)   │   │
│  │   LLM)     │ ────────────────▶  │                        │   │
│  └────────────┘   JSON analysis    └────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

---

## C4 Container Diagram

```
┌─────────────────────────────── Docker Network ───────────────────────────────┐
│                                                                               │
│  ┌─────────────┐    HTTP     ┌─────────────────┐   asyncpg  ┌─────────────┐  │
│  │  Next.js    │ ──────────▶ │   FastAPI        │ ─────────▶ │ PostgreSQL  │  │
│  │  Frontend   │ ◀────────── │   Backend        │            │  (pgdata)   │  │
│  │  :3000      │    SSE      │   :8000          │            │  :5432      │  │
│  └─────────────┘             └────────┬─────────┘            └─────────────┘  │
│                                       │                                        │
│                              enqueue  │  Redis                                 │
│                              pub/sub  │  pub/sub                               │
│                                       ▼                                        │
│                              ┌─────────────────┐                               │
│                              │     Redis        │                               │
│                              │     :6379        │                               │
│                              └────────┬─────────┘                               │
│                                       │ dequeue                                 │
│                                       ▼                                         │
│                              ┌─────────────────┐   HTTP    ┌─────────────────┐  │
│                              │   ARQ Worker     │ ────────▶ │  Ollama         │  │
│                              │   (background)   │           │  host:11434     │  │
│                              └─────────────────┘           └─────────────────┘  │
└───────────────────────────────────────────────────────────────────────────────┘
```

---

## End-to-End Data Flow

```
1. GitHub sends POST /api/webhooks/github
        │
        ▼
2. FastAPI verifies HMAC-SHA256 signature (constant-time)
        │
        ▼
3. Payload parsed → PullRequest row upserted (QUEUED)
        │
        ▼
4. ARQ job enqueued with deterministic job_id (dedup)
        │
        ▼
5. 202 Accepted returned to GitHub immediately
        │
        ▼ (async)
6. ARQ Worker picks up job
        │
        ├─▶ GitHub API fetches PR diff (Redis cached 2h)
        │
        ├─▶ semgrep + bandit run against changed files (tmpdir)
        │        └─ findings parsed into unified schema
        │
        ├─▶ Ollama LLM receives diff + findings → JSON response
        │        └─ suggestions + risk score + security notes
        │
        ├─▶ Results merged, deduplicated, severity-ranked
        │
        ├─▶ PullRequest row updated (COMPLETED/FAILED)
        │
        └─▶ Redis PUBLISH → SSE stream → browser updates instantly
```

---

## Key Design Decisions

### 1. ARQ over Celery

**Decision:** Use ARQ (async Redis queue) instead of Celery.

**Reasoning:**
- ARQ is fully async-native — no need for `asyncio.run()` wrappers
- Celery requires separate result backend configuration; ARQ uses Redis for both
- ARQ's `WorkerSettings` is a plain Python class — easier to test and extend
- Our worker does async I/O (GitHub API, Ollama HTTP, asyncpg) — ARQ handles this natively

**Trade-off:** ARQ has a smaller ecosystem than Celery. No native support for periodic tasks (would use `arq.cron` if needed).

---

### 2. SSE over WebSockets

**Decision:** Use Server-Sent Events instead of WebSockets.

**Reasoning:**
- PR analysis is unidirectional — server pushes to client, no client→server messages needed
- SSE works through HTTP/1.1 and proxies without special configuration
- SSE auto-reconnects natively in the browser (EventSource API)
- WebSockets require stateful connections and special load-balancer config
- SSE with Redis pub/sub scales horizontally — any backend replica can serve any client

**Trade-off:** SSE is HTTP/1.1 only; multiplexing requires HTTP/2. Not a concern at our scale.

---

### 3. Local Ollama over Cloud LLM

**Decision:** Default to Ollama (local) instead of OpenAI/Anthropic API.

**Reasoning:**
- **Zero data egress:** code diffs contain intellectual property — they must not leave the org's infrastructure
- **Zero cost per review:** no API bills; model runs on local hardware
- **No rate limits:** cloud LLM APIs rate-limit aggressively; local has no such constraint
- **Deterministic:** `temperature=0.1` + `format=json` gives reproducible results

**Trade-off:** Quality is lower than GPT-4 class models. Inference is slower on CPU (15–60s vs 2–5s). GPU recommended for production.

**Model selection guide:**
| Model | RAM | Speed (CPU) | Quality |
|-------|-----|-------------|---------|
| `qwen2.5:3b` | 2GB | 15–30s | Good |
| `qwen2.5:7b` | 4GB | 30–60s | Better |
| `llama3.1:8b` | 5GB | 45–90s | Best |
| `codellama:7b` | 4GB | 30–60s | Best for code |

---

### 4. PostgreSQL over MongoDB

**Decision:** Relational PostgreSQL with async SQLAlchemy.

**Reasoning:**
- PR analysis results have a well-defined schema (status enum, JSON findings)
- Alembic migrations give us versioned, reversible schema changes
- ACID guarantees prevent duplicate records under concurrent webhook delivery
- The `UniqueConstraint(repo, pr_number, head_sha)` enforces idempotency at DB level

**Trade-off:** JSON column for `analysis_result` is semi-schemaless — acceptable since the analysis schema evolves frequently during development.

---

### 5. Idempotency Strategy

Multiple layers prevent duplicate processing:

| Layer | Mechanism |
|-------|-----------|
| DB | `UniqueConstraint(repo_full_name, pr_number, head_sha)` — duplicate rows impossible |
| ARQ | `_job_id=f"analyse:{repo}:{pr}:{sha}"` — ARQ ignores duplicate job IDs |
| Redis | Diff cached by `repo:pr:sha` — same commit never fetched twice |
| Race condition | `IntegrityError` caught and treated as success (not 500) |

---

## Scaling Considerations

**Current architecture supports:**
- Multiple backend replicas (stateless API, shared Redis/Postgres)
- Multiple workers (Redis queue naturally distributes jobs)
- Redis pub/sub fans out to all SSE clients regardless of which replica they're on

**Bottlenecks at scale:**
1. Ollama is single-process — parallel analyses queue up. **Solution:** run multiple Ollama instances or upgrade to a cloud LLM with high rate limits.
2. semgrep is CPU-intensive. **Solution:** dedicate a separate worker tier for static analysis.
3. PostgreSQL single instance. **Solution:** read replicas for the PR list endpoint.

---

## Security Model

| Threat | Mitigation |
|--------|-----------|
| Forged webhooks | HMAC-SHA256 constant-time verification |
| Replay attacks | GitHub includes `X-GitHub-Delivery` UUID (future: store and check) |
| Oversized payloads | 5MB hard cap before parsing |
| SQL injection | All DB access through SQLAlchemy ORM (parameterised queries) |
| XSS | `Content-Security-Policy` header on all responses |
| Clickjacking | `X-Frame-Options: DENY` |
| Rate abuse | Per-IP sliding window (30/min webhooks, 300/min global) |
| Secret leakage | Structlog redaction processor strips token/secret keys from logs |
| Container escape | Non-root user (UID 1001) in all containers |
