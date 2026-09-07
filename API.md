# YMONEY API (v1)

Base URL: `/api/v1`. Auth: `Authorization: Bearer <access_token>`.
All errors: `{ "detail": string | [{msg, loc, type}] }`.

## Auth
| Method | Path | Notes |
|---|---|---|
| POST | `/auth/register` | `{email, password≥10}` → tokens + bootstrapped workspace |
| POST | `/auth/login` | → tokens |
| POST | `/auth/refresh` | rotate refresh token |
| GET | `/auth/me` | profile + workspaces |

## Workspaces
- `GET /workspaces` — list mine
- `POST /workspaces` — create `{name, niche?, brand_voice?}`
- `GET/PATCH /workspaces/{id}` — detail/update (admin)
- `GET/{id}/members`
- `GET/PUT /workspaces/{id}/settings` — merged JSON settings (scoring weights, autopilot defaults…)
- `GET/POST/DELETE /workspaces/{id}/trend-sources[...]`
- `GET /workspaces/{id}/agents` — live agent stats
- `GET/PUT /workspaces/{id}/agents/config[/{key}]` — enable/disable per agent

## Autopilot (`/workspaces/{id}/autopilot`)
- `POST /start` — `{mode: CONTINUOUS|SINGLE_CYCLE, cycles_target?, scheduled_start_at?, scheduled_stop_at?, config?}`
  - config keys: `interval_seconds`, `measure_delay_minutes`, `simulation` (set by /simulation)
- `POST /stop` · `POST /pause` · `POST /resume` · `POST /run-one-cycle`
- `GET /status` → state machine snapshot + current cycle

## Decision & Safety
- `GET /workspaces/{id}/decision` — live NEXT BEST ACTION with full WHY payload
- `GET/PUT /workspaces/{id}/safety` — safety limits (budgets, caps, thresholds); admin to write
- `POST /workspaces/{id}/simulation` — `{cycles: 1-100, interval_seconds}` run fully-mocked cycles (admin)
- `GET /workspaces/{id}/simulation/report` — decisions/produced/failures/retries/cost aggregation

## Opportunities (`/workspaces/{id}/opportunities`)
- `GET ?limit&offset&status=selected` — with explainable component breakdowns
- `POST /{opp_id}/select` — manual production trigger
- `POST /{opp_id}/skip` — `{reason?}`

## Content (`/workspaces/{id}/content`)
- `GET ?search&status&campaign_id&limit&offset`
- `GET /{content_id}` — full detail: decision WHY, research brief + claims
  (VERIFIED/LIKELY/UNCERTAIN/CONFLICTING) + factual confidence, strategy, variants+scripts,
  metadata, video, QC (13 components + confidence)
- `GET /{content_id}/timeline` — chronological event history
- `POST /{content_id}/actions` — `{action: approve|reject|retry|skip}` (human override)

## Assets (`/workspaces/{id}/assets`)
- `GET ""` — system-produced artifacts (videos table) with capabilities metadata.
  Uploads are reported as unsupported rather than faked.

## Videos (`/workspaces/{id}/videos`)
- `GET /` · `GET /{video_id}` (incl. quality checks) · `GET /{video_id}/file`

## Cycles & jobs
- `GET /workspaces/{id}/cycles`
- `GET /workspaces/{id}/jobs[?status]` · `POST /jobs/{job_id}/cancel`

## Agents
- `GET /workspaces/{id}/agents` — live stats for the full crew + recent runs
- `GET /workspaces/{id}/agents/{key}` — per-agent detail: config, aggregates, last 50 runs
- `GET/PUT /workspaces/{id}/agents/config[/{key}]` — enable/disable, model override

## Campaigns (`/workspaces/{id}/campaigns`)
- `GET ""` list · `POST ""` create (admin) · `GET /{campaign_id}` with progress counters

## Calendar (`/workspaces/{id}/calendar`)
- `GET ""` pending entries · `POST ""` schedule (admin)
- `PATCH /{entry_id}` reschedule (admin) · `DELETE /{entry_id}` cancel (admin)

## Publishing (`/workspaces/{id}/publishing`)
- `GET/POST/DELETE /accounts...` — connect stores tokens encrypted; plaintext never returned
- `GET /jobs` · `GET /posts` (with latest metrics)

## Analytics (`/workspaces/{id}/analytics`)
- `GET /overview` — totals, per-platform, best post, cost total
- `GET /patterns` — learned patterns with confidence/sample size

## Activity
- `GET /workspaces/{id}/activity/recent?limit`
- `GET /workspaces/{id}/activity/stream` — **SSE** live feed (keepalives every 15s)

## System
- `GET /system/health` — DB + video engine + LLM provider + per-publisher status, mock flags
- `GET /workspaces/{id}/logs?category&level&search` (admin)
- `GET /workspaces/{id}/costs` — 24h spend by category vs budgets
- `GET /workspaces/{id}/costs/intelligence` — cost per cycle/video/publication/1k views,
  per-agent and per-category breakdowns (estimated return is never fabricated)

### Roles
Workspace roles: `owner > admin > member > viewer`. Reads require viewer; content
actions/publishing require member/admin; destructive ops and settings require admin.
