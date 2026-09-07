# Security

## Authentication & sessions
- Passwords: PBKDF2-HMAC-SHA256, 600k iterations, per-user salt (constant-time verify).
- Access tokens: JWT HS256, short expiry (`ACCESS_TOKEN_EXPIRE_MINUTES`).
- Refresh tokens: 48-byte random, stored SHA-256-hashed, rotated on every refresh,
  revocable individually or in bulk.
- All `/api/v1/*` routes require a valid token except register/login/refresh/health.

## Authorization
- Workspace-scoped RBAC: owner/admin/member/viewer enforced by a FastAPI dependency
  on every workspace route. Cross-workspace access returns 403 even with a valid token
  (covered by tests).
- Roles gate destructive actions; reads are viewer-level.

## Secrets at rest
- Platform OAuth tokens are encrypted with AES-256-GCM before storage
  (`social_accounts.access_token_enc`). Key derived from `YMONEY_SECRET_KEY`.
- Plaintext secrets are never returned by any API response or written to logs.
- `.env` is git-ignored; `.env.example` documents required variables only.

## Input & output hardening
- Pydantic validation on all request bodies (types, lengths, enums).
- SQLAlchemy ORM exclusively — parameterized queries, no string SQL from user input.
- Video artifacts served only through path-checked endpoints; MPT's own file-security
  utilities guard its static mounts.
- CORS restricted to configured origins; only necessary methods/headers allowed.
- Rate limiting: provider calls respect source-specific intervals (e.g. Reddit ≥2s);
  engine queue full → HTTP 429 surfaced as retryable.

## Autonomy guardrails
- Budget circuit breakers (daily + monthly + per-video) halt production automatically.
- Safety Center auto-pauses autopilot with explanations on repeated failures or
  abnormal publishing rates.
- Quality threshold blocks weak content from publishing; repeated QC failures stop the cycle.
- Risk component penalizes sensitive topics; above the workspace threshold the
  decision engine escalates to HUMAN_REVIEW instead of producing.
- Content diversity: topics too similar to recent published content are skipped
  (configurable similarity threshold).
- Mock publishing default means no platform is ever touched until explicitly configured;
  simulation runs force mock publishing at the agent level.
- Publishing idempotency: unique (video_id, platform) constraints plus pre-checks make
  duplicate uploads impossible even across retries and crash recovery.
- Human override endpoints (approve/reject/retry/skip, cancel job, disconnect account)
  always supersede automation.

## Auditability
- `audit_logs` table + structured request logging for non-GET API traffic.
- Every agent execution recorded (`agent_runs`) with cost + duration + errors.
- Activity feed persists all notable system actions (`events`).

## Known limitations (documented, intentional)
- Single-process job workers; horizontal scaling requires the Redis queue swap
  (see ARCHITECTURE.md §9). The DB claim is atomic within one process.
- 2FA is scaffolded (`users.two_factor_enabled`) but not yet enforced at login —
  do not expose the API publicly without an authenticating reverse proxy if this matters.
