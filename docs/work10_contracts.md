# Work 10 — Authoritative contracts (all lanes read this)

Repo conventions: SQLAlchemy+SQLite, migrations append-only (`upgrade(session)` only,
filename-sorted, next free number **0027**), models registered in `models/__init__.py`,
FastAPI routers wired in `api/v1/__init__.py`, jobs via `services/jobs.py`
(`register_handler`, `enqueue(idempotency_key=)`, `check_cancelled(ctx)`, backoff/retry built in).
PowerShell 5.1: no `&&`, trust `$LASTEXITCODE`, no sleep-waits, suites run SEQUENTIALLY.
All work UNCOMMITTED (no git commits).

## Exclusive file ownership

| Lane | Owns (only these files may be edited) |
|---|---|
| Foundation | `backend/app/models/knowledge.py`, `backend/app/models/__init__.py`, `backend/app/migrations/versions/0027_knowledge.py`, `backend/app/engine/knowledge/__init__.py`, `backend/app/engine/sources/__init__.py` |
| L0 (main thread) | `engine/community/autonomy.py`, `engine/community/policy.py`, `api/v1/inbox.py` (classify label filter only), `tests/test_community_policy.py`, `tests/test_inbox_api.py`, `tests/test_jobs_bootstrap.py` |
| A memory | `engine/knowledge/memory.py`, `engine/knowledge/freshness.py`, `tests/test_knowledge_memory.py` |
| B graph+retrieval | `engine/knowledge/graph.py`, `engine/knowledge/retrieval.py`, `tests/test_knowledge_graph.py`, `tests/test_knowledge_retrieval.py` |
| C sources | `engine/sources/base.py`, `engine/sources/registry.py`, `engine/sources/adapters/*`, `engine/sources/sync.py`, `tests/test_sources.py` |
| D api | `api/v1/knowledge.py`, `api/v1/__init__.py` (wiring lines only), `api/v1/content.py` (append response-windows route only), `tests/test_knowledge_api.py` |
| E integration | `engine/knowledge/context_bridge.py`, `engine/knowledge/community_bridge.py`, `engine/agents/creation.py`, `engine/agents/scheduler.py`, `tests/test_knowledge_integration.py` |
| F lineage | `engine/knowledge/lineage.py`, `tests/test_source_lineage.py` |
| G frontend | `frontend/src/pages/Knowledge.tsx`, `frontend/src/App.tsx`, `frontend/src/components/Layout.tsx` |

## Foundation schema (migration 0027, 6 tables)

### knowledge_memories (GlobalMemory record)
id PK, workspace_id FK NOT NULL index, brand_id String(36) NULL,
type String(40) index (one of TYPES), content Text NOT NULL,
topic String(200) default "", topic_key String(64) index default "",
scope String(120) default "", platform String(40) default "",
source_ids JSON default [] , evidence_ids JSON default [],
confidence Float default 0.5,
freshness String(20) default "FRESH"   -- age band FRESH|AGING|STALE (recomputed on read)
status String(20) default "ACTIVE"     -- lifecycle: ACTIVE|CONFLICTED|SUPERSEDED|UNVERIFIED|DISABLED
origin String(80) default ""           -- agent key or "user"
content_hash String(64) index default ""  -- sha256 of normalized content (dedupe)
conflict_group String(64) index default "",
last_verified_at DateTime NULL,
superseded_by String(36) NULL,
use_count Integer default 0, last_used_at DateTime NULL,   -- historical usefulness
related_json JSON default {},
created_at, updated_at
Indexes: (workspace_id,type), (workspace_id,topic_key), (workspace_id,status),
         unique (workspace_id, type, topic_key, content_hash) is NOT global-unique — dedupe in service.

**effective_status(row, now)**: if status != ACTIVE → status; else freshness band.
Six spec statuses map: FRESH/AGING/STALE = freshness band; CONFLICTED/SUPERSEDED/UNVERIFIED = status.
Extra ALLOWED value: DISABLED (user action, UI "disable a memory").

### knowledge_evidence
id PK, workspace_id FK index, memory_id FK index CASCADE, kind String(40)
(evidence_record|publication|interaction|source_document|agent_run|user|metric|research_claim),
ref_id String(250) default "", source_id String(36) default "" (source_documents.id when applicable),
detail Text default "", confidence Float default 1.0, captured_at DateTime, created_at.
Unique (memory_id, kind, ref_id).

### knowledge_nodes  (graph)
id PK, workspace_id FK index, node_type String(30) index — one of:
Brand|Campaign|Content|Scene|Topic|Entity|Source|Claim|AudienceInsight|CommunityInsight|Experiment|CreativeLesson|Publication,
ref_id String(36) default "" (domain row id, "" for Topic/Entity), node_key String(120),
label String(200) default "", topic_key String(64) index default "", meta_json JSON default {},
created_at, updated_at. Unique (workspace_id, node_type, node_key).

### knowledge_edges
id PK, workspace_id FK index, from_node_id String(36) index, to_node_id String(36) index,
relationship String(40) index — one of:
CONTENT_ABOUT_TOPIC|CLAIM_SUPPORTED_BY|DERIVED_FROM|MENTS_ENTITY|PERFORMED_ON|LEARNED_FROM|
COMMUNITY_REQUESTED|EXPERIMENT_TESTED|BRAND_USES|SOURCE_REFERENCES,
weight Float default 1.0, evidence_ids JSON default [], meta_json JSON default {}, created_at.
Unique (workspace_id, from_node_id, to_node_id, relationship).

### source_connectors
id PK, workspace_id FK index, kind String(40), name String(120),
status String(20) default "AVAILABLE"  -- AVAILABLE|UNAVAILABLE|DISABLED|ERROR
unavailable_reason String(200) default "",
config_json JSON default {}   -- NEVER returned raw by API (redact secrets)
last_cursor Text default "", last_sync_at DateTime NULL, last_error Text default "",
enabled Bool default True, doc_count Integer default 0,
created_at, updated_at. Unique (workspace_id, kind, name).

### source_documents
id PK, workspace_id FK index, connector_id FK index CASCADE, remote_id String(200),
title Text default "", mime_type String(100) default "",
remote_created_at DateTime NULL, remote_updated_at DateTime NULL,
author String(200) default "", content Text default "",
asset_reference String(500) default "",   -- storage_key / s3://... / "" for text-only
checksum String(64) default "", cursor String(200) default "",
state String(20) default "active"  -- active|updated|deleted
first_seen_at, last_seen_at, meta_json JSON default {}, created_at, updated_at.
Unique (workspace_id, connector_id, remote_id). Index (workspace_id, state).

## Service contracts

### Lane A — `engine/knowledge/memory.py` + `freshness.py`
```python
TYPES = ("RESEARCH_FACT","SOURCE","CONTENT_RESULT","AUDIENCE_INSIGHT","COMMUNITY_INSIGHT",
         "BRAND_KNOWLEDGE","CREATIVE_LESSON","EXPERIMENT_RESULT","PLATFORM_LEARNING",
         "ENTITY","RELATIONSHIP","USER_APPROVED_KNOWLEDGE")
FACT_CONFLICT_TYPES = ("RESEARCH_FACT",)   # auto-conflict on same topic_key, different hash

class GlobalMemory:
    @staticmethod
    def store(db, workspace_id, *, type, content, confidence=0.5, scope="", brand_id=None,
              topic="", platform="", source_ids=None, evidence_ids=None, origin="",
              supersedes: str | None = None, conflicts_with: str | None = None) -> dict
    @staticmethod
    def get(db, workspace_id, memory_id) -> dict | None
    @staticmethod
    def list(db, workspace_id, *, type=None, status=None, scope=None, topic=None, q=None,
             limit=50) -> list[dict]
    @staticmethod
    def verify(db, workspace_id, memory_id, *, user_id="") -> dict
    @staticmethod
    def disable(db, workspace_id, memory_id) -> dict
    @staticmethod
    def supersede(db, workspace_id, target_id, *, replacement_id) -> dict
    @staticmethod
    def mark_used(db, workspace_id, memory_ids) -> None   # use_count += 1, last_used_at
```
Rules (must hold, tested):
- Provenance: empty `evidence_ids` AND empty `source_ids` at store → `status="UNVERIFIED"`.
  `verify()` adds evidence kind `user` (ref=user_id) + sets `last_verified_at` + `status="ACTIVE"`.
- Supersession: target → `SUPERSEDED`, `superseded_by=new id`; history (old row) preserved.
- Conflict: new row with `conflicts_with=old` → BOTH `status="CONFLICTED"`, shared `conflict_group`;
  auto for FACT_CONFLICT_TYPES when same (type, topic_key) ACTIVE row exists with different content_hash.
  Conflicting rows are NEVER deleted/overwritten.
- Store idempotent: same (type, topic_key, content_hash) ACTIVE row → return existing (no dup row).
- Freshness band (`freshness.py`): FRESH <7d, AGING <30d, STALE >=30d, age from
  `last_verified_at or created_at`; `refresh(db, ws, memory_id)` recomputes on read/list.
- Secrets: `store` must reject content matching token-ish patterns? NO — instead: never accept
  credential fields; `related_json` must not be API-writable. No OAuth/secrets stored (test asserts
  API never echoes `config_json` secrets — that is Lane D).

### Lane B — `engine/knowledge/graph.py` + `retrieval.py`
```python
class KnowledgeGraphProvider:            # relational implementation, provider-independent interface
    def upsert_node(self, db, workspace_id, *, node_type, ref_id="", label, topic_key="", meta=None) -> dict
    def link(self, db, workspace_id, *, from_node, to_node, relationship, weight=1.0,
             evidence_ids=None, meta=None) -> dict   # from/to = node dicts or (node_type, ref_id/label)
    def get_node(self, db, workspace_id, node_type, key) -> dict | None
    def neighbors(self, db, workspace_id, *, node_id=None, node_type=None, key=None,
                  relationship=None, direction="out"|"in"|"both", limit=50) -> list[dict]
    def subgraph(self, db, workspace_id, *, seed_node, depth=2, limit=200) -> dict  # {nodes, edges}
    def edges_for(self, db, workspace_id, *, ref_id) -> list[dict]

class MemoryRetriever:
    def retrieve(self, db, workspace_id, *, task="", brand_id=None, platform=None, topic="",
                 content_format=None, time_window_days=90, max_results=10,
                 types=None) -> dict   # {"items": [memory dict ranked + _score breakdown],
                                       #  "metrics": {"considered","filtered_hard","ranked","returned"},
                                       #  "ranking": {"weights": {...}}}
```
Ranking score = 0.25*scope_match + 0.25*relevance + 0.15*evidence_quality + 0.15*freshness
+ 0.10*confidence + 0.10*usefulness, each in [0,1], breakdown exposed per item.
- scope_match: brand/platform/scope/type filters hit = 1.0, partial 0.5, none 0.0
- relevance: token overlap vs task/topic (`services.memory.semantic_score` style, deterministic)
- evidence_quality: `min(1, (n_evidence + n_sources)/3)`; 0 if unprovenanced
- freshness: FRESH 1.0, AGING 0.6, STALE 0.2, UNVERIFIED 0.4, CONFLICTED 0.15
- usefulness: `min(1, use_count/10)`
- HARD filters (deterministic, before ranking): `workspace_id ==`, status not SUPERSEDED/DISABLED,
  time_window on created_at, type/platform/scope match. Workspace isolation NEVER via scoring.
- Semantic stage: candidate top-3x re-ranked by Work 05 `DecisionEngine.rank` mode-gated
  (DISABLED → keep deterministic order; SHADOW → persist decision record, keep deterministic order;
  ASSISTED/PRIMARY → apply engine order). Helper `_semantic_rank(workspace_id, items, task)`.

### Lane C — `engine/sources/*`
```python
# base.py
MAX_SOURCE_BYTES = 10 * 1024 * 1024      # text content cap
ALLOWED_TEXT_MIME = ("text/plain","text/html","application/xml","application/rss+xml",
                     "application/atom+xml","application/json","application/pdf")
class SourceError(Exception): ...        # honest failure, message safe to surface
@dataclass class SourceDocumentDoc: connector, remote_id, title, mime_type, created_at,
    updated_at, author, content, asset_reference, checksum, cursor, meta
class SourceConnector(ABC):
    kind: str; implemented: bool = True; requires_credentials: bool = False
    def connect(self, config: dict) -> None            # validate; SourceError on bad config
    def health(self, config: dict) -> dict             # {"status","reason"} status in AVAILABLE|UNAVAILABLE|ERROR
    def list(self, config, *, cursor: str | None) -> tuple[list[SourceDocumentDoc], str | None]
    def fetch(self, config, remote_id: str) -> SourceDocumentDoc
    def search(self, config, query: str, *, limit=20) -> list[SourceDocumentDoc]
    def disconnect(self, config) -> None
# registry.py — CONNECTOR_CATALOG: 13 kinds:
#   implemented+creds-required: s3 (requires_credentials=True)
#   implemented: local, url, rss, youtube
#   catalog-only (implemented=False, health() -> UNAVAILABLE "not implemented yet"):
#   google_drive, dropbox, oneDrive, zoom, riverside, twitch, vimeo, loom
#   create_connector(kind, config) -> SourceConnector  (raises SourceError for unknown/unimplemented)
# sync.py — SOURCE_SYNC durable job (registered idempotently, mirrors community jobs pattern):
#   payload {"connector_id": ...}; workspace from ctx.workspace_id (hard filter everywhere);
#   cursor persisted on source_connectors.last_cursor (durable across jobs);
#   upsert per (workspace_id, connector_id, remote_id) — created/updated/unchanged;
#   snapshot lists mark unseen active docs state="deleted";
#   check_cancelled between pages; failures raise → jobs retry/backoff, connector.last_error set,
#   per-connector job isolation (one connector failing never touches another).
#   enqueue key: f"source-sync:{workspace_id}:{connector_id}" (dedupe in-flight).
```
URL safety (all network adapters): `services.webhooks.validate_url` + `ipaddress` reject private/
loopback/link-local hosts unless `config.allow_private` (default False); scheme http/https only;
size cap enforced during read; MIME allowlist enforced. Path safety: local adapter resolves only
inside workspace storage root via `storage.validate_storage_key` / `managed_path`.
Credential-required connectors: health/connect honest UNAVAILABLE without config; API never
returns config values (redact keys matching token|key|secret|password|credential → `has_credentials`).

### Lane D — `api/v1/knowledge.py` (router prefix `/knowledge`, dual-mounted like inbox:
`include_router(knowledge_router, prefix="/workspaces/{workspace_id}")` + flat include)
Roles: GET = viewer, POST memory actions = member, connector register/disconnect = admin.
Routes (all workspace-scoped, ws from dependency, 404 foreign ids):
```
GET  /knowledge/memories?type=&status=&scope=&topic=&q=&limit=
POST /knowledge/memories                      {type, content, ...}
POST /knowledge/memories/{id}/verify
POST /knowledge/memories/{id}/disable
POST /knowledge/memories/{id}/supersede       {replacement_id}
GET  /knowledge/graph?node_type=&limit=
GET  /knowledge/sources                       (redacted)
POST /knowledge/sources                       {kind, name, config}  admin
POST /knowledge/sources/{id}/sync             admin (enqueue SOURCE_SYNC)
POST /knowledge/sources/{id}/disconnect       admin
GET  /knowledge/sources/{id}/documents?limit=
GET  /knowledge/community-signals
POST /knowledge/promote-insights              member
GET  /knowledge/retrieve?task=&topic=&platform=&max_results=
```
content.py append: `GET /workspaces/{id}/calendar/response-windows` (viewer) →
`SchedulerAgent.recommend_response_windows` (Lane E). Errors: HTTPException with short detail,
422 enum-ish validation, generic 500 (no exception echo, logger `ymoney.knowledge`).

### Lane E — integration
```python
# engine/knowledge/context_bridge.py
def build_memory_context(workspace_id, *, db=None, task="", brand_id=None, platform=None,
                         topic="", content_format=None, time_window_days=90,
                         max_results=10, max_tokens=2000) -> dict
# flow: MemoryRetriever.retrieve -> provenance/freshness filter (drop UNVERIFIED? no:
#   keep but downrank already; drop SUPERSEDED/DISABLED) -> ContextBudgetManager.add(each,
#   Category.LONG_TERM_MEMORY) -> budget(max_tokens) -> mark_used(kept)
# returns {"metrics": {"retrieved","used","filtered","recalled","raw_tokens","kept_tokens",
#                      "filtered_tokens","compression_ratio"},
#          "items": [kept dicts], "references": [...], "memory_ids": [...all...],
#          "used_memory_ids": [...kept...], "manager": ContextBudgetManager}  # manager NOT serialized by API
def recall(ref_id, manager) -> dict   # increments recalled metric path
# engine/knowledge/community_bridge.py
def promote_insights_to_memory(db, workspace_id, *, min_count=3) -> list[dict]
# for each community insight meeting threshold: GlobalMemory.store(type="COMMUNITY_INSIGHT",
#   evidence_ids=[interaction ids...], confidence from insight, topic=..., origin="community_agent")
#   idempotent via topic_key+content_hash. Non-meeting insights skipped (low count never high-confidence).
# engine/agents/creation.py (L): ResearchAgent.research + StrategistAgent.strategize gain a
#   `_memory_block(ws, topic)` using build_memory_context(task=topic) -> prompt text block;
#   outputs embed {"memory": {"retrieved": n, "used_memory_ids": [...]}} (research_json/strategy_json).
# engine/agents/scheduler.py (L):
#   SchedulerAgent.recommend_response_windows(workspace_id, *, platform=None) -> dict
#   {"items": [{"hour","reason","sources":[...]}], "measured": bool, "activity": bool,
#    "audience_activity": {"available": bool, "hours": [...]}, "caps": {...},
#    "activity_policy": "recommendation_only"|"action_allowed", "notes": [str...]}
#   measured = reuse _best_hours logic (>=3 measured posts else measured=False + platform seeds
#   labeled heuristic). audience_activity = bucket SocialInteraction.created_at hour (>=10 rows
#   else available=False). caps from autonomy (daily_cap, rate_per_10min, cooldown) + safety
#   (max_uploads_per_hour) + campaign schedule density (existing ScheduleEntry hours to avoid).
#   activity_policy = "action_allowed" only if settings_json["schedule_automation"] is True
#   (new explicit opt-in, default False) else "recommendation_only". NEVER invent best-time data.
```
Strategist/community proof test: seed >=27 interactions on one topic → promote →
`build_memory_context(task="that topic")` returns the memory in `used_memory_ids` AND the
captured strategist/research prompt contains its content (patch llm.complete_json to capture).

### Lane F — `engine/knowledge/lineage.py`
```python
def build_research_bundle(db, workspace_id, *, document_ids, topic, title="") -> dict
# {"topic","title","summary","sources":[{"document_id","title","mime_type","retrieved_at","checksum"}],
#  "excerpts":[{"document_id","text"}], "from_documents":[ids], "factual_confidence": float,
#  "provenance": "source_documents"}
def create_content_from_bundle(db, workspace_id, *, bundle, campaign_id=None) -> content_id
# ContentItem(topic, research_json=bundle, campaign_id) — EXISTING research contract untouched;
# graph edges: Content node -SOURCE_REFERENCES-> Source nodes; Content -CONTENT_ABOUT_TOPIC-> Topic
def asset_from_document(db, workspace_id, *, document_id) -> asset_id | None
# only when doc.asset_reference resolves via managed_path/validate_storage_key (or s3);
# MediaAsset dedupe by (workspace_id, storage_key) + checksum stored when computable
def timeline_from_asset(db, workspace_id, *, asset_id, content_id=None) -> dict
# wraps EXISTING engine.timeline.timeline_from_video(workspace_id, video_id=asset.id, file_path=...)
```
No bypass of existing lineage: content uses ContentItem research_json + content_graph node ids.

### Lane G — frontend
`/knowledge` route (App.tsx static import pattern), nav entry in Layout NAV (group
next to Inbox, icon ◈), page with 4 tabs: Memory (type/scope/status/confidence/freshness/evidence +
actions verify/disable/supersede), Graph (nodes by type + edges grouped), Sources (connectors,
status, last cursor, failures, Sync button), Community Signals (topic, count, confidence, promote).
Data via `wsApi` + `useFetch`; poll sources tab 30s.

## Test matrix (must all exist, names close to spec list)
memory provenance · workspace isolation · freshness/stale rules · conflicting facts ·
supersession · retrieval ranking · context-budget integration · community→strategy influence ·
strict approval mode · write-time label validation · connector idempotency · cursor resume ·
connector failure isolation · source update/delete · URL safety · source→research lineage ·
source asset→MediaAsset lineage · bootstrap-guard registration · graph relationships ·
scheduler recommendation honesty (no invented data) · connector redaction (API) · RBAC on /knowledge.

## Gates (sequential, evidence to $env:TEMP\opencode\)
fast (`-m "not slow"`) · slow · battery (new test files) · ruff `--select F,I,SIM,UP` on touched files
· migration hygiene + fresh replay (0027) · Postman regen + test_postman · `npm run build`.

---

## Wave status + deltas (orchestrator, live)

DONE:
- Section 0 (Work 09 deferred): strict_approval toggle in community autonomy +
  send_action gate (approval_required_strict, default off); write-time label
  filter `_validated_labels` in api/v1/inbox.py (read-time labels_of kept);
  `_bootstrap_community_jobs()` + tests/test_jobs_bootstrap.py.
- Foundation: models/knowledge.py (6 tables; composite index names ix_kmem_* /
  ix_kdoc_* to avoid the memory_records ix_memory_ws_type collision), migration
  0027_knowledge.py, exports, engine/knowledge + engine/sources packages.
  Fresh replay APPLIED 27 / REPLAY_NOOP, hygiene + import gate green.
- Orchestrator-owned shared helpers (READ ONLY for lanes):
  `engine/knowledge/freshness.py` (age_days, freshness_band, effective_status,
  STATUS_PRECEDENCE, ALL_STATUSES) and `engine/knowledge/normalize.py`
  (normalize_text, content_hash_of, topic_key_of).
- Scheduler: `SchedulerAgent.recommend_response_windows` static method at
  `engine/agents/scheduler.py:190` (+ `_measured_hour_facts`); 10 tests in
  tests/test_scheduler_windows.py. Lane D: expose GET calendar/response-windows
  (viewer role) serializing the dict as-is; key `schedule_automation` opt-in.
- Frontend: pages/Knowledge.tsx (4 tabs) + App.tsx route + Layout NAV entry;
  build exit 0; degrades honestly on 404/503 until Lane D lands.
- test_phase_b.py pollinations live skip broadened (user-approved): skips on
  any HTTP-status/transport failure with the real status in the reason
  (was 5xx-only; service now answers 402 Payment Required).

IN FLIGHT: Lane A (memory.py), Lane B (graph.py + retrieval.py),
Lane C (engine/sources/*).

DELTAS vs sections above:
- Lane C: do NOT use jobs enqueue idempotency_key for sync (it dedupes against
  ANY historical job and would block re-syncs) - use `enqueue_source_sync`
  with an active (queued/running) SOURCE_SYNC job query instead.
- Lane C: connectors hold config on the instance via create_connector(kind, config);
  SourceConnector.snapshot class flag drives deletion semantics.
- Freshness read path: services recompute freshness/effective_status on read;
  persisting the band is optional (flush only, never commit in services).
- Auto-conflict requires non-empty topic_key (empty = topicless facts coexist).
- Remaining waves: D (api/v1/knowledge.py + response-windows route + guarded
  source-job bootstrap), E (context_bridge, community_bridge, creation.py memory
  block), F (lineage), then full gates.
