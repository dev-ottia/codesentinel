# CodeSentinel

> **AI-powered GitHub pull request security analysis platform.**
> Automatically reviews every PR for vulnerabilities, misconfigurations, and code quality issues — using local AI so your code never leaves your infrastructure.

[![CI](https://github.com/YOUR_USERNAME/codesentinel/actions/workflows/ci.yml/badge.svg)](https://github.com/YOUR_USERNAME/codesentinel/actions)
![Python](https://img.shields.io/badge/python-3.11-blue)
![Next.js](https://img.shields.io/badge/next.js-14-black)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Live Demo

![Dashboard screenshot showing PR list with severity badges and risk scores](docs/dashboard-preview.png)

- **Backend API:** `http://localhost:8000/docs`
- **Frontend:** `http://localhost:3000`

---

## What It Does

When a developer opens or updates a pull request on GitHub, CodeSentinel:

1. **Receives** the webhook event and validates the HMAC-SHA256 signature
2. **Fetches** the PR diff from the GitHub API (cached in Redis for 2h)
3. **Runs** semgrep + bandit static analysis against all changed files
4. **Sends** the diff and findings to a local Ollama LLM for AI-powered review
5. **Merges** static + AI findings, deduplicates, and ranks by severity
6. **Broadcasts** results via Server-Sent Events — dashboard updates in real-time

---

## Architecture

```
GitHub → Webhook → FastAPI → PostgreSQL
                       ↓
                    Redis Queue
                       ↓
                    ARQ Worker
                    ├── GitHub API (fetch diff)
                    ├── Semgrep + Bandit (static analysis)
                    └── Ollama LLM (AI review)
                           ↓
                    Redis Pub/Sub → SSE → Browser
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full C4 diagram and design decisions.

---

## Tech Stack

| Layer | Technology | Why |
|-------|-----------|-----|
| API | FastAPI (Python 3.11) | Async-native, automatic OpenAPI docs, type-safe |
| Queue | ARQ + Redis | Lightweight async job queue, Redis pub/sub for SSE |
| Database | PostgreSQL + SQLAlchemy 2.0 | Async ORM, Alembic migrations, ACID guarantees |
| Static Analysis | Semgrep + Bandit | Best-in-class Python security scanners, JSON output |
| AI | Ollama (qwen2.5:3b) | Local LLM — zero data egress, $0 per review |
| Frontend | Next.js 14 + Tailwind CSS | App Router, SSE client, dark mode |
| Observability | Structlog + OpenTelemetry | JSON logs, trace IDs, request correlation |

---

## Quick Start

### Prerequisites

- Docker Desktop ≥ 4.x
- Python 3.11+ (for running alembic/pytest outside Docker)
- [Ollama](https://ollama.com) (for AI analysis)
- A GitHub account (for real webhooks — optional for local testing)

### 1. Clone & configure

```bash
git clone https://github.com/YOUR_USERNAME/codesentinel.git
cd codesentinel
cp .env.example .env
```

Edit `.env` — the minimum required fields:

```bash
GITHUB_APP_WEBHOOK_SECRET=your_webhook_secret   # any random string for local dev
SECRET_KEY=your_random_secret_key               # any random string
GITHUB_TOKEN=ghp_xxxx                           # GitHub PAT (optional — needed for real PRs)
```

### 2. Set up Ollama (for AI analysis)

```bash
# Install Ollama from https://ollama.com/download
# Then pull the model (one-time, ~2GB download):
ollama pull qwen2.5:3b

# Start Ollama server (keep this running in a separate terminal):
ollama serve
```

Ollama runs on `http://localhost:11434` and is accessible from Docker containers
via `http://host.docker.internal:11434` (already configured in `.env`).

### 3. Start the stack

```bash
docker compose up --build
```

Services started:
- `postgres` on port `5433` (host) / `5432` (container)
- `redis` on port `6379`
- `backend` FastAPI on port `8000`
- `worker` ARQ background worker
- `frontend` Next.js on port `3000`

### 4. Apply database migrations

In a new terminal (with the venv activated):

```bash
cd backend
pip install -e ".[test]"          # install all dependencies
alembic upgrade head              # create tables
```

### 5. Verify everything is running

```bash
curl http://localhost:8000/health
# Expected: {"status":"ok","version":"0.1.0","db":"connected","redis":"connected"}
```

Open `http://localhost:3000` in your browser.

### 6. Send a test webhook

```bash
# From project root
python test_webhook.py
```

Watch the worker analyse the PR in real-time:

```bash
docker compose logs worker -f
```

You'll see:
```
worker-1 | ▶ Worker picked up PR db_id=...
worker-1 | Diff fetched: 3 files, 45 changes
worker-1 | Static: 2 findings, severity=high
worker-1 | LLM: risk_score=65, 3 suggestions
worker-1 | ✅ Analysis complete
```

The dashboard at `http://localhost:3000/dashboard` updates automatically via SSE.

---

## Connecting Real GitHub Webhooks

### Option A — ngrok (recommended for local dev)

```bash
# Install ngrok from https://ngrok.com
ngrok http 8000
# Copy the https URL, e.g.: https://abc123.ngrok.io
```

### Option B — smee.io (free, no account)

```bash
npm install -g smee-client
smee --url https://smee.io/YOUR_CHANNEL --target http://localhost:8000/api/webhooks/github
```

### Configure GitHub webhook

1. Go to your GitHub repo → **Settings → Webhooks → Add webhook**
2. **Payload URL:** `https://your-tunnel-url.ngrok.io/api/webhooks/github`
3. **Content type:** `application/json`
4. **Secret:** same value as `GITHUB_APP_WEBHOOK_SECRET` in your `.env`
5. **Events:** select "Pull requests"

Now every PR open/update in that repo triggers a full analysis.

---

## Running Tests

```bash
cd backend
pytest -v                              # all 64 unit + integration tests
pytest --cov=app --cov-report=term-missing   # with coverage report
pytest tests/test_live.py -m live     # live tests (requires Docker stack)
```

---

## Project Structure

```
codesentinel/
├── backend/
│   ├── app/
│   │   ├── api/
│   │   │   ├── sse.py              # Redis pub/sub SSE publisher + generators
│   │   │   └── routes/
│   │   │       ├── webhooks.py     # GitHub webhook ingestion + PR CRUD
│   │   │       └── sse.py          # SSE FastAPI routes
│   │   ├── core/
│   │   │   ├── arq_pool.py         # Shared ARQ Redis pool
│   │   │   ├── config.py           # Pydantic settings (env-driven)
│   │   │   ├── logging.py          # Structlog JSON/console setup
│   │   │   └── middleware.py       # Security headers, rate limit, request ID
│   │   ├── db/
│   │   │   ├── base.py             # SQLAlchemy DeclarativeBase
│   │   │   ├── models.py           # PullRequest model
│   │   │   └── session.py          # Async engine + session factory
│   │   ├── services/
│   │   │   ├── analysis.py         # Pipeline orchestrator
│   │   │   ├── github_client.py    # GitHub REST API + diff fetching
│   │   │   ├── llm_client.py       # Ollama HTTP client + prompt engineering
│   │   │   └── static_analysis.py  # Semgrep + bandit runner + parser
│   │   ├── workers/
│   │   │   └── queue.py            # ARQ task + WorkerSettings
│   │   └── main.py                 # FastAPI app + middleware stack
│   ├── alembic/                    # Database migrations
│   ├── tests/                      # 64 tests across 6 test files
│   └── pyproject.toml
├── frontend/
│   ├── app/
│   │   ├── dashboard/
│   │   │   ├── page.tsx            # Dashboard with SSE live updates
│   │   │   └── pr/[id]/page.tsx    # PR detail: findings, diff, AI review
│   │   ├── layout.tsx
│   │   └── page.tsx                # Landing page
│   └── components/
│       └── dashboard/
│           ├── backend-health.tsx
│           ├── pr-list.tsx         # Clickable PR list with SSE updates
│           ├── sse-monitor.tsx     # Live event stream panel
│           └── stats-bar.tsx
├── docker-compose.yml
├── test_webhook.py                 # Live webhook test script
└── ARCHITECTURE.md
```

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DATABASE_URL` | ✅ | — | PostgreSQL async URL |
| `REDIS_URL` | ✅ | — | Redis URL |
| `GITHUB_APP_WEBHOOK_SECRET` | ✅ | — | Webhook HMAC secret |
| `SECRET_KEY` | ✅ | — | App secret key |
| `GITHUB_TOKEN` | ⚠️ | — | GitHub PAT for diff fetching |
| `OLLAMA_URL` | — | `http://host.docker.internal:11434` | Ollama base URL |
| `OLLAMA_MODEL` | — | `qwen2.5:3b` | Model name |
| `DIFF_CACHE_TTL` | — | `7200` | Diff cache TTL (seconds) |
| `LOG_LEVEL` | — | `INFO` | `DEBUG`/`INFO`/`WARNING` |
| `LOG_FORMAT` | — | `console` | `console` or `json` |
| `CORS_ORIGINS` | — | `http://localhost:3000` | Comma-separated allowed origins |

---

## Security

- **Webhook validation:** HMAC-SHA256 constant-time comparison — replay attacks prevented
- **Rate limiting:** 30 req/min on webhook endpoint, 300 req/min globally (Redis sliding window)
- **Security headers:** HSTS, CSP, X-Frame-Options, X-Content-Type-Options on every response
- **Input sanitisation:** All user input truncated before DB writes, SQL injection prevented by ORM
- **Secrets:** Never logged — automatic redaction in structlog pipeline
- **Non-root Docker:** Backend runs as `appuser` (UID 1001)
- **Local AI:** Code diffs never sent to external APIs

---

## Performance

| Metric | Value |
|--------|-------|
| Webhook ingestion p95 | < 50ms |
| Static analysis (Python file) | 2–8s |
| AI analysis (qwen2.5:3b, CPU) | 15–60s |
| AI analysis (qwen2.5:3b, GPU) | 3–10s |
| Concurrent webhooks | 50 req/s sustained |
| Diff cache hit rate | ~95% (same PR re-analysed) |

---

## Contributing

1. Fork the repo
2. Create a feature branch: `git checkout -b feat/my-feature`
3. Write tests for your changes
4. Run `pytest -v` — all tests must pass
5. Open a PR

---

## License

MIT — see [LICENSE](LICENSE) for details.
