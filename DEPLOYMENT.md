# Deployment

## Docker Compose (recommended)

```bash
cp .env.example .env
# REQUIRED: set YMONEY_SECRET_KEY to a long random string.
# Review mock flags: MOCK_PUBLISHING=true is the safe default until accounts are connected.

docker compose build          # builds backend + MoneyPrinterTurbo engine
docker compose up -d
```

- Web UI: http://localhost (Caddy serves `frontend/dist` and proxies `/api`)
  - Build the frontend first: `cd frontend && npm ci && npm run build`
- Backend API: internal-only via Caddy (`http://backend:8100` inside the network)
- Data persists in the `ymoney-data` volume (SQLite with WAL).

### First production checklist

1. `YMONEY_SECRET_KEY` — 48+ random chars. Rotating it invalidates all tokens AND
   encrypted stored platform credentials (they must be reconnected).
2. Set real provider config: `OPENAI_API_KEY`, `MOCK_LLM=false`,
   `UPLOAD_POST_API_KEY`/`UPLOAD_POST_USERNAME` or connect OAuth accounts in-app,
   then `MOCK_PUBLISHING=false`, `MOCK_ANALYTICS=false`.
3. Keep `DAILY_BUDGET_USD` set — autopilot halts production when exhausted.
4. Put the backend behind TLS (Caddy/traefik/nginx) if exposed beyond localhost.
5. Back up the data volume; SQLite WAL checkpoints on clean shutdown.

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
