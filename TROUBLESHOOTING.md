# Troubleshooting

## Backend won't start
| Symptom | Fix |
|---|---|
| `unable to open database file` | The data dir is derived from `DATABASE_URL`; ensure its parent exists or use the default (auto-created). |
| `migration ... failed` | Delete dev DB (`backend/data/ymoney.db*`) — dev-only shortcut; never on production data. |
| Port 8100 busy | Change `YMONEY_PORT` or kill the stale process. |

## Autopilot
**START does nothing / state stuck STARTING**
Check the activity feed and `GET /autopilot/status`. If a previous crash paused runs,
press Resume.

**"Cycle skipped: best score too low"**
Normal conservative behavior: no opportunity crossed the PRODUCE threshold. Set a
workspace niche, enable better trend sources, or tune `scoring_weights` in settings.

**"circuit breaker: N consecutive failed cycles"**
A provider is repeatedly failing (LLM unreachable, engine down). Check `/system/health`,
fix the provider, then start again. Each failure's cause is in the activity feed and
`/jobs?status=DEAD`.

**"daily budget exhausted"**
Raise `DAILY_BUDGET_USD` or wait for the 24h window; costs visible under Settings →
Cost control.

## Video engine
- Health shows engine down → MPT not reachable at `MPT_BASE_URL`. Start it or switch
  `VIDEO_ENGINE=mock`.
- Renders fail with "engine queue full" → MPT concurrency limit hit; YMONEY retries
  automatically with backoff.
- Render timeouts default to 30min (`MPT_TIMEOUT_SECONDS`).

## Publishing
- "account missing credentials … reconnect" → delete + reconnect the account with real
  OAuth tokens. Mock accounts cannot post to real platforms.
- Everything says mock → that's `MOCK_PUBLISHING=true`. Flip only after connecting real
  accounts and verifying platform app access (YouTube resumable uploads, TikTok direct
  posting requires an audited app, Facebook page video publish).

## Frontend
- Blank page / API 502 → backend not running or Vite proxy target changed.
- No live activity → SSE requires HTTP/1.1 keep-alive through proxies; Caddy config
  handles it. Direct connections work by default.

## Resetting development state
```bash
rm -rf backend/data          # wipes local DB (dev only!)
rm -rf frontend/node_modules # clean reinstall if deps misbehave
```
