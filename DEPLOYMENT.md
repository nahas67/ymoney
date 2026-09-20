# Deployment

## Docker Compose (recommended)

```bash
cp .env.example .env
# REQUIRED: set YMONEY_SECRET_KEY to a long random string (48+ chars).
# Defaults are production-safe: VIDEO_ENGINE=ffmpeg_avatar, MOCK_*=false.

docker compose build          # builds backend + video engine (+ ffmpeg)
docker compose up -d
```

- Web UI: http://localhost (Caddy serves `frontend/dist` and proxies `/api` + SSE)
  - Build the frontend first: `cd frontend && npm ci && npm run build`
- Backend API: internal-only via Caddy (`http://backend:8100` inside the network)
- Data persists in the `ymoney-data` volume (SQLite with WAL).

## Production (Postgres + Redis)

```bash
cp .env.example .env
# REQUIRED: YMONEY_SECRET_KEY, POSTGRES_PASSWORD, CORS_ALLOWED_ORIGINS=https://<domain>
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d
```

- Postgres 16 holds all durable state (jobs, cycles, idempotency keys); the
  claim path uses `SELECT … FOR UPDATE SKIP LOCKED` so replicas never block.
- Redis accelerates dispatch (LPUSH/BRPOP); any outage degrades to DB polling
  automatically — Redis is never a dependency. Check `GET /system/health` →
  `queue.redis`.
- Backups: `docker exec ymoney-prod-postgres-1 pg_dump -U ymoney ymoney | gzip > backup.sql.gz`
  plus the `redisdata` volume (AOF) for in-flight dispatch signals.
- GPU lane: on a CUDA host, run `GPU_WORKER=true docker compose -f
  docker-compose.prod.yml up -d backend` (image needs torch + enabled lanes);
  GPU-native engines (`gpu_engines`, default `wan,ltx`) requeue with backpressure
  until a GPU worker claims them. `queue.gpu_worker` / `queue.gpu_cuda` in health.

### First production checklist

1. `YMONEY_SECRET_KEY` — 48+ random chars. Rotating it invalidates all tokens AND
   encrypted stored platform credentials (they must be reconnected). The backend
   refuses to start in production with the `change-me` default.
2. Set real provider config: `OPENAI_API_KEY`, `UPLOAD_POST_API_KEY`/
   `UPLOAD_POST_USERNAME` or connect OAuth accounts in-app. Mock flags default
   to false; publishing without credentials fails closed with remediation.
3. Keep `DAILY_BUDGET_USD` set — autopilot halts production when exhausted.
4. TLS: set `YMONEY_DOMAIN` + `YMONEY_TLS_EMAIL` in `.env` and uncomment the TLS
   block in `deploy/Caddyfile` if exposed beyond localhost.
5. Back up the data volume; SQLite WAL checkpoints on clean shutdown:
   `docker run --rm -v ymoney-data:/data -v %cd%/backups:/backup alpine tar czf /backup/ymoney-data-$(date +%F).tgz -C /data .`

## Manual deployment

1. Backend: Python 3.12, `pip install ./backend`, run uvicorn behind a process manager:
   `uvicorn app.main:app --host 0.0.0.0 --port 8100`
2. Video engine: run MoneyPrinterTurbo per its README (its Redis mode recommended for
   concurrent renders); point `MPT_BASE_URL` at it.
3. Frontend static build served by any web server that proxies `/api` + SSE to the API.

## Operations

- Health probe: `GET /api/v1/system/health`
- Logs: `/api/v1/workspaces/{id}/logs?category=...&level=error` plus container stdout
  (loguru). Audit trail for non-GET API calls is logged server-side.
- After a crash: restart the backend; runs auto-pause, failed cycles are marked.
  Resume from the dashboard.
