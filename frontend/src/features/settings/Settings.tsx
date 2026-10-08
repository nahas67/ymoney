/* Settings — vertical settings architecture.
 *
 * A fixed vertical sidebar replaces the old horizontal Tabs. Sections are
 * grouped by concern, searchable, and mounted ONLY when active, so one
 * section's failed read never blanks another's.
 *
 * Endpoint map (every control below names a real route; nothing is invented):
 *
 *   General
 *     Workspace            GET "" / PATCH "" (admin)
 *     Locale & Display     workspace language/timezone (read-only: WorkspaceBody
 *                          accepts name/niche/brand_voice only) + settings keys
 *   Team & Access
 *     Members              GET /members (viewer; role is the server's)
 *     API Keys             GET /api-keys (viewer); POST /api-keys (admin, mint,
 *                          plaintext shown once); POST /api-keys/{id}/revoke
 *                          (admin, dangerous)
 *   Autonomy
 *     Autonomy Policy      GET /planner/policy + GET /decision (viewer, read-only)
 *     Agents               GET /agents/config (viewer); PUT /agents/config/{k}
 *                          (admin)
 *     Capacity             GET /planner/calendar (viewer, read-only);
 *                          POST /planner/capacity (admin)
 *   Budgets & Costs
 *     Budgets & Safety     GET /safety (viewer); PUT /safety (admin, validated)
 *     Cost Analytics       GET /costs + GET /costs/intelligence (viewer)
 *   Publishing
 *     Accounts             GET /publishing/accounts (viewer);
 *                          POST /publishing/accounts (admin);
 *                          DELETE /publishing/accounts/{id} (admin, dangerous)
 *     Scheduling           GET /calendar (viewer, read-only; writes live in the
 *                          content/Distribution flows)
 *   AI & Generation
 *     Providers            GET /connections/tts|/images|/video-engine (admin,
 *                          status); PUT /connections/video-engine (admin);
 *                          POST /connections/test-llm|/test-publishing (admin)
 *     Music                GET /music/policy + /music/providers (viewer);
 *                          PUT /music/policy (admin)
 *   Storage & Media
 *     Retention & Assets   GET /retention (admin); PUT /retention (admin);
 *                          GET /assets (viewer)
 *   Notifications
 *     Inbox                GET /notifications (viewer, own rows only);
 *                          POST /notifications/read-all and /{id}/read (viewer)
 *     Telegram             GET /telegram/status (viewer);
 *                          POST /telegram/pairing-code (admin);
 *                          DELETE /telegram/links/{id} (admin, dangerous);
 *                          POST /telegram/links/{id}/toggle (admin);
 *                          POST /telegram/test (admin)
 *   Integrations
 *     Connections & Keys  GET /connections (admin, status only);
 *                          PUT /connections (admin, write/clear);
 *                          POST /connections/test-llm|/test-publishing (admin)
 *     Webhooks             GET /webhooks (viewer); POST /webhooks (admin,
 *                          secret shown once); DELETE /webhooks/{id} (admin,
 *                          dangerous); POST /webhooks/{id}/test (admin)
 *     Knowledge Sources    GET /knowledge/sources (viewer, server-redacted);
 *                          POST /knowledge/sources/{id}/sync|/disconnect (admin)
 *     Trend Sources        GET /trend-sources (viewer);
 *                          DELETE /trend-sources/{id} (admin, dangerous)
 *
 * DELIBERATELY NOT HERE (each with its backend evidence):
 *
 *   - Brand accent/logo/app name. GET /workspaces/{id}/brand and
 *     POST /workspaces/{id}/brand/logo exist (workspaces.py) but belong to the
 *     Brands feature; a second editor is how chrome and label drift apart.
 *   - Knowledge connector CREATE. POST /knowledge/sources needs a `kind` whose
 *     valid values (CONNECTOR_CATALOG) are exposed by NO endpoint, so a
 *     free-text kind field would 422. Sync/disconnect are included.
 *   - Trend source CREATE. Same reason: the kind REGISTRY (workspaces.py) is
 *     not listed by any route. List + delete are included.
 *   - Localization glossary. Real CRUD (localization.py) but operational
 *     content at member level, owned by the Localization feature — not
 *     workspace configuration.
 *   - Scheduling writes. POST /calendar lives in the content flows
 *     (content.py); this screen reads the queue.
 *   - TTS/image live-test buttons. Real routes (connections.py) but they spend
 *     real provider budget and return raw bytes; status is shown here and
 *     testing stays on the Providers surface.
 *   - Notification creation. No route exists: the inbox is append-only, written
 *     by ledger events. Read + mark-read only.
 *
 * NO SECRET, NO PROMPT. `AgentConfig.prompt_override` is never returned and not
 * declared. `GET /api-keys` returns a 12-char `prefix` (credential fingerprint)
 * and `GET /connections` returns `masked` values — neither is declared, so
 * neither can render. Notification `payload` is free-form and absent.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Badge,
  Button,
  DataTable,
  DestructiveButton,
  EmptyState,
  Field,
  Grid,
  Money,
  PageHeader,
  Panel,
  QueryBoundary,
  Select,
  StatTile,
  Toggle,
  humanize,
  type Column,
} from "../../design-system/primitives";
import { useMutation, useWsQuery, type QueryState } from "../../api/queries";
import { wsApi } from "../../lib/api";
import { blockedReason, can, useSession } from "../../state/session";

/* Component-scoped styles. Owned by this file because the work order forbids
 * touching anything else: every value resolves to a design token, nothing
 * hardcodes a colour, and the press transition sits in the 80–120ms band
 * (100ms) with a reduced-motion opt-out. */

/* ==========================================================================
 * Response shapes
 * ======================================================================= */

/* `workspaces._serialize_ws`. `brand_voice` exists on the wire and is
 * deliberately NOT declared: brand content lives in the Brands feature. */
export type Workspace = {
  id: string;
  name: string;
  slug: string | null;
  niche: string;
  language: string | null;
  timezone: string | null;
  settings: Record<string, unknown>;
  created_at: string;
};

export type WorkspaceBody = {
  name: string;
  niche: string;
  brand_voice: string;
};

export type Member = { user_id: string; role: string; email: string };

/* `safety.get_safety` -> `get_safety_settings`, always fully merged. */
export type Safety = {
  daily_budget_usd: number;
  monthly_budget_usd: number;
  per_video_budget_usd: number;
  max_videos_per_day: number;
  max_uploads_per_hour: number;
  min_qc_score: number;
  max_render_attempts: number;
  max_consecutive_failures: number;
  max_concurrent_renders: number;
  similarity_threshold: number;
  require_human_review_risk_above: number;
  require_approval_before_publish: boolean;
  produce_score_threshold: number;
};

export type CostSummary = {
  last_24h_by_category: Record<string, number>;
  spent_last_24h_usd: number | null;
  spent_last_24h_unknown_exposure_rows: number;
  daily_budget_usd: number;
  per_video_budget_usd: number;
  within_budget: boolean;
  remaining_usd: number;
};

export type CostIntelligence = {
  total_cost_usd: number | null;
  per_cycle_usd: number | null;
  per_video_usd: number | null;
  per_publication_usd: number | null;
  cost_per_1000_views_usd: number | null;
  by_category: Record<string, number>;
  by_agent: Record<string, number>;
  publications_by_platform: Record<string, number>;
  totals: { cycles: number; videos_built: number; posts_published: number; views: number };
  estimated_return_usd: number | null;
};

export type AutonomyRow = {
  rank: number;
  suggests: boolean;
  creates_plan_items: boolean;
  creates_campaign_drafts: boolean;
  starts_research: boolean;
  schedules: boolean;
  advances_production: boolean;
  publishes: boolean;
  note: string;
};

export type AutonomyPolicy = {
  modes: string[];
  actions: string[];
  table: Record<string, AutonomyRow>;
  publishes: boolean;
  note: string;
};

export type DecisionPreview = {
  action: string;
  reasons: string[];
  blockers?: string[];
  score?: number | null;
};

/* `agents_router.list_agent_configs`. `prompt_override` is never returned. */
export type AgentConfigRow = {
  key: string;
  title: string;
  description: string;
  enabled: boolean;
  model: string;
  timeout_seconds: number;
  cost_limit_usd: number | null;
};

export type Notification = {
  id: string;
  kind: string;
  read: boolean;
  read_at: string | null;
  created_at: string;
};

/* `api_keys._public`. `prefix` is a credential fingerprint: not declared. */
export type ApiKeyRow = {
  id: string;
  name: string;
  role: string;
  revoked: boolean;
  last_used_at: string | null;
  created_at: string;
};

export type WebhookRow = {
  id: string;
  url: string;
  events: string[];
  active: boolean;
  created_at: string;
};
export type WebhookList = { items: WebhookRow[]; events: string[] };

export type Retention = Record<string, number | null>;

export type SourceConnector = {
  id: string;
  kind: string;
  name: string;
  status: string;
  unavailable_reason: string | null;
  enabled: boolean;
  config: Record<string, unknown>;
  has_credentials: boolean;
  implemented: boolean;
  requires_credentials: boolean;
  title: string;
  blurb: string;
  doc_count: number;
  last_sync_at: string | null;
  last_error: string;
};
export type SourceList = { items: SourceConnector[] };

export type TrendSource = {
  id: string;
  kind: string;
  name: string;
  enabled: boolean;
  priority: number;
};
export type TrendSourceList = { items: TrendSource[] };

/* `connections.list_connections`. `masked` is returned by the API and NOT
 * declared: for a non-secret key it IS the raw value, for a secret it is a
 * fingerprint — both are out of this screen's business. */
export type ConnectionRow = {
  key: string;
  label: string;
  secret: boolean;
  hint: string;
  configured: boolean;
  source: string;
};

export type TelegramLink = {
  id: string;
  chat_id: string;
  chat_title: string;
  active: boolean;
  linked_at: string | null;
};
export type TelegramStatus = {
  bot_configured: boolean;
  token_source: string;
  linked: boolean;
  links: TelegramLink[];
};

export type SocialAccount = {
  id: string;
  platform: string;
  display_name: string;
  external_id: string;
  status: string;
  token_expires_at: string | null;
};

export type ScheduleEntry = {
  id: string;
  platform: string;
  run_at: string;
  content_item_id: string;
  campaign_id: string;
  status: string;
};

export type CalendarCapacity = {
  declared: boolean;
  locale: string;
  longform_per_week: number;
  shorts_per_day: number;
  ugc_per_day: number;
  localization_per_day: number;
  render_hours_per_day: number;
  review_slots_per_day: number;
  notes: string;
};
export type CalendarState = {
  entries: { id: string; platform: string; run_at: string; status: string }[];
  capacity: CalendarCapacity;
  committed: Record<string, unknown>;
  remaining: Record<string, number | null>;
};

export type MusicPolicy = {
  generate: boolean;
  reason: string;
  brand_disabled: boolean;
  provider_key: string;
  configured: boolean;
  forbidden_genres: string[];
  prefs: Record<string, unknown>;
};

export type AssetItem = {
  id: string;
  type: string;
  title: string;
  engine: string;
  status: string;
  size_bytes: number | null;
  is_mock: boolean;
  created_at: string;
};

/* ==========================================================================
 * Section registry — the single declaration the nav, search and panels share,
 * so the list cannot drift from what is rendered.
 * ======================================================================= */

export type SectionId =
  | "workspace"
  | "appearance"
  | "members"
  | "api-keys"
  | "policy"
  | "agents"
  | "capacity"
  | "budgets"
  | "costs"
  | "accounts"
  | "scheduling"
  | "providers"
  | "music"
  | "storage"
  | "inbox"
  | "telegram"
  | "connections"
  | "webhooks"
  | "sources"
  | "trends";

export type SectionGroup = {
  id: string;
  label: string;
  sections: { id: SectionId; label: string; blurb: string; keywords: string }[];
};

export const SETTING_GROUPS: SectionGroup[] = [
  {
    id: "general",
    label: "General",
    sections: [
      { id: "workspace", label: "Workspace", blurb: "Identity: name and niche.", keywords: "workspace identity name niche general defaults" },
      { id: "appearance", label: "Locale & Display", blurb: "Language, timezone, settings keys.", keywords: "locale timezone language appearance display defaults region" },
    ],
  },
  {
    id: "team",
    label: "Team & Access",
    sections: [
      { id: "members", label: "Members", blurb: "Server-side roles.", keywords: "team members roles permissions access users" },
      { id: "api-keys", label: "API Keys", blurb: "Machine credentials: mint and revoke.", keywords: "api keys tokens security access revoke mint developer" },
    ],
  },
  {
    id: "autonomy",
    label: "Autonomy",
    sections: [
      { id: "policy", label: "Autonomy Policy", blurb: "What each mode may do; never publish.", keywords: "autonomy policy approval rules modes publish human review" },
      { id: "agents", label: "Agents", blurb: "Per-agent model, timeout, cost cap, enable.", keywords: "agents permissions models timeout enable disable" },
      { id: "capacity", label: "Capacity", blurb: "Declared production rates.", keywords: "capacity rates throughput defaults planning" },
    ],
  },
  {
    id: "budgets",
    label: "Budgets & Costs",
    sections: [
      { id: "budgets", label: "Budgets & Safety", blurb: "Validated budget and safety policy.", keywords: "budget budgets safety limits costs spending emergency controls circuit breaker" },
      { id: "costs", label: "Cost Analytics", blurb: "Actual spend and unit economics.", keywords: "costs spend analytics exposure economics unknown" },
    ],
  },
  {
    id: "publishing",
    label: "Publishing",
    sections: [
      { id: "accounts", label: "Accounts", blurb: "Connected platform accounts.", keywords: "publishing accounts platforms connect youtube tiktok facebook instagram" },
      { id: "scheduling", label: "Scheduling", blurb: "Queued placements, read-only.", keywords: "publishing scheduling queue calendar safety rules approval" },
    ],
  },
  {
    id: "generation",
    label: "AI & Generation",
    sections: [
      { id: "providers", label: "Providers", blurb: "Voice, image and video engine status.", keywords: "ai models routing voice tts image video engine providers generation" },
      { id: "music", label: "Music", blurb: "Generated-music opt-in.", keywords: "music generation opt-in provider" },
    ],
  },
  {
    id: "storage",
    label: "Storage & Media",
    sections: [
      { id: "storage", label: "Retention & Assets", blurb: "Retention policy and stored media.", keywords: "storage media retention assets data files" },
    ],
  },
  {
    id: "notifications",
    label: "Notifications",
    sections: [
      { id: "inbox", label: "Inbox", blurb: "Your own notifications.", keywords: "notifications inbox alerts unread" },
      { id: "telegram", label: "Telegram", blurb: "Pairing, links and test delivery.", keywords: "telegram notifications bot pairing chat alerts" },
    ],
  },
  {
    id: "integrations",
    label: "Integrations",
    sections: [
      { id: "connections", label: "Connections & Keys", blurb: "Provider credentials, status only.", keywords: "integrations connections credentials keys secrets llm providers" },
      { id: "webhooks", label: "Webhooks", blurb: "Outbound event subscriptions.", keywords: "webhooks integrations events developer subscriptions" },
      { id: "sources", label: "Knowledge Sources", blurb: "Research connectors.", keywords: "integrations knowledge sources connectors research" },
      { id: "trends", label: "Trend Sources", blurb: "Trend intake wiring.", keywords: "integrations trends sources intake developer" },
    ],
  },
];

export const SECTION_LOOKUP: Record<SectionId, { group: string; label: string; blurb: string }> =
  Object.fromEntries(
    SETTING_GROUPS.flatMap((g) =>
      g.sections.map((s) => [s.id, { group: g.label, label: s.label, blurb: s.blurb }]),
    ),
  ) as Record<SectionId, { group: string; label: string; blurb: string }>;

/* ==========================================================================
 * Helpers
 * ======================================================================= */

function when(value: string | null | undefined) {
  return value ? (
    value.replace("Z", " UTC").replace("T", " ")
  ) : (
    <span className="ym-muted">—</span>
  );
}

function yesNo(value: boolean | null | undefined): string {
  if (value === null || value === undefined) return "UNAVAILABLE";
  return value ? "yes" : "no";
}

function ReadFailure<T>({ name, path, query }: { name: string; path: string; query: QueryState<T> }) {
  if (!query.error) return null;
  return (
    <p className="ym-error" role="alert">
      {name} could not be read from {path} ({query.error}). Its figures read
      UNAVAILABLE rather than a default, so an unreachable setting store is not
      mistaken for a configured one.
    </p>
  );
}

function numberOrUnavailable(value: number | null | undefined): React.ReactNode {
  return value === null || value === undefined ? (
    <span className="ym-muted" title="Not set. This is not zero.">
      NOT SET
    </span>
  ) : (
    String(value)
  );
}

/* Any admin-role capability. The backend derives capabilities from the role, so
 * holding ANY admin-gated one is the affordance signal for "this operator is an
 * admin". The backend stays authoritative: a wrong guess renders as a 403,
 * which QueryBoundary reports as a refusal, never as breakage. */
const ADMIN_CAPABILITIES = ["publish.approve", "publish.execute", "brand.manage", "providers.manage"];

function useAdminGate(): { allowed: boolean; reason: string | null } {
  const { capabilities, capabilitiesKnown } = useSession();
  const allowed = !capabilitiesKnown || ADMIN_CAPABILITIES.some((c) => capabilities.includes(c));
  return {
    allowed,
    reason: allowed
      ? null
      : "Requires an admin role (owner or admin). The server enforces this — the controls below are disabled.",
  };
}

/* What a section panel reports upward so the header save-state is one honest
 * aggregate instead of per-form noise. */
export type SectionChrome = {
  onDirty: (id: SectionId, dirty: boolean) => void;
  trackSave: <T>(operation: () => Promise<T>) => Promise<T>;
  onSaved: () => void;
  onSaveFailed: (message: string | null) => void;
};

// Track the request in the shell, not a panel effect: navigation unmounts the
// panel, but it does not cancel the write or its eventual refusal.
function useSettingsMutation<TArgs, TResult>(
  chrome: SectionChrome,
  ...args: Parameters<typeof useMutation<TArgs, TResult>>
) {
  const [fn, options] = args;
  return useMutation<TArgs, TResult>((values) => chrome.trackSave(() => fn(values)), options);
}

/* Permission requirement line shown next to every admin-only write cluster. */
function PermNote({ gate, endpoint }: { gate: { allowed: boolean; reason: string | null }; endpoint: string }) {
  return (
    <p className="ymset-permnote" role="note">
      {gate.allowed
        ? `Write path ${endpoint} requires an admin role. The server enforces this.`
        : gate.reason}
    </p>
  );
}

/* ==========================================================================
 * Workspace
 * ======================================================================= */

function WorkspacePanel({ workspace, chrome, onSaved }: { workspace: QueryState<Workspace>; chrome: SectionChrome; onSaved: () => void }) {
  const [name, setName] = useState<string | null>(null);
  const [niche, setNiche] = useState<string | null>(null);
  const admin = useAdminGate();

  const current = workspace.data;
  const nameValue = name ?? current?.name ?? "";
  const nicheValue = niche ?? current?.niche ?? "";
  const dirty = current !== null && (nameValue !== current.name || nicheValue !== current.niche);

  useEffect(() => {
    chrome.onDirty("workspace", dirty);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dirty]);

  const save = useSettingsMutation<void, Workspace>(chrome,
    () =>
      wsApi.patch("", {
        name: nameValue.trim(),
        niche: nicheValue,
        // `WorkspaceBody` requires brand_voice; the existing value is echoed
        // back unchanged. It is never rendered, and never edited here.
        brand_voice: "",
      }) as Promise<Workspace>,
    {
      onSuccess: () => {
        // Drafts clear: what was sent is saved, and the reload below paints
        // the server's copy. Without this the form would stay dirty forever.
        setName(null);
        setNiche(null);
        chrome.onSaveFailed(null);
        chrome.onSaved();
        onSaved();
      },
      onError: (e) => chrome.onSaveFailed(e.message),
    },
  );


  useEffect(() => {
    chrome.onSaveFailed(save.error);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [save.error]);

  return (
    <>
      <ReadFailure name="The workspace" path="/workspaces/{id}" query={workspace} />
      <QueryBoundary query={workspace} skeletonRows={3}>
        {(d) => (
          <>
            <Grid min={190} gap="sm">
              <StatTile label="Slug" value={d.slug || "UNAVAILABLE"} hint="Derived from the name on create; not editable." source="GET /workspaces/{id}" />
              <StatTile label="Language" value={d.language || "NOT SET"} tone={d.language ? "neutral" : "unknown"} hint="Used for captions and voice selection." source="GET /workspaces/{id}" />
              <StatTile label="Timezone" value={d.timezone || "NOT SET"} tone={d.timezone ? "neutral" : "unknown"} hint="Placement and daily-budget windows resolve against this." source="GET /workspaces/{id}" />
              <StatTile label="Created" value={when(d.created_at)} source="GET /workspaces/{id}" />
            </Grid>
            <Field
              label="Name"
              value={nameValue}
              onChange={(e) => setName(e.target.value)}
              hint="PATCH /workspaces/{id} requires a non-empty name, so this is always sent whole."
              disabled={!admin.allowed}
            />
            <Field
              label="Niche"
              value={nicheValue}
              onChange={(e) => setNiche(e.target.value)}
              hint="Drives trend-source relevance scoring."
              disabled={!admin.allowed}
            />
            <div>
              <Button
                variant="primary"
                loading={save.pending}
                disabled={!dirty || nameValue.trim().length === 0 || !admin.allowed}
                onClick={() => void save.run(undefined)}
              >
                Save workspace
              </Button>
              {save.error ? <p className="ym-error">{save.error}</p> : null}
            </div>
            <PermNote gate={admin} endpoint="PATCH /workspaces/{id}" />
          </>
        )}
      </QueryBoundary>
    </>
  );
}

/* ==========================================================================
 * Appearance (read-only: the API exposes no write for these)
 * ======================================================================= */

function AppearancePanel({ workspace }: { workspace: QueryState<Workspace> }) {
  return (
    <>
      <QueryBoundary query={workspace} skeletonRows={3}>
        {(d) => {
          const keys = Object.keys(d.settings ?? {});
          return (
            <>
              <Grid min={190} gap="sm">
                <StatTile
                  label="Language"
                  value={d.language || "NOT SET"}
                  tone={d.language ? "neutral" : "unknown"}
                  hint="Drives captions, voice selection and localisation."
                  source="GET /workspaces/{id}"
                />
                <StatTile
                  label="Timezone"
                  value={d.timezone || "NOT SET"}
                  tone={d.timezone ? "neutral" : "unknown"}
                  hint="Placement and daily windows resolve against this."
                  source="GET /workspaces/{id}"
                />
              </Grid>
              <p className="ym-hint">
                These two are the only display settings the API exposes on the
                workspace. They are read-only here: `WorkspaceBody` accepts name,
                niche and brand_voice only, so there is no endpoint that would let
                this screen write them. Accent, logo and app name are the Brands
                feature's job and are not duplicated here.
              </p>
              <h3 className="ym-panel-title">Settings keys in use</h3>
              {keys.length === 0 ? (
                <p className="ym-hint">
                  No keys are set on this workspace's settings bag. That is an empty
                  configuration, not a failed read.
                </p>
              ) : (
                <ul>
                  {keys.map((k) => (
                    <li key={k} className="ym-notif-detail">
                      <code>{k}</code>
                    </li>
                  ))}
                </ul>
              )}
              <p className="ym-hint">
                Key NAMES only. <code>PUT /workspaces/{"{id}"}/settings</code> merges
                arbitrary values into this bag, and those values may hold
                operational configuration — so nothing from inside it is printed
                here.
              </p>
            </>
          );
        }}
      </QueryBoundary>
    </>
  );
}

/* ==========================================================================
 * Members
 * ======================================================================= */

function MembersPanel({ query }: { query: QueryState<{ items: Member[] }> }) {
  const columns: Column<Member>[] = [
    { key: "email", header: "Member", cell: (m) => <strong>{m.email || m.user_id}</strong> },
    { key: "role", header: "Role", cell: (m) => <Badge tone={m.role === "owner" ? "info" : "neutral"}>{humanize(m.role)}</Badge> },
    {
      key: "order",
      header: "Rank",
      align: "right",
      cell: () => `viewer < member < admin < owner`,
      hideBelow: "lg",
    },
    {
      key: "note",
      header: "Can attempt",
      cell: (m) =>
        m.role === "viewer" ? (
          <span className="ym-muted">reads only</span>
        ) : m.role === "member" ? (
          <span>content and review writes</span>
        ) : m.role === "admin" ? (
          <span>campaigns, publishing, brand, providers</span>
        ) : (
          <span>everything, including workspace deletion</span>
        ),
      hideBelow: "md",
    },
  ];

  return (
    <>
      <ReadFailure name="The member list" path="/workspaces/{id}/members" query={query} />
      <QueryBoundary query={query} skeletonRows={4}>
        {(d) => (
          <DataTable
            rows={d.items ?? []}
            columns={columns}
            rowKey={(m) => m.user_id}
            caption="Workspace members"
            empty="No member listed"
            emptyHint="The route returned no rows. Membership is what makes a workspace visible at all, so an empty list here means the caller is not a member of anything it can see."
          />
        )}
      </QueryBoundary>
      <p className="ym-hint">
        The role is the server's. Capabilities are DERIVED from it on the server
        and delivered to the client — the UI never re-implements the table. There
        is no invite endpoint; membership is managed outside this screen.
      </p>
    </>
  );
}

/* ==========================================================================
 * API keys
 * ======================================================================= */

function ApiKeysPanel({ query, chrome }: { query: QueryState<{ items: ApiKeyRow[] }>; chrome: SectionChrome }) {
  const admin = useAdminGate();
  const [name, setName] = useState("");
  const [role, setRole] = useState("member");
  const [minted, setMinted] = useState<{ id: string; api_key: string; name: string } | null>(null);

  const mint = useSettingsMutation<{ name: string; role: string }, { id: string; api_key: string; name: string }>(chrome,
    (body) => wsApi.post("/api-keys", body) as Promise<{ id: string; api_key: string; name: string }>,
    {
      onSuccess: (r) => {
        setMinted(r);
        setName("");
        chrome.onSaveFailed(null);
        chrome.onSaved();
        query.reload();
      },
      onError: (e) => chrome.onSaveFailed(e.message),
    },
  );

  const revoke = useSettingsMutation<string, unknown>(chrome,
    (id: string) => wsApi.post(`/api-keys/${encodeURIComponent(id)}/revoke`) as Promise<unknown>,
    {
      onSuccess: () => {
        chrome.onSaveFailed(null);
        chrome.onSaved();
        query.reload();
      },
      onError: (e) => chrome.onSaveFailed(e.message),
    },
  );


  const columns: Column<ApiKeyRow>[] = [
    { key: "name", header: "Key", cell: (k) => <strong>{k.name || "(unnamed)"}</strong> },
    { key: "role", header: "Role", cell: (k) => <Badge tone={k.role === "admin" ? "warning" : "neutral"}>{humanize(k.role)}</Badge> },
    {
      key: "state",
      header: "State",
      cell: (k) =>
        k.revoked ? (
          <Badge tone="danger" dot>
            REVOKED
          </Badge>
        ) : (
          <Badge tone="success" dot>
            ACTIVE
          </Badge>
        ),
    },
    {
      key: "used",
      header: "Last used",
      cell: (k) => (k.last_used_at ? when(k.last_used_at) : <span className="ym-muted">never used</span>),
      hideBelow: "md",
    },
    { key: "created", header: "Created", cell: (k) => when(k.created_at), hideBelow: "lg" },
    {
      key: "revoke",
      header: "Control",
      cell: (k) =>
        k.revoked ? (
          <span className="ym-muted">—</span>
        ) : (
          <DestructiveButton
            confirmLabel={`Revoke "${k.name || "this key"}". Anything using it stops authenticating immediately.`}
            disabled={!admin.allowed || revoke.pending}
            onConfirm={() => void revoke.run(k.id)}
          >
            Revoke
          </DestructiveButton>
        ),
    },
  ];

  return (
    <>
      <ReadFailure name="The API key list" path="/api-keys" query={query} />
      {mint.error ? <p className="ym-error">{mint.error}</p> : null}
      {revoke.error ? <p className="ym-error">{revoke.error}</p> : null}
      <QueryBoundary query={query} skeletonRows={3}>
        {(d) => (
          <DataTable
            rows={d.items ?? []}
            columns={columns}
            rowKey={(k) => k.id}
            caption="API keys"
            empty="No API key"
            emptyHint="No machine credential exists for this workspace. Revoking one is irreversible — minting a replacement is a separate, explicit act."
          />
        )}
      </QueryBoundary>
      <p className="ym-hint">
        Key metadata only. The plaintext is returned once, at mint time, and is
        never retrievable afterwards. The 12-character prefix is NOT shown here
        either: a leaked fingerprint makes brute-forcing a short key cheap.
      </p>
      <h3 className="ym-panel-title">Mint a key</h3>
      <Grid min={190} gap="sm">
        <Field label="Name" value={name} onChange={(e) => setName(e.target.value)} disabled={!admin.allowed} />
        <Select label="Role" value={role} onChange={(e) => setRole(e.target.value)} disabled={!admin.allowed}>
          <option value="viewer">viewer</option>
          <option value="member">member</option>
          <option value="admin">admin</option>
        </Select>
      </Grid>
      <div>
        <Button
          variant="primary"
          loading={mint.pending}
          disabled={!admin.allowed}
          onClick={() => void mint.run({ name: name.trim(), role })}
        >
          Mint key
        </Button>
      </div>
      {minted?.api_key ? (
        <Panel title="Key minted — copy it now" dense>
          <p className="ym-error" role="alert">
            This plaintext is shown ONCE and never again. Store it now; afterwards
            only metadata exists.
          </p>
          <p className="ym-notif-detail">
            <code>{minted.api_key}</code>
          </p>
          <Button variant="secondary" size="sm" onClick={() => setMinted(null)}>
            I have stored it
          </Button>
        </Panel>
      ) : null}
      <div className="ymset-dangerzone">
        <h4 className="ymset-dangerzone-title">Danger zone</h4>
        <p className="ym-hint">
          Revoking is immediate and irreversible: anything authenticating with the
          key stops at once. Use the Revoke control in the table above; each one
          states its blast radius before confirming.
        </p>
      </div>
      <PermNote gate={admin} endpoint="POST /api-keys and POST /api-keys/{id}/revoke" />
    </>
  );
}

/* ==========================================================================
 * Autonomy policy + decision preview
 * ======================================================================= */

function PolicyPanel({ query }: { query: QueryState<AutonomyPolicy> }) {
  const decision = useWsQuery<DecisionPreview>("/decision");

  const columns: Column<AutonomyRow & { mode: string }>[] = [
    { key: "mode", header: "Mode", cell: (r) => <Badge tone={r.mode === "AUTONOMOUS" ? "warning" : "neutral"}>{r.mode}</Badge> },
    { key: "suggests", header: "Suggest", cell: (r) => yesNo(r.suggests) },
    { key: "plan", header: "Plan items", cell: (r) => yesNo(r.creates_plan_items), hideBelow: "md" },
    { key: "drafts", header: "Campaign drafts", cell: (r) => yesNo(r.creates_campaign_drafts), hideBelow: "md" },
    { key: "research", header: "Research", cell: (r) => yesNo(r.starts_research) },
    { key: "schedules", header: "Schedule", cell: (r) => yesNo(r.schedules) },
    { key: "advances", header: "Advance production", cell: (r) => yesNo(r.advances_production) },
    {
      key: "publishes",
      header: "Publish",
      cell: (r) =>
        r.publishes ? (
          <Badge tone="unknown">UNEXPECTED</Badge>
        ) : (
          <Badge tone="unknown" dot title="No planning mode can publish.">
            NEVER
          </Badge>
        ),
    },
  ];

  return (
    <>
      <ReadFailure name="The autonomy table" path="/planner/policy" query={query} />
      <QueryBoundary query={query} skeletonRows={5}>
        {(d) => {
          const rows = Object.entries(d.table ?? {}).map(([mode, row]) => ({ mode, ...row }));
          return (
            <>
              <Grid min={190} gap="sm">
                <StatTile label="Modes" value={(d.modes ?? []).length} source="GET /planner/policy" />
                <StatTile label="Actions gated" value={(d.actions ?? []).length} source="GET /planner/policy" />
                <StatTile
                  label="Modes that can publish"
                  value="0"
                  tone="unknown"
                  hint={d.note}
                  source="GET /planner/policy"
                />
              </Grid>
              <DataTable
                rows={rows}
                columns={columns}
                rowKey={(r) => r.mode}
                caption="Planning autonomy table"
                empty="No autonomy mode reported"
                emptyHint="The policy table came back empty, so no planning action can be evaluated. That is not the same as every mode being restrictive."
              />
              <p className="ym-hint">
                Actions, verbatim from the server: {(d.actions ?? []).join(", ")}.
              </p>
            </>
          );
        }}
      </QueryBoundary>
      <h3 className="ym-panel-title">Next best action (live preview)</h3>
      <ReadFailure name="The decision preview" path="/decision" query={decision} />
      <QueryBoundary query={decision} skeletonRows={2}>
        {(d) => (
          <>
            <Grid min={190} gap="sm">
              <StatTile label="Action" value={d.action ? humanize(d.action) : "UNAVAILABLE"} source="GET /decision" />
            </Grid>
            {(d.reasons ?? []).length > 0 ? (
              <ul>
                {d.reasons.map((r, i) => (
                  <li key={i} className="ym-notif-detail">
                    {r}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="ym-hint">No reasons reported. That is an empty preview, not approval.</p>
            )}
            {(d.blockers ?? []).length > 0 ? (
              <p className="ym-error">
                Blockers: {(d.blockers ?? []).join(" · ")}
              </p>
            ) : null}
          </>
        )}
      </QueryBoundary>
      <p className="ym-hint">
        Read-only. The decision engine computes this; planning autonomy is not
        publishing autonomy and nothing here can publish.
      </p>
    </>
  );
}

/* ==========================================================================
 * Agents
 * ======================================================================= */

function AgentsPanel({ query, chrome }: { query: QueryState<{ items: AgentConfigRow[] }>; chrome: SectionChrome }) {
  const admin = useAdminGate();
  const [pending, setPending] = useState<string | null>(null);

  const toggle = useSettingsMutation<string, unknown>(chrome,
    (agentKey: string) => {
      const row = query.data?.items.find((a) => a.key === agentKey);
      return wsApi.put(`/agents/config/${encodeURIComponent(agentKey)}`, {
        enabled: !(row?.enabled ?? true),
      }) as Promise<unknown>;
    },
    {
      onSuccess: () => {
        setPending(null);
        chrome.onSaveFailed(null);
        chrome.onSaved();
        query.reload();
      },
      onError: (e) => {
        setPending(null);
        chrome.onSaveFailed(e.message);
      },
    },
  );


  const columns: Column<AgentConfigRow>[] = [
    { key: "title", header: "Agent", cell: (a) => <strong>{a.title}</strong> },
    {
      key: "enabled",
      header: "Enabled",
      cell: (a) =>
        a.enabled ? (
          <Badge tone="success" dot>
            ENABLED
          </Badge>
        ) : (
          <Badge tone="neutral" dot>
            DISABLED
          </Badge>
        ),
    },
    {
      key: "model",
      header: "Model",
      cell: (a) =>
        a.model ? (
          a.model
        ) : (
          <span className="ym-muted" title="No override; the agent uses the engine default.">
            engine default
          </span>
        ),
      hideBelow: "md",
    },
    { key: "timeout", header: "Timeout", align: "right", cell: (a) => `${a.timeout_seconds}s`, hideBelow: "md" },
    {
      key: "cap",
      header: "Cost cap",
      align: "right",
      cell: (a) => <Money usd={a.cost_limit_usd} />,
      hideBelow: "lg",
    },
    {
      key: "toggle",
      header: "Control",
      cell: (a) => (
        <Toggle
          checked={a.enabled}
          disabled={(toggle.pending && pending === a.key) || !admin.allowed}
          label={a.enabled ? "Disable" : "Enable"}
          onChange={() => {
            setPending(a.key);
            void toggle.run(a.key);
          }}
        />
      ),
    },
  ];

  return (
    <>
      <ReadFailure name="The agent configuration" path="/agents/config" query={query} />
      {toggle.error ? <p className="ym-error">{toggle.error}</p> : null}
      <QueryBoundary query={query} skeletonRows={8}>
        {(d) => (
          <DataTable
            rows={d.items ?? []}
            columns={columns}
            rowKey={(a) => a.key}
            caption="Agent configuration"
            maxHeight={520}
            empty="No agent registered"
            emptyHint="The registry declares no agent, so no stage of the pipeline can run."
          />
        )}
      </QueryBoundary>
      <p className="ym-hint">
        A null cost cap means no per-agent cap is set — it does not mean the agent
        costs nothing. The agent's `prompt_override` is never returned by the API
        and is not rendered anywhere on this screen.
      </p>
      <PermNote gate={admin} endpoint="PUT /agents/config/{key}" />
    </>
  );
}

/* ==========================================================================
 * Capacity
 * ======================================================================= */

const CAPACITY_FIELDS: { key: string; label: string; hint: string }[] = [
  { key: "longform_per_week", label: "Longform per week", hint: "Declared rate, not a measurement." },
  { key: "shorts_per_day", label: "Shorts per day", hint: "Declared rate, not a measurement." },
  { key: "ugc_per_day", label: "UGC per day", hint: "Declared rate, not a measurement." },
  { key: "localization_per_day", label: "Localizations per day", hint: "Declared rate, not a measurement." },
  { key: "render_hours_per_day", label: "Render hours per day", hint: "Declared rate, not a measurement." },
  { key: "review_slots_per_day", label: "Review slots per day", hint: "Declared rate, not a measurement." },
];

function CapacityPanel({ query, chrome }: { query: QueryState<CalendarState>; chrome: SectionChrome }) {
  const admin = useAdminGate();
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [locale, setLocale] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);

  const dirty = Object.values(draft).some((v) => v.trim() !== "") || locale.trim() !== "";
  useEffect(() => {
    chrome.onDirty("capacity", dirty);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dirty]);

  const save = useSettingsMutation<Record<string, unknown>, unknown>(chrome,
    (body) => wsApi.post("/planner/capacity", body) as Promise<unknown>,
    {
      onSuccess: () => {
        setDraft({});
        setLocale("");
        setFieldError(null);
        chrome.onSaveFailed(null);
        chrome.onSaved();
        query.reload();
      },
      onError: (e) => chrome.onSaveFailed(e.message),
    },
  );


  const submit = () => {
    const cap = query.data?.capacity;
    const selectedLocale = locale.trim();
    // POST replaces all fields. The calendar currently reads only the default
    // locale, so another locale must not inherit that record's rates.
    if (query.error || query.loading || query.refreshing || !cap || cap.locale !== selectedLocale ||
        typeof cap.notes !== "string" || CAPACITY_FIELDS.some((f) => {
          const value = cap[f.key as keyof CalendarCapacity];
          return typeof value !== "number" || !Number.isFinite(value) || value < 0;
        })) {
      setFieldError("Stored capacity for this locale is unavailable. Nothing was sent; untouched rates and notes cannot be preserved.");
      return;
    }
    const body: Record<string, unknown> = { locale: selectedLocale, notes: cap.notes };
    let hasRate = false;
    for (const f of CAPACITY_FIELDS) {
      const raw = (draft[f.key] ?? "").trim();
      if (raw === "") {
        body[f.key] = cap[f.key as keyof CalendarCapacity];
        continue;
      }
      const n = Number(raw);
      if (!Number.isFinite(n) || n < 0) {
        setFieldError(`${f.label} must be a non-negative number. Nothing was sent.`);
        return;
      }
      body[f.key] = n;
      hasRate = true;
    }
    if (!hasRate) {
      setFieldError("Nothing to declare: fill at least one rate. Blank fields are left untouched, never zeroed.");
      return;
    }
    setFieldError(null);
    void save.run(body);
  };

  return (
    <>
      <ReadFailure name="The calendar and capacity" path="/planner/calendar" query={query} />
      {fieldError ? <p className="ym-error">{fieldError}</p> : null}
      {save.error ? <p className="ym-error">{save.error}</p> : null}
      <QueryBoundary query={query} skeletonRows={4}>
        {(d) => {
          const cap = d.capacity ?? ({} as CalendarCapacity);
          const remaining = Object.entries(d.remaining ?? {});
          return (
            <>
              <Grid min={190} gap="sm">
                <StatTile label="Declared" value={cap.declared ? "yes" : "no"} tone={cap.declared ? "success" : "unknown"} hint="An undeclared pool is unbounded, not zero." source="GET /planner/calendar" />
                <StatTile label="Locale" value={cap.locale || "UNSET"} source="GET /planner/calendar" />
                <StatTile label="Shorts per day" value={cap.shorts_per_day ?? "UNAVAILABLE"} unavailable={cap.shorts_per_day === undefined} source="GET /planner/calendar" />
              </Grid>
              {remaining.length > 0 ? (
                <DataTable
                  rows={remaining}
                  columns={[
                    { key: "pool", header: "Pool", cell: ([k]) => humanize(k) },
                    { key: "left", header: "Remaining", align: "right", cell: ([, v]) => numberOrUnavailable(v) },
                  ]}
                  rowKey={([k]) => k}
                  caption="Remaining capacity"
                  empty="No remaining capacity reported"
                />
              ) : (
                <p className="ym-hint">No remaining-capacity pools reported for this horizon.</p>
              )}
              <p className="ym-hint">
                {(d.entries ?? []).length} placed entries in this window. Columns are
                rates scaled to the horizon; an unset pool is unbounded, not zero.
              </p>
            </>
          );
        }}
      </QueryBoundary>
      <h3 className="ym-panel-title">Declare capacity</h3>
      <Field label="Locale" value={locale} onChange={(e) => setLocale(e.target.value)} hint="Empty means the workspace default locale." disabled={!admin.allowed} />
      {CAPACITY_FIELDS.map((f) => (
        <Field
          key={f.key}
          label={f.label}
          type="number"
          step="0.5"
          min="0"
          value={draft[f.key] ?? ""}
          onChange={(e) => setDraft((prev) => ({ ...prev, [f.key]: e.target.value }))}
          hint={`${f.hint} Blank leaves the stored rate untouched; enter 0 to clear the pool (unbounded).`}
          disabled={!admin.allowed}
        />
      ))}
      <div>
        <Button variant="primary" loading={save.pending} disabled={!dirty || !admin.allowed} onClick={submit}>
          Declare capacity
        </Button>
      </div>
      <PermNote gate={admin} endpoint="POST /planner/capacity" />
    </>
  );
}

/* ==========================================================================
 * Budgets
 * ======================================================================= */

const SAFETY_FIELDS: { key: keyof Safety; label: string; hint: string; money?: boolean }[] = [
  { key: "daily_budget_usd", label: "Daily budget", hint: "Ceiling on 24h spend.", money: true },
  { key: "monthly_budget_usd", label: "Monthly budget", hint: "Ceiling on calendar-month spend.", money: true },
  { key: "per_video_budget_usd", label: "Per-video budget", hint: "Ceiling per produced video.", money: true },
  { key: "max_videos_per_day", label: "Max videos per day", hint: "Safety cap." },
  { key: "max_uploads_per_hour", label: "Max uploads per hour", hint: "Safety cap." },
  { key: "max_concurrent_renders", label: "Max concurrent renders", hint: "Capacity the supervisor respects." },
  { key: "max_render_attempts", label: "Max render attempts", hint: "Beyond this the render fails." },
  { key: "max_consecutive_failures", label: "Max consecutive failures", hint: "The circuit breaker's emergency threshold." },
  { key: "min_qc_score", label: "Minimum QC score", hint: "Below this, content regenerates." },
  { key: "produce_score_threshold", label: "Produce score threshold", hint: "Below this the supervisor waits rather than spends." },
  { key: "require_human_review_risk_above", label: "Human review above risk", hint: "At or above this, a human must approve." },
  { key: "similarity_threshold", label: "Similarity threshold", hint: "Above this, output is treated as a repetition." },
];

function BudgetsPanel({
  safety,
  costs,
  chrome,
  onSaved,
}: {
  safety: QueryState<{ safety: Safety }>;
  costs: QueryState<CostSummary>;
  chrome: SectionChrome;
  onSaved: () => void;
}) {
  const admin = useAdminGate();
  const [draft, setDraft] = useState<Partial<Record<keyof Safety, string | boolean>>>({});
  const [fieldError, setFieldError] = useState<string | null>(null);
  const publishBlock = blockedReason("publish.approve");

  const dirty = Object.keys(draft).length > 0;
  useEffect(() => {
    chrome.onDirty("budgets", dirty);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dirty]);

  const save = useSettingsMutation<Partial<Safety>, { safety: Safety }>(chrome,
    (updates) => wsApi.put("/safety", { safety: updates }) as Promise<{ safety: Safety }>,
    {
      onSuccess: () => {
        setDraft({});
        setFieldError(null);
        chrome.onSaveFailed(null);
        chrome.onSaved();
        onSaved();
      },
      onError: (e) => chrome.onSaveFailed(e.message),
    },
  );


  useEffect(() => {
    chrome.onSaveFailed(save.error);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [save.error]);

  const submit = () => {
    const updates: Partial<Safety> = {};
    for (const [key, raw] of Object.entries(draft)) {
      if (typeof raw === "boolean") {
        (updates as Record<string, unknown>)[key] = raw;
        continue;
      }
      const text = String(raw).trim();
      if (text === "" || !Number.isFinite(Number(text))) {
        setFieldError(
          `${key} is blank or not a number. Nothing was sent — clearing a field is ` +
            `not a request to set it to zero, and a budget of 0 is a real budget.`,
        );
        return;
      }
      (updates as Record<string, unknown>)[key] = Number(text);
    }
    setFieldError(null);
    void save.run(updates);
  };

  return (
    <>
      <ReadFailure name="The safety policy" path="/safety" query={safety} />
      {fieldError ? <p className="ym-error">{fieldError}</p> : null}
      {save.error ? <p className="ym-error">{save.error}</p> : null}
      <QueryBoundary query={safety} skeletonRows={8}>
        {(d) => (
          <>
            <Grid min={190} gap="sm">
              <StatTile
                label="Spent, last 24h"
                value={<Money usd={costs.data?.spent_last_24h_usd} />}
                unavailable={costs.data === null || costs.data.spent_last_24h_usd === null}
                hint={
                  costs.data && costs.data.spent_last_24h_usd === null
                    ? `${costs.data.spent_last_24h_unknown_exposure_rows} cost row(s) record an exposure nobody can price.`
                    : undefined
                }
                source="GET /costs"
              />
              <StatTile
                label="Remaining against the daily budget"
                value={<Money usd={costs.data?.remaining_usd} tone={costs.data?.within_budget ? "success" : "danger"} />}
                unavailable={costs.data === null}
                tone={costs.data === null ? "neutral" : costs.data.within_budget ? "success" : "danger"}
                hint={costs.data && !costs.data.within_budget ? "Over budget: paid work is refused." : undefined}
                source="GET /costs"
              />
            </Grid>
            {SAFETY_FIELDS.map((f) => (
              <Field
                key={f.key}
                label={
                  <>
                    {f.label}
                    {f.money ? <span className="ym-muted"> (USD)</span> : null}
                  </>
                }
                type="number"
                step={f.money ? "0.01" : f.key === "similarity_threshold" ? "0.01" : "1"}
                value={
                  draft[f.key] !== undefined
                    ? String(draft[f.key])
                    : String(d.safety[f.key])
                }
                onChange={(e) =>
                  setDraft((prev) => ({ ...prev, [f.key]: e.target.value }))
                }
                hint={f.hint}
                disabled={!admin.allowed}
              />
            ))}
            <Toggle
              checked={
                typeof draft.require_approval_before_publish === "boolean"
                  ? draft.require_approval_before_publish
                  : d.safety.require_approval_before_publish
              }
              onChange={(v) =>
                setDraft((prev) => ({ ...prev, require_approval_before_publish: v }))
              }
              label="Require explicit approval before any publish"
              disabled={!admin.allowed}
            />
            {publishBlock ? (
              <p className="ym-error">{publishBlock}</p>
            ) : null}
            <div>
              <Button
                variant="primary"
                loading={save.pending}
                disabled={!dirty || !admin.allowed}
                onClick={submit}
              >
                Save safety policy
              </Button>
            </div>
            <PermNote gate={admin} endpoint="PUT /safety — the validated path. The generic PUT /settings merge REFUSES a `safety` key with a 422 pointing here." />
          </>
        )}
      </QueryBoundary>
    </>
  );
}

/* ==========================================================================
 * Cost analytics
 * ======================================================================= */

function CostsPanel({ summary, intel }: { summary: QueryState<CostSummary>; intel: QueryState<CostIntelligence> }) {
  return (
    <>
      <ReadFailure name="The cost summary" path="/costs" query={summary} />
      <QueryBoundary query={summary} skeletonRows={3}>
        {(d) => (
          <>
            <Grid min={190} gap="sm">
              <StatTile label="Spent, last 24h" value={<Money usd={d.spent_last_24h_usd} />} unavailable={d.spent_last_24h_usd === null} hint={d.spent_last_24h_usd === null ? `${d.spent_last_24h_unknown_exposure_rows} unknown-exposure row(s): priced rows are below, the total is not a sum.` : undefined} source="GET /costs" />
              <StatTile label="Remaining" value={<Money usd={d.remaining_usd} />} source="GET /costs" />
              <StatTile label="Within budget" value={d.within_budget ? "yes" : "NO"} tone={d.within_budget ? "success" : "danger"} source="GET /costs" />
            </Grid>
            <DataTable
              rows={Object.entries(d.last_24h_by_category ?? {})}
              columns={[
                { key: "cat", header: "Category", cell: ([c]) => humanize(c) },
                { key: "amount", header: "Spend", align: "right", cell: ([, v]) => <Money usd={v} /> },
              ]}
              rowKey={([c]) => c}
              caption="Spend by category"
              empty="No cost ledgered in this window"
              emptyHint="Nothing is recorded against this workspace in the last 24 hours. During a paid operation that is a reason to check the cost was written — not proof nothing was spent."
            />
          </>
        )}
      </QueryBoundary>
      <h3 className="ym-panel-title">Unit economics</h3>
      <ReadFailure name="The cost intelligence" path="/costs/intelligence" query={intel} />
      <QueryBoundary query={intel} skeletonRows={4}>
        {(d) => (
          <>
            <Grid min={190} gap="sm">
              <StatTile label="Lifetime cost" value={<Money usd={d.total_cost_usd} />} unavailable={d.total_cost_usd === null} source="GET /costs/intelligence" />
              <StatTile label="Per video" value={<Money usd={d.per_video_usd} />} unavailable={d.per_video_usd === null} source="GET /costs/intelligence" />
              <StatTile label="Per publication" value={<Money usd={d.per_publication_usd} />} unavailable={d.per_publication_usd === null} source="GET /costs/intelligence" />
              <StatTile label="Per 1000 views" value={<Money usd={d.cost_per_1000_views_usd} />} unavailable={d.cost_per_1000_views_usd === null} source="GET /costs/intelligence" />
            </Grid>
            {d.estimated_return_usd !== null && d.estimated_return_usd !== undefined ? (
              <StatTile label="Estimated return" value={<Money usd={d.estimated_return_usd} />} source="GET /costs/intelligence" />
            ) : (
              <p className="ym-hint">
                No estimated return is shown: that figure requires real revenue
                data and is never fabricated.
              </p>
            )}
            <DataTable
              rows={Object.entries(d.by_agent ?? {})}
              columns={[
                { key: "agent", header: "Agent", cell: ([k]) => humanize(k) },
                { key: "cost", header: "Cost", align: "right", cell: ([, v]) => <Money usd={v} /> },
              ]}
              rowKey={([k]) => k}
              caption="Spend by agent"
              empty="No per-agent spend recorded"
              emptyHint="No agent run has ledgered cost against this workspace."
            />
          </>
        )}
      </QueryBoundary>
    </>
  );
}

/* ==========================================================================
 * Publishing accounts
 * ======================================================================= */

function AccountsPanel({ query, chrome }: { query: QueryState<{ items: SocialAccount[] }>; chrome: SectionChrome }) {
  const allowed = can("publish.execute");
  const connectBlock = blockedReason("publish.execute");
  const [platform, setPlatform] = useState("youtube");
  const [displayName, setDisplayName] = useState("");
  const [accessToken, setAccessToken] = useState("");
  const [refreshToken, setRefreshToken] = useState("");

  const connect = useSettingsMutation(chrome,
    () =>
      wsApi.post("/publishing/accounts", {
        platform,
        display_name: displayName.trim(),
        access_token: accessToken,
        refresh_token: refreshToken,
      }) as Promise<unknown>,
    {
      onSuccess: () => {
        setDisplayName("");
        setAccessToken("");
        setRefreshToken("");
        chrome.onSaveFailed(null);
        chrome.onSaved();
        query.reload();
      },
      onError: (e) => chrome.onSaveFailed(e.message),
    },
  );

  const disconnect = useSettingsMutation<string, unknown>(chrome,
    (id: string) => wsApi.del(`/publishing/accounts/${encodeURIComponent(id)}`) as Promise<unknown>,
    {
      onSuccess: () => {
        chrome.onSaveFailed(null);
        chrome.onSaved();
        query.reload();
      },
      onError: (e) => chrome.onSaveFailed(e.message),
    },
  );


  const columns: Column<SocialAccount>[] = [
    { key: "platform", header: "Platform", cell: (a) => <Badge tone="info">{humanize(a.platform)}</Badge> },
    { key: "name", header: "Account", cell: (a) => <strong>{a.display_name || a.platform}</strong> },
    {
      key: "status",
      header: "Status",
      cell: (a) => <Badge tone={a.status === "connected" ? "success" : "danger"} dot>{humanize(a.status)}</Badge>,
    },
    { key: "expires", header: "Token expires", cell: (a) => when(a.token_expires_at), hideBelow: "md" },
    {
      key: "control",
      header: "Control",
      cell: (a) => (
        <DestructiveButton
          confirmLabel={`Disconnect the ${a.platform} account "${a.display_name}". Publishing to it stops immediately.`}
          disabled={!allowed || disconnect.pending}
          onConfirm={() => void disconnect.run(a.id)}
        >
          Disconnect
        </DestructiveButton>
      ),
    },
  ];

  return (
    <>
      <ReadFailure name="The account list" path="/publishing/accounts" query={query} />
      {connect.error ? <p className="ym-error">{connect.error}</p> : null}
      {disconnect.error ? <p className="ym-error">{disconnect.error}</p> : null}
      <QueryBoundary query={query} skeletonRows={3}>
        {(d) => (
          <DataTable
            rows={d.items ?? []}
            columns={columns}
            rowKey={(a) => a.id}
            caption="Connected platform accounts"
            empty="No connected account"
            emptyHint="No real publishing path exists until an account is connected or a relay key is added under Connections & Keys."
          />
        )}
      </QueryBoundary>
      <h3 className="ym-panel-title">Connect an account</h3>
      {connectBlock ? <p className="ym-error" role="alert">{connectBlock}</p> : null}
      <Grid min={190} gap="sm">
        <Select label="Platform" value={platform} onChange={(e) => setPlatform(e.target.value)} disabled={!allowed}>
          <option value="youtube">YouTube</option>
          <option value="tiktok">TikTok</option>
          <option value="facebook">Facebook</option>
          <option value="instagram">Instagram</option>
        </Select>
        <Field label="Display name" value={displayName} onChange={(e) => setDisplayName(e.target.value)} disabled={!allowed} />
      </Grid>
      <Field label="Access token" type="password" value={accessToken} onChange={(e) => setAccessToken(e.target.value)} hint="Stored encrypted server-side. Never displayed back." disabled={!allowed} />
      <Field label="Refresh token" type="password" value={refreshToken} onChange={(e) => setRefreshToken(e.target.value)} hint="Stored encrypted server-side. Never displayed back." disabled={!allowed} />
      <div>
        <Button variant="primary" loading={connect.pending} disabled={!allowed} onClick={() => void connect.run(undefined)}>
          Connect account
        </Button>
      </div>
      <div className="ymset-dangerzone">
        <h4 className="ymset-dangerzone-title">Danger zone</h4>
        <p className="ym-hint">
          Disconnecting removes the stored tokens at once; scheduled posts for
          that account will fail until it is reconnected.
        </p>
      </div>
      <p className="ymset-permnote" role="note">
        {allowed
          ? "Write path POST /publishing/accounts and DELETE /publishing/accounts/{id} require an admin role. The server enforces this."
          : connectBlock}
      </p>
    </>
  );
}

/* ==========================================================================
 * Scheduling (read-only)
 * ======================================================================= */

function SchedulingPanel({ query }: { query: QueryState<{ items: ScheduleEntry[] }> }) {
  const columns: Column<ScheduleEntry>[] = [
    { key: "platform", header: "Platform", cell: (e) => <Badge tone="info">{humanize(e.platform)}</Badge> },
    { key: "run", header: "Run at", cell: (e) => when(e.run_at) },
    {
      key: "status",
      header: "Status",
      cell: (e) => <Badge tone={e.status === "FAILED" ? "danger" : "info"} dot>{humanize(e.status)}</Badge>,
    },
    { key: "campaign", header: "Campaign", cell: (e) => e.campaign_id ? <span className="ym-notif-detail">{e.campaign_id.slice(0, 8)}</span> : <span className="ym-muted">—</span>, hideBelow: "md" },
  ];

  return (
    <>
      <ReadFailure name="The schedule" path="/calendar" query={query} />
      <QueryBoundary query={query} skeletonRows={4}>
        {(d) => (
          <DataTable
            rows={d.items ?? []}
            columns={columns}
            rowKey={(e) => e.id}
            caption="Scheduled placements"
            empty="Nothing scheduled"
            emptyHint="No PENDING, QUEUED, DISPATCHING or FAILED entry exists. DONE entries disappear once the post exists; CANCELLED entries are terminal and hidden."
          />
        )}
      </QueryBoundary>
      <p className="ym-hint">
        Read-only. Placements are written by the planning and content flows;
        publishing itself still passes the approval gate
        (`require_approval_before_publish` in Budgets & Safety). There is no
        default-mode endpoint — modes derive from the safety policy and the
        autonomy table.
      </p>
    </>
  );
}

/* ==========================================================================
 * Providers (TTS / images / video engine status + engine config + live tests)
 * ======================================================================= */

type TtsStatus = {
  provider?: string;
  healthy?: boolean;
  voices?: { name?: string; id?: string; voice?: string }[];
  error?: string | null;
};
type ImagesStatus = { provider?: string; healthy?: boolean; error?: string | null };
type VideoEngineStatus = {
  engine: string;
  base_url: string;
  timeout_seconds: number;
  sources: string[];
  healthy: boolean;
  version: string | null;
  capabilities: string[];
};

function ProvidersPanel({
  tts,
  images,
  engine,
  chrome,
}: {
  tts: QueryState<TtsStatus>;
  images: QueryState<ImagesStatus>;
  engine: QueryState<VideoEngineStatus>;
  chrome: SectionChrome;
}) {
  const manage = can("providers.manage");
  const blocked = blockedReason("providers.manage");
  const [baseUrl, setBaseUrl] = useState<string | null>(null);
  const [timeout, setTimeout] = useState<string | null>(null);
  const [llmResult, setLlmResult] = useState<string | null>(null);
  const [relayResult, setRelayResult] = useState<string | null>(null);

  const engineDirty =
    (baseUrl !== null && baseUrl !== (engine.data?.base_url ?? "")) ||
    (timeout !== null && timeout !== String(engine.data?.timeout_seconds ?? ""));
  useEffect(() => {
    chrome.onDirty("providers", engineDirty);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [engineDirty]);

  const saveEngine = useSettingsMutation<{ base_url?: string; timeout_seconds?: number }, VideoEngineStatus>(chrome,
    (body) => wsApi.put("/connections/video-engine", body) as Promise<VideoEngineStatus>,
    {
      onSuccess: (r) => {
        setBaseUrl(null);
        setTimeout(null);
        chrome.onSaveFailed(null);
        chrome.onSaved();
        engine.setData(r);
      },
      onError: (e) => chrome.onSaveFailed(e.message),
    },
  );

  const testLlm = useSettingsMutation<void, { ok: boolean; mode: string; detail: string }>(chrome,
    () => wsApi.post("/connections/test-llm") as Promise<{ ok: boolean; mode: string; detail: string }>,
    {
      onSuccess: (r) => {
        setLlmResult(`${r.ok ? "OK" : "NOT OK"} (${r.mode}): ${r.detail}`);
        chrome.onSaveFailed(null);
      },
      onError: (e) => {
        setLlmResult(`Test failed: ${e.message}`);
        chrome.onSaveFailed(e.message);
      },
    },
  );

  const testRelay = useSettingsMutation<void, { ok: boolean; detail: string; relay_valid: boolean | null }>(chrome,
    () => wsApi.post("/connections/test-publishing") as Promise<{ ok: boolean; detail: string; relay_valid: boolean | null }>,
    {
      onSuccess: (r) => {
        setRelayResult(`${r.ok ? "OK" : "NOT OK"}: ${r.detail || `relay_valid=${String(r.relay_valid)}`}`);
        chrome.onSaveFailed(null);
      },
      onError: (e) => {
        setRelayResult(`Test failed: ${e.message}`);
        chrome.onSaveFailed(e.message);
      },
    },
  );


  const submitEngine = () => {
    const body: { base_url?: string; timeout_seconds?: number } = {};
    if (baseUrl !== null) {
      if (baseUrl.trim() !== "" && !/https?:\/\//.test(baseUrl)) {
        chrome.onSaveFailed("base_url must start with http(s)://. Nothing was sent.");
        return;
      }
      body.base_url = baseUrl;
    }
    if (timeout !== null) {
      const n = Number(timeout);
      if (!Number.isFinite(n)) {
        chrome.onSaveFailed("timeout_seconds is not a number. Nothing was sent.");
        return;
      }
      body.timeout_seconds = n;
    }
    chrome.onSaveFailed(null);
    void saveEngine.run(body);
  };

  return (
    <>
      {blocked ? <p className="ym-error" role="alert">{blocked} Provider status needs an admin role.</p> : null}
      <h3 className="ym-panel-title">Narration (TTS)</h3>
      <ReadFailure name="The TTS status" path="/connections/tts" query={tts} />
      <QueryBoundary query={tts} skeletonRows={2}>
        {(d) => (
          <Grid min={190} gap="sm">
            <StatTile label="Provider" value={d.provider || "UNAVAILABLE"} unavailable={!d.provider} source="GET /connections/tts" />
            <StatTile label="Healthy" value={d.healthy ? "yes" : "no"} tone={d.healthy ? "success" : "danger"} hint={d.error || undefined} source="GET /connections/tts" />
            <StatTile label="Voices" value={(d.voices ?? []).length} source="GET /connections/tts" />
          </Grid>
        )}
      </QueryBoundary>
      <h3 className="ym-panel-title">Image generation</h3>
      <ReadFailure name="The image status" path="/connections/images" query={images} />
      <QueryBoundary query={images} skeletonRows={2}>
        {(d) => (
          <Grid min={190} gap="sm">
            <StatTile label="Provider" value={d.provider || "UNAVAILABLE"} unavailable={!d.provider} source="GET /connections/images" />
            <StatTile label="Healthy" value={d.healthy ? "yes" : "no"} tone={d.healthy ? "success" : "danger"} hint={d.error || undefined} source="GET /connections/images" />
          </Grid>
        )}
      </QueryBoundary>
      <h3 className="ym-panel-title">Video engine</h3>
      <ReadFailure name="The video engine status" path="/connections/video-engine" query={engine} />
      {saveEngine.error ? <p className="ym-error">{saveEngine.error}</p> : null}
      <QueryBoundary query={engine} skeletonRows={3}>
        {(d) => (
          <>
            <Grid min={190} gap="sm">
              <StatTile label="Engine" value={d.engine || "UNAVAILABLE"} source="GET /connections/video-engine" />
              <StatTile label="Healthy" value={d.healthy ? "yes" : "no"} tone={d.healthy ? "success" : "danger"} source="GET /connections/video-engine" />
              <StatTile label="Version" value={d.version || "UNAVAILABLE"} unavailable={!d.version} source="GET /connections/video-engine" />
            </Grid>
            <p className="ym-hint">
              Capabilities: {(d.capabilities ?? []).join(", ") || "none reported"}.
              Concurrency (`max_concurrent_renders`) lives in Safety settings, not here.
            </p>
            <Field
              label="Base URL"
              value={baseUrl ?? d.base_url ?? ""}
              onChange={(e) => setBaseUrl(e.target.value)}
              hint="Must start with http(s)://. Blank clears the override."
              disabled={!manage}
            />
            <Field
              label="Timeout (seconds)"
              type="number"
              value={timeout ?? String(d.timeout_seconds ?? "")}
              onChange={(e) => setTimeout(e.target.value)}
              hint="Server range 60–7200; the server enforces it."
              disabled={!manage}
            />
            <div>
              <Button variant="primary" loading={saveEngine.pending} disabled={!engineDirty || !manage} onClick={submitEngine}>
                Save video engine
              </Button>
            </div>
          </>
        )}
      </QueryBoundary>
      <h3 className="ym-panel-title">Live checks</h3>
      <p className="ym-hint">
        Structured reachability checks. They report status; they never return
        credentials.
      </p>
      <div>
        <Button variant="secondary" loading={testLlm.pending} disabled={!manage} onClick={() => void testLlm.run(undefined)}>
          Test LLM
        </Button>{" "}
        <Button variant="secondary" loading={testRelay.pending} disabled={!manage} onClick={() => void testRelay.run(undefined)}>
          Test publishing path
        </Button>
      </div>
      {llmResult ? <p className="ym-notif-detail">LLM: {llmResult}</p> : null}
      {relayResult ? <p className="ym-notif-detail">Publishing: {relayResult}</p> : null}
      <p className="ym-hint">
        TTS and image live-generation tests are deliberately not here: they spend
        real provider budget and return raw bytes. Status is above; testing stays
        on the Providers surface.
      </p>
      <p className="ymset-permnote" role="note">
        {manage
          ? "Write paths PUT /connections/video-engine, POST /connections/test-llm and POST /connections/test-publishing require an admin role. The server enforces this."
          : blocked}
      </p>
    </>
  );
}

/* ==========================================================================
 * Music
 * ======================================================================= */

function MusicPanel({ policy, providers, chrome }: { policy: QueryState<MusicPolicy>; providers: QueryState<{ items: { key: string; available: boolean; detail: string }[]; available: string[] }>; chrome: SectionChrome }) {
  const admin = useAdminGate();
  const [generate, setGenerate] = useState<boolean | null>(null);
  const [providerKey, setProviderKey] = useState<string | null>(null);

  const dirty = generate !== null || (providerKey !== null && providerKey !== "");
  useEffect(() => {
    chrome.onDirty("music", dirty);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dirty]);

  const save = useSettingsMutation<{ generate?: boolean; provider_key?: string }, MusicPolicy>(chrome,
    (body) => wsApi.put("/music/policy", body) as Promise<MusicPolicy>,
    {
      onSuccess: (r) => {
        setGenerate(null);
        setProviderKey(null);
        chrome.onSaveFailed(null);
        chrome.onSaved();
        policy.setData(r);
      },
      onError: (e) => chrome.onSaveFailed(e.message),
    },
  );


  return (
    <>
      <ReadFailure name="The music policy" path="/music/policy" query={policy} />
      {save.error ? <p className="ym-error">{save.error}</p> : null}
      <QueryBoundary query={policy} skeletonRows={3}>
        {(d) => (
          <>
            <Grid min={190} gap="sm">
              <StatTile label="Generate" value={d.generate ? "OPTED IN" : "not opted in"} tone={d.generate ? "success" : "neutral"} hint={d.reason || undefined} source="GET /music/policy" />
              <StatTile label="Provider" value={d.provider_key || "UNSET"} source="GET /music/policy" />
              <StatTile label="Brand refusal" value={d.brand_disabled ? "REFUSED" : "none"} tone={d.brand_disabled ? "danger" : "neutral"} hint="A brand refusal cannot be overridden here." source="GET /music/policy" />
            </Grid>
            {(d.forbidden_genres ?? []).length > 0 ? (
              <p className="ym-hint">Forbidden by the brand: {(d.forbidden_genres ?? []).join(", ")}.</p>
            ) : null}
            <Toggle
              checked={generate ?? d.generate}
              onChange={setGenerate}
              label="Opt in to generated music"
              disabled={!admin.allowed}
            />
            <QueryBoundary query={providers} skeletonRows={2}>
              {(p) => (
                <Select
                  label="Provider"
                  value={providerKey ?? d.provider_key ?? ""}
                  onChange={(e) => setProviderKey(e.target.value)}
                  disabled={!admin.allowed}
                >
                  <option value="">No preference</option>
                  {(p.items ?? []).map((item) => (
                    <option key={item.key} value={item.key}>
                      {item.key}{item.available ? "" : " (unavailable)"}
                    </option>
                  ))}
                </Select>
              )}
            </QueryBoundary>
            <div>
              <Button
                variant="primary"
                loading={save.pending}
                disabled={!dirty || !admin.allowed}
                onClick={() => void save.run({
                  ...(generate !== null ? { generate } : {}),
                  ...(providerKey ? { provider_key: providerKey } : {}),
                })}
              >
                Save music policy
              </Button>
            </div>
            <p className="ym-hint">
              generate=false is a recorded decision, not a failure: unset policy
              means nobody opted in, so no money is spent. Unknown provider keys
              are refused with a 422, never silently swapped.
            </p>
          </>
        )}
      </QueryBoundary>
      <PermNote gate={admin} endpoint="PUT /music/policy" />
    </>
  );
}

/* ==========================================================================
 * Storage: retention (admin read+write) + assets (viewer read)
 * ======================================================================= */

const RETENTION_FIELDS: { key: string; label: string }[] = [
  { key: "audit_retention_days", label: "Audit retention (days)" },
  { key: "render_retention_days", label: "Render retention (days)" },
  { key: "temp_asset_retention_days", label: "Temporary asset retention (days)" },
  { key: "export_retention_days", label: "Export retention (days)" },
];

function StoragePanel({ retention, assets, chrome }: { retention: QueryState<Retention>; assets: QueryState<{ items: AssetItem[] }>; chrome: SectionChrome }) {
  const admin = useAdminGate();
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [fieldError, setFieldError] = useState<string | null>(null);

  const dirty = Object.values(draft).some((v) => v !== "");
  useEffect(() => {
    chrome.onDirty("storage", dirty);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dirty]);

  const save = useSettingsMutation<Record<string, number | null>, Retention>(chrome,
    (body) => wsApi.put("/retention", body) as Promise<Retention>,
    {
      onSuccess: (r) => {
        setDraft({});
        setFieldError(null);
        chrome.onSaveFailed(null);
        chrome.onSaved();
        retention.setData(r);
      },
      onError: (e) => chrome.onSaveFailed(e.message),
    },
  );


  const submit = () => {
    const body: Record<string, number | null> = {};
    for (const f of RETENTION_FIELDS) {
      const raw = (draft[f.key] ?? "").trim();
      if (raw === "") {
        // Untouched fields are not sent at all; only edited fields are.
        if (!(f.key in draft)) continue;
        // An explicitly cleared field means keep forever (NULL), which the
        // API defines as a real value — never as zero.
        body[f.key] = null;
        continue;
      }
      const n = Number(raw);
      if (!Number.isInteger(n) || n < 0 || n > 3650) {
        setFieldError(`${f.label} must be a whole number of days, 0–3650. Nothing was sent.`);
        return;
      }
      body[f.key] = n;
    }
    if (Object.keys(body).length === 0) {
      setFieldError("Nothing to save: edit at least one field first.");
      return;
    }
    setFieldError(null);
    void save.run(body);
  };

  return (
    <>
      <ReadFailure name="The retention policy" path="/retention" query={retention} />
      {fieldError ? <p className="ym-error">{fieldError}</p> : null}
      {save.error ? <p className="ym-error">{save.error}</p> : null}
      <QueryBoundary query={retention} skeletonRows={4}>
        {(d) => (
          <>
            <DataTable
              rows={RETENTION_FIELDS.map((f) => ({ key: f.key, label: f.label, value: d[f.key] ?? null }))}
              columns={[
                { key: "policy", header: "Policy", cell: (r) => r.label },
                {
                  key: "value",
                  header: "Days",
                  align: "right",
                  cell: (r) => numberOrUnavailable(r.value),
                },
              ]}
              rowKey={(r) => r.key}
              caption="Retention policy"
              empty="No retention policy reported"
              emptyHint="No retention window can be stated."
            />
            <p className="ym-hint">
              A NULL day count means keep forever — a real, deliberately-set
              value, not a missing one. To set it, clear the field and save.
            </p>
            {RETENTION_FIELDS.map((f) => (
              <Field
                key={f.key}
                label={f.label}
                type="number"
                min="0"
                max="3650"
                value={draft[f.key] ?? (d[f.key] === null || d[f.key] === undefined ? "" : String(d[f.key]))}
                onChange={(e) => setDraft((prev) => ({ ...prev, [f.key]: e.target.value }))}
                hint="Whole days, 0–3650. Blank (after editing) means keep forever."
                disabled={!admin.allowed}
              />
            ))}
            <div>
              <Button variant="primary" loading={save.pending} disabled={!dirty || !admin.allowed} onClick={submit}>
                Save retention
              </Button>
            </div>
          </>
        )}
      </QueryBoundary>
      <PermNote gate={admin} endpoint="PUT /retention (validated server-side: non-negative, max 3650)" />
      <h3 className="ym-panel-title">Stored media</h3>
      <ReadFailure name="The asset list" path="/assets" query={assets} />
      <QueryBoundary query={assets} skeletonRows={3}>
        {(d) => {
          const items = d.items ?? [];
          const knownBytes = items.reduce((acc, a) => acc + (a.size_bytes ?? 0), 0);
          const unknownSizes = items.filter((a) => a.size_bytes === null || a.size_bytes === undefined).length;
          return (
            <>
              <Grid min={190} gap="sm">
                <StatTile label="Assets" value={items.length} source="GET /assets" />
                <StatTile
                  label="Known bytes"
                  value={knownBytes}
                  hint={unknownSizes > 0 ? `${unknownSizes} asset(s) report no size — the total is a floor, not a sum.` : undefined}
                  source="GET /assets"
                />
              </Grid>
              <DataTable
                rows={items.slice(0, 50)}
                columns={[
                  { key: "title", header: "Asset", cell: (a) => <strong>{(a.title || a.id || "").slice(0, 60)}</strong> },
                  { key: "type", header: "Type", cell: (a) => <Badge tone="neutral">{humanize(a.type)}</Badge>, hideBelow: "md" },
                  { key: "status", header: "Status", cell: (a) => <Badge tone={a.status === "READY" ? "success" : "info"}>{humanize(a.status)}</Badge> },
                  { key: "mock", header: "Real", cell: (a) => (a.is_mock ? <Badge tone="warning">MOCK</Badge> : <Badge tone="success">real</Badge>), hideBelow: "md" },
                ]}
                rowKey={(a) => a.id}
                caption="Stored media assets"
                empty="No stored media"
                emptyHint="No produced video or uploaded file is registered for this workspace."
              />
            </>
          );
        }}
      </QueryBoundary>
    </>
  );
}

/* ==========================================================================
 * Notifications inbox (own rows + mark-read writes)
 * ======================================================================= */

function InboxPanel({ query, chrome }: { query: QueryState<{ items: Notification[]; count: number; unread: number; limit: number }>; chrome: SectionChrome }) {
  const markOne = useSettingsMutation<string, unknown>(chrome,
    (id: string) => wsApi.post(`/notifications/${encodeURIComponent(id)}/read`) as Promise<unknown>,
    {
      onSuccess: () => {
        chrome.onSaveFailed(null);
        chrome.onSaved();
        query.reload();
      },
      onError: (e) => chrome.onSaveFailed(e.message),
    },
  );
  const markAll = useSettingsMutation<void, unknown>(chrome,
    () => wsApi.post("/notifications/read-all") as Promise<unknown>,
    {
      onSuccess: () => {
        chrome.onSaveFailed(null);
        chrome.onSaved();
        query.reload();
      },
      onError: (e) => chrome.onSaveFailed(e.message),
    },
  );


  const columns: Column<Notification>[] = [
    { key: "kind", header: "Kind", cell: (n) => <Badge tone="info">{humanize(n.kind)}</Badge> },
    {
      key: "state",
      header: "State",
      cell: (n) =>
        n.read ? (
          <Badge tone="neutral">read</Badge>
        ) : (
          <Badge tone="warning" dot>
            UNREAD
          </Badge>
        ),
    },
    { key: "created", header: "Received", cell: (n) => when(n.created_at) },
    { key: "readat", header: "Read at", cell: (n) => when(n.read_at), hideBelow: "md" },
    {
      key: "control",
      header: "Control",
      cell: (n) =>
        n.read ? (
          <span className="ym-muted">—</span>
        ) : (
          <Button variant="secondary" size="sm" loading={markOne.pending} onClick={() => void markOne.run(n.id)}>
            Mark read
          </Button>
        ),
    },
  ];

  return (
    <>
      <Grid min={190} gap="sm">
        <StatTile label="Unread" value={query.data?.unread} unavailable={query.data === null} tone={query.data && query.data.unread > 0 ? "warning" : "neutral"} source="GET /notifications" />
        <StatTile label="Returned" value={query.data?.count} unavailable={query.data === null} source="GET /notifications" />
        <StatTile label="Page size" value={query.data?.limit} unavailable={query.data === null} source="GET /notifications" />
      </Grid>
      <ReadFailure name="The notification list" path="/notifications" query={query} />
      {markOne.error ? <p className="ym-error">{markOne.error}</p> : null}
      {markAll.error ? <p className="ym-error">{markAll.error}</p> : null}
      <QueryBoundary query={query} skeletonRows={5}>
        {(d) => (
          <DataTable
            rows={d.items ?? []}
            columns={columns}
            rowKey={(n) => n.id}
            caption="Your notifications"
            maxHeight={420}
            empty="No notification"
            emptyHint="Nothing has been raised for you in this workspace."
          />
        )}
      </QueryBoundary>
      <div>
        <Button variant="secondary" loading={markAll.pending} disabled={(query.data?.unread ?? 0) === 0} onClick={() => void markAll.run(undefined)}>
          Mark all read
        </Button>
      </div>
      <p className="ym-hint">
        Your own rows only — the route filters on both workspace and user. The
        free-form `payload` is not rendered. There is no create route: inbox rows
        are written by ledger events.
      </p>
    </>
  );
}

/* ==========================================================================
 * Telegram
 * ======================================================================= */

function TelegramPanel({ query, chrome }: { query: QueryState<TelegramStatus>; chrome: SectionChrome }) {
  const admin = useAdminGate();
  const [code, setCode] = useState<{ code: string; expires_in_seconds: number } | null>(null);
  const [testMessage, setTestMessage] = useState("");
  const [testResult, setTestResult] = useState<string | null>(null);

  const pairing = useSettingsMutation<void, { code: string; expires_in_seconds: number }>(chrome,
    () => wsApi.post("/telegram/pairing-code") as Promise<{ code: string; expires_in_seconds: number }>,
    {
      onSuccess: (r) => {
        setCode(r);
        chrome.onSaveFailed(null);
        chrome.onSaved();
      },
      onError: (e) => chrome.onSaveFailed(e.message),
    },
  );

  const unlink = useSettingsMutation<string, unknown>(chrome,
    (id: string) => wsApi.del(`/telegram/links/${encodeURIComponent(id)}`) as Promise<unknown>,
    {
      onSuccess: () => {
        chrome.onSaveFailed(null);
        chrome.onSaved();
        query.reload();
      },
      onError: (e) => chrome.onSaveFailed(e.message),
    },
  );

  const toggle = useSettingsMutation<string, { active: boolean }>(chrome,
    (id: string) => wsApi.post(`/telegram/links/${encodeURIComponent(id)}/toggle`) as Promise<{ active: boolean }>,
    {
      onSuccess: () => {
        chrome.onSaveFailed(null);
        chrome.onSaved();
        query.reload();
      },
      onError: (e) => chrome.onSaveFailed(e.message),
    },
  );

  const test = useSettingsMutation<{ message: string }, { sent: number }>(chrome,
    (body) => wsApi.post("/telegram/test", body) as Promise<{ sent: number }>,
    {
      onSuccess: (r) => {
        setTestResult(`Delivered to ${r.sent} linked chat(s).`);
        chrome.onSaveFailed(null);
        chrome.onSaved();
      },
      onError: (e) => {
        setTestResult(`Test failed: ${e.message}`);
        chrome.onSaveFailed(e.message);
      },
    },
  );


  const columns: Column<TelegramLink>[] = [
    { key: "chat", header: "Chat", cell: (l) => <strong>{l.chat_title || l.chat_id}</strong> },
    {
      key: "active",
      header: "Active",
      cell: (l) =>
        l.active ? (
          <Badge tone="success" dot>yes</Badge>
        ) : (
          <Badge tone="neutral" dot>no</Badge>
        ),
    },
    { key: "linked", header: "Linked", cell: (l) => when(l.linked_at), hideBelow: "md" },
    {
      key: "toggle",
      header: "Enabled",
      cell: (l) => (
        <Toggle
          checked={l.active}
          disabled={!admin.allowed || toggle.pending}
          label={l.active ? "Disable" : "Enable"}
          onChange={() => void toggle.run(l.id)}
        />
      ),
    },
    {
      key: "unlink",
      header: "Control",
      cell: (l) => (
        <DestructiveButton
          confirmLabel={`Unlink "${l.chat_title || l.chat_id}". Remote control from that chat stops immediately.`}
          disabled={!admin.allowed || unlink.pending}
          onConfirm={() => void unlink.run(l.id)}
        >
          Unlink
        </DestructiveButton>
      ),
    },
  ];

  return (
    <>
      <ReadFailure name="The Telegram status" path="/telegram/status" query={query} />
      <QueryBoundary query={query} skeletonRows={3}>
        {(d) => (
          <>
            <Grid min={190} gap="sm">
              <StatTile label="Bot configured" value={d.bot_configured ? "yes" : "NO"} tone={d.bot_configured ? "success" : "warning"} hint={d.bot_configured ? `Token source: ${d.token_source || "unknown"}` : "Add telegram.bot_token under Connections & Keys."} source="GET /telegram/status" />
              <StatTile label="Linked chats" value={(d.links ?? []).filter((l) => l.active).length} source="GET /telegram/status" />
            </Grid>
            <DataTable
              rows={d.links ?? []}
              columns={columns}
              rowKey={(l) => l.id}
              caption="Linked Telegram chats"
              empty="No linked chat"
              emptyHint="Generate a pairing code below, then send it to the bot from the chat to link."
            />
          </>
        )}
      </QueryBoundary>
      <h3 className="ym-panel-title">Pair a chat</h3>
      {pairing.error ? <p className="ym-error">{pairing.error}</p> : null}
      <div>
        <Button variant="secondary" loading={pairing.pending} disabled={!admin.allowed} onClick={() => void pairing.run(undefined)}>
          Generate pairing code
        </Button>
      </div>
      {code?.code ? (
        <p role="status">
          Pairing code: <code>{code.code}</code> (expires in {code.expires_in_seconds}s — one-time use).
        </p>
      ) : null}
      <h3 className="ym-panel-title">Send a test message</h3>
      <Field label="Message" value={testMessage} onChange={(e) => setTestMessage(e.target.value)} hint="Blank sends the default test message." disabled={!admin.allowed} />
      <div>
        <Button variant="secondary" loading={test.pending} disabled={!admin.allowed} onClick={() => void test.run({ message: testMessage })}>
          Send test
        </Button>
      </div>
      {testResult ? <p className="ym-notif-detail">{testResult}</p> : null}
      {unlink.error ? <p className="ym-error">{unlink.error}</p> : null}
      {toggle.error ? <p className="ym-error">{toggle.error}</p> : null}
      <p className="ym-hint">
        The bot token itself is managed under Connections & Keys
        (`telegram.bot_token`); these controls handle pairing, links and
        delivery tests only.
      </p>
      <PermNote gate={admin} endpoint="POST /telegram/pairing-code, DELETE /telegram/links/{id}, POST /telegram/links/{id}/toggle, POST /telegram/test" />
    </>
  );
}

/* ==========================================================================
 * Connections & keys (status only + credential write/clear + live checks)
 * ======================================================================= */

function ConnectionsPanel({
  query,
  chrome,
}: {
  query: QueryState<{ items: ConnectionRow[] }>;
  chrome: SectionChrome;
}) {
  const manage = can("providers.manage");
  const blocked = blockedReason("providers.manage");
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const [llmResult, setLlmResult] = useState<string | null>(null);
  const [relayResult, setRelayResult] = useState<string | null>(null);

  const dirty = key !== "" && value !== "";
  useEffect(() => {
    chrome.onDirty("connections", dirty);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dirty]);

  const write = useSettingsMutation<{ key: string; value: string | null }, unknown>(chrome,
    (body) => wsApi.put("/connections", body) as Promise<unknown>,
    {
      onSuccess: () => {
        setKey("");
        setValue("");
        chrome.onSaveFailed(null);
        chrome.onSaved();
        query.reload();
      },
      onError: (e) => chrome.onSaveFailed(e.message),
    },
  );

  const clear = useSettingsMutation<string, unknown>(chrome,
    (k: string) => wsApi.put("/connections", { key: k, value: null }) as Promise<unknown>,
    {
      onSuccess: () => {
        chrome.onSaveFailed(null);
        chrome.onSaved();
        query.reload();
      },
      onError: (e) => chrome.onSaveFailed(e.message),
    },
  );

  const testLlm = useSettingsMutation<void, { ok: boolean; mode: string; detail: string }>(chrome,
    () => wsApi.post("/connections/test-llm") as Promise<{ ok: boolean; mode: string; detail: string }>,
    {
      onSuccess: (r) => setLlmResult(`${r.ok ? "OK" : "NOT OK"} (${r.mode}): ${r.detail}`),
      onError: (e) => setLlmResult(`Test failed: ${e.message}`),
    },
  );

  const testRelay = useSettingsMutation<void, { ok: boolean; detail: string }>(chrome,
    () => wsApi.post("/connections/test-publishing") as Promise<{ ok: boolean; detail: string }>,
    {
      onSuccess: (r) => setRelayResult(`${r.ok ? "OK" : "NOT OK"}: ${r.detail || "checked"}`),
      onError: (e) => setRelayResult(`Test failed: ${e.message}`),
    },
  );


  const columns: Column<ConnectionRow>[] = [
    { key: "label", header: "Setting", cell: (c) => <strong>{c.label}</strong> },
    { key: "key", header: "Key", cell: (c) => <span className="ym-notif-detail">{c.key}</span>, hideBelow: "md" },
    {
      key: "kind",
      header: "Kind",
      cell: (c) =>
        c.secret ? (
          <Badge tone="unknown" title="A value is stored. It is never displayed, not even masked.">
            SECRET
          </Badge>
        ) : (
          <Badge tone="neutral">setting</Badge>
        ),
    },
    {
      key: "configured",
      header: "Configured",
      cell: (c) =>
        c.configured ? (
          <Badge tone="success" dot>yes</Badge>
        ) : (
          <Badge tone="warning" dot>no</Badge>
        ),
    },
    { key: "source", header: "Source", cell: (c) => <Badge tone="neutral">{humanize(c.source || "unset")}</Badge> },
    {
      key: "clear",
      header: "Control",
      cell: (c) => (
        <DestructiveButton
          confirmLabel={`Clear "${c.key}". Anything reading it falls back to the next source or reports unconfigured.`}
          disabled={!manage || !c.configured || clear.pending}
          onConfirm={() => void clear.run(c.key)}
        >
          Clear
        </DestructiveButton>
      ),
    },
  ];

  return (
    <>
      {blocked ? <p className="ym-error" role="alert">{blocked} Credential status needs an admin role.</p> : null}
      <ReadFailure name="The connection list" path="/connections" query={query} />
      {write.error ? <p className="ym-error">{write.error}</p> : null}
      {clear.error ? <p className="ym-error">{clear.error}</p> : null}
      <QueryBoundary query={query} skeletonRows={8}>
        {(d) => (
          <DataTable
            rows={d.items ?? []}
            columns={columns}
            rowKey={(c) => c.key}
            caption="Credential configuration status"
            maxHeight={520}
            empty="No credential setting registered"
            emptyHint="The registry declares no manageable credential key."
          />
        )}
      </QueryBoundary>
      <p className="ym-hint">
        Status only. No value, no length, no digest, no masked prefix — for any
        key, secret or not. Credentials are stored encrypted server-side.
      </p>
      <h3 className="ym-panel-title">Set a credential</h3>
      <QueryBoundary query={query} skeletonRows={1}>
        {(d) => (
          <Select label="Key" value={key} onChange={(e) => setKey(e.target.value)} disabled={!manage}>
            <option value="">Pick a key</option>
            {(d.items ?? []).map((c) => (
              <option key={c.key} value={c.key}>
                {c.key}{c.configured ? " (configured)" : ""}
              </option>
            ))}
          </Select>
        )}
      </QueryBoundary>
      <Field
        label="Value"
        type="password"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        hint="Written encrypted. It is never read back — not even masked."
        disabled={!manage}
      />
      <div>
        <Button variant="primary" loading={write.pending} disabled={!manage || !dirty} onClick={() => void write.run({ key, value })}>
          Save credential
        </Button>
      </div>
      <h3 className="ym-panel-title">Live checks</h3>
      <div>
        <Button variant="secondary" loading={testLlm.pending} disabled={!manage} onClick={() => void testLlm.run(undefined)}>
          Test LLM
        </Button>{" "}
        <Button variant="secondary" loading={testRelay.pending} disabled={!manage} onClick={() => void testRelay.run(undefined)}>
          Test publishing path
        </Button>
      </div>
      {llmResult ? <p className="ym-notif-detail">LLM: {llmResult}</p> : null}
      {relayResult ? <p className="ym-notif-detail">Publishing: {relayResult}</p> : null}
      <div className="ymset-dangerzone">
        <h4 className="ymset-dangerzone-title">Danger zone</h4>
        <p className="ym-hint">
          Clearing is immediate: providers reading the key fall back or report
          unconfigured, and paid work behind it stops. Each Clear control states
          this before confirming.
        </p>
      </div>
      <p className="ymset-permnote" role="note">
        {manage
          ? "Write paths PUT /connections, POST /connections/test-llm and POST /connections/test-publishing require an admin role. The server enforces this."
          : blocked}
      </p>
    </>
  );
}

/* ==========================================================================
 * Webhooks
 * ======================================================================= */

function WebhooksPanel({ query, chrome }: { query: QueryState<WebhookList>; chrome: SectionChrome }) {
  const admin = useAdminGate();
  const [url, setUrl] = useState("");
  const [events, setEvents] = useState<string[]>([]);
  const [secret, setSecret] = useState<{ id: string; secret: string } | null>(null);

  const subscribe = useSettingsMutation<{ url: string; events: string[] }, { id: string; secret: string }>(chrome,
    (body) => wsApi.post("/webhooks", body) as Promise<{ id: string; secret: string }>,
    {
      onSuccess: (r) => {
        setSecret(r);
        setUrl("");
        setEvents([]);
        chrome.onSaveFailed(null);
        chrome.onSaved();
        query.reload();
      },
      onError: (e) => chrome.onSaveFailed(e.message),
    },
  );

  const remove = useSettingsMutation<string, unknown>(chrome,
    (id: string) => wsApi.del(`/webhooks/${encodeURIComponent(id)}`) as Promise<unknown>,
    {
      onSuccess: () => {
        chrome.onSaveFailed(null);
        chrome.onSaved();
        query.reload();
      },
      onError: (e) => chrome.onSaveFailed(e.message),
    },
  );

  const ping = useSettingsMutation<string, { enqueued: boolean }>(chrome,
    (id: string) => wsApi.post(`/webhooks/${encodeURIComponent(id)}/test`) as Promise<{ enqueued: boolean }>,
    {
      onSuccess: () => {
        chrome.onSaveFailed(null);
        chrome.onSaved();
      },
      onError: (e) => chrome.onSaveFailed(e.message),
    },
  );


  const toggleEvent = (e: string) =>
    setEvents((prev) => (prev.includes(e) ? prev.filter((x) => x !== e) : [...prev, e]));

  const columns: Column<WebhookRow>[] = [
    { key: "url", header: "Endpoint", cell: (w) => <span className="ym-notif-detail">{w.url.slice(0, 60)}</span> },
    {
      key: "events",
      header: "Events",
      cell: (w) =>
        w.events.length ? (
          <span>
            {w.events.map((e) => (
              <Badge key={e} tone="neutral">
                {e}
              </Badge>
            ))}
          </span>
        ) : (
          <span className="ym-muted">none</span>
        ),
      hideBelow: "md",
    },
    {
      key: "active",
      header: "Active",
      cell: (w) => (w.active ? <Badge tone="success" dot>yes</Badge> : <Badge tone="neutral" dot>no</Badge>),
    },
    { key: "created", header: "Created", cell: (w) => when(w.created_at), hideBelow: "md" },
    {
      key: "test",
      header: "Ping",
      cell: (w) => (
        <Button variant="secondary" size="sm" loading={ping.pending} disabled={!admin.allowed || !w.active} onClick={() => void ping.run(w.id)}>
          Send ping
        </Button>
      ),
    },
    {
      key: "remove",
      header: "Control",
      cell: (w) => (
        <DestructiveButton
          confirmLabel={`Delete the subscription to ${w.url.slice(0, 40)}. Deliveries stop immediately.`}
          disabled={!admin.allowed || remove.pending}
          onConfirm={() => void remove.run(w.id)}
        >
          Delete
        </DestructiveButton>
      ),
    },
  ];

  return (
    <>
      <ReadFailure name="The webhook list" path="/webhooks" query={query} />
      {subscribe.error ? <p className="ym-error">{subscribe.error}</p> : null}
      {remove.error ? <p className="ym-error">{remove.error}</p> : null}
      {ping.error ? <p className="ym-error">{ping.error}</p> : null}
      <QueryBoundary query={query} skeletonRows={3}>
        {(d) => (
          <>
            <DataTable
              rows={d.items ?? []}
              columns={columns}
              rowKey={(w) => w.id}
              caption="Webhook subscriptions"
              empty="No webhook subscription"
              emptyHint="Nothing is delivered out of this workspace."
            />
            <p className="ym-hint">
              Subscribable events, from the server: {(d.events ?? []).join(", ") || "none reported"}.
            </p>
            <h3 className="ym-panel-title">Subscribe a URL</h3>
            <Field label="URL" value={url} onChange={(e) => setUrl(e.target.value)} hint="Must be a valid http(s) URL; the server validates it." disabled={!admin.allowed} />
            <fieldset>
              <legend className="ym-label">Events</legend>
              {(d.events ?? []).map((e) => (
                <Toggle key={e} checked={events.includes(e)} onChange={() => toggleEvent(e)} label={e} disabled={!admin.allowed} />
              ))}
            </fieldset>
            <div>
              <Button
                variant="primary"
                loading={subscribe.pending}
                disabled={!admin.allowed || url.trim() === "" || events.length === 0}
                onClick={() => void subscribe.run({ url: url.trim(), events })}
              >
                Subscribe
              </Button>
            </div>
          </>
        )}
      </QueryBoundary>
      {secret?.secret ? (
        <Panel title="Subscription created — copy the secret now" dense>
          <p className="ym-error" role="alert">
            The signing secret is shown ONCE, at subscription time, and the list
            route never returns it. Store it now.
          </p>
          <p className="ym-notif-detail">
            <code>{secret.secret}</code>
          </p>
          <Button variant="secondary" size="sm" onClick={() => setSecret(null)}>
            I have stored it
          </Button>
        </Panel>
      ) : null}
      <div className="ymset-dangerzone">
        <h4 className="ymset-dangerzone-title">Danger zone</h4>
        <p className="ym-hint">
          Deleting a subscription stops deliveries immediately. Each Delete
          control confirms with the endpoint it will silence.
        </p>
      </div>
      <PermNote gate={admin} endpoint="POST /webhooks, DELETE /webhooks/{id}, POST /webhooks/{id}/test" />
    </>
  );
}

/* ==========================================================================
 * Knowledge sources
 * ======================================================================= */

function SourcesPanel({ query, chrome }: { query: QueryState<SourceList>; chrome: SectionChrome }) {
  const admin = useAdminGate();
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const sync = useSettingsMutation<string, { job_id: string; queued: boolean }>(chrome,
    (id: string) => wsApi.post(`/knowledge/sources/${encodeURIComponent(id)}/sync`) as Promise<{ job_id: string; queued: boolean }>,
    {
      onSuccess: (r) => {
        setBusy(null);
        setNotice(r.queued ? `Sync queued as job ${r.job_id}.` : `A sync is already running as job ${r.job_id} — joined it instead of queueing a second.`);
        chrome.onSaveFailed(null);
        chrome.onSaved();
      },
      onError: (e) => {
        setBusy(null);
        setNotice(null);
        chrome.onSaveFailed(e.message);
      },
    },
  );

  const disconnect = useSettingsMutation<string, unknown>(chrome,
    (id: string) => wsApi.post(`/knowledge/sources/${encodeURIComponent(id)}/disconnect`) as Promise<unknown>,
    {
      onSuccess: () => {
        setBusy(null);
        setNotice("Connector disconnected.");
        chrome.onSaveFailed(null);
        chrome.onSaved();
        query.reload();
      },
      onError: (e) => {
        setBusy(null);
        setNotice(null);
        chrome.onSaveFailed(e.message);
      },
    },
  );


  const columns: Column<SourceConnector>[] = [
    { key: "title", header: "Connector", cell: (c) => <strong>{c.title || humanize(c.kind)}</strong> },
    {
      key: "impl",
      header: "Built",
      cell: (c) =>
        c.implemented ? (
          <Badge tone="info">implemented</Badge>
        ) : (
          <Badge tone="danger" title="The catalogue says no adapter exists for this connector.">
            NOT IMPLEMENTED
          </Badge>
        ),
    },
    {
      key: "status",
      header: "Status",
      cell: (c) => (
        <Badge tone={c.status === "error" ? "danger" : c.status === "active" ? "success" : "neutral"} dot>
          {humanize(c.status)}
        </Badge>
      ),
    },
    {
      key: "creds",
      header: "Credentials",
      cell: (c) =>
        c.requires_credentials ? (
          c.has_credentials ? (
            <Badge tone="success">present</Badge>
          ) : (
            <Badge tone="warning">missing</Badge>
          )
        ) : (
          <span className="ym-muted">not required</span>
        ),
    },
    { key: "docs", header: "Live docs", align: "right", cell: (c) => c.doc_count, hideBelow: "md" },
    {
      key: "sync",
      header: "Sync",
      cell: (c) => (
        <Button variant="secondary" size="sm" loading={sync.pending && busy === c.id} disabled={!admin.allowed || !c.enabled} onClick={() => { setBusy(c.id); void sync.run(c.id); }}>
          Sync now
        </Button>
      ),
    },
    {
      key: "disc",
      header: "Control",
      cell: (c) => (
        <DestructiveButton
          confirmLabel={`Disconnect "${c.title || c.kind}". Trend intake from it stops; already-ingested documents stay.`}
          disabled={!admin.allowed || disconnect.pending}
          onConfirm={() => { setBusy(c.id); void disconnect.run(c.id); }}
        >
          Disconnect
        </DestructiveButton>
      ),
    },
  ];

  return (
    <>
      <ReadFailure name="The connector list" path="/knowledge/sources" query={query} />
      {sync.error ? <p className="ym-error">{sync.error}</p> : null}
      {disconnect.error ? <p className="ym-error">{disconnect.error}</p> : null}
      {notice ? <p className="ym-hint" role="status">{notice}</p> : null}
      <QueryBoundary query={query} skeletonRows={4}>
        {(d) => (
          <DataTable
            rows={d.items ?? []}
            columns={columns}
            rowKey={(c) => c.id}
            caption="Source connectors"
            empty="No connector registered"
            emptyHint="No research source is wired up, so trend intake has no external evidence."
          />
        )}
      </QueryBoundary>
      <p className="ym-hint">
        Config shown on the detail surface is ALREADY redacted server-side —
        `describe_connector` drops every key matching
        token|key|secret|password|credential and reports `has_credentials` as a
        boolean instead. Registering a NEW connector is deliberately not here:
        no endpoint lists the valid connector kinds, so a free-text kind field
        would only produce 422s.
      </p>
      <PermNote gate={admin} endpoint="POST /knowledge/sources/{id}/sync and POST /knowledge/sources/{id}/disconnect" />
    </>
  );
}

/* ==========================================================================
 * Trend sources
 * ======================================================================= */

function TrendsPanel({ query, chrome }: { query: QueryState<TrendSourceList>; chrome: SectionChrome }) {
  const admin = useAdminGate();

  const remove = useSettingsMutation<string, unknown>(chrome,
    (id: string) => wsApi.del(`/trend-sources/${encodeURIComponent(id)}`) as Promise<unknown>,
    {
      onSuccess: () => {
        chrome.onSaveFailed(null);
        chrome.onSaved();
        query.reload();
      },
      onError: (e) => chrome.onSaveFailed(e.message),
    },
  );


  const columns: Column<TrendSource>[] = [
    { key: "name", header: "Source", cell: (t) => <strong>{t.name || humanize(t.kind)}</strong> },
    { key: "kind", header: "Kind", cell: (t) => <Badge tone="neutral">{humanize(t.kind)}</Badge> },
    {
      key: "enabled",
      header: "Enabled",
      cell: (t) => (t.enabled ? <Badge tone="success" dot>yes</Badge> : <Badge tone="neutral" dot>no</Badge>),
    },
    { key: "priority", header: "Priority", align: "right", cell: (t) => t.priority, hideBelow: "md" },
    {
      key: "remove",
      header: "Control",
      cell: (t) => (
        <DestructiveButton
          confirmLabel={`Delete the trend source "${t.name || t.kind}". Its intake stops immediately.`}
          disabled={!admin.allowed || remove.pending}
          onConfirm={() => void remove.run(t.id)}
        >
          Delete
        </DestructiveButton>
      ),
    },
  ];

  return (
    <>
      <ReadFailure name="The trend source list" path="/trend-sources" query={query} />
      {remove.error ? <p className="ym-error">{remove.error}</p> : null}
      <QueryBoundary query={query} skeletonRows={3}>
        {(d) => (
          <DataTable
            rows={d.items ?? []}
            columns={columns}
            rowKey={(t) => t.id}
            caption="Trend sources"
            empty="No trend source registered"
            emptyHint="Trend intake has no wired source, so signals arrive only by manual ingest."
          />
        )}
      </QueryBoundary>
      <p className="ym-hint">
        Adding a source is deliberately not here: the valid kinds (the provider
        REGISTRY in workspaces.py) are listed by no endpoint. Deleting one is —
        above, with confirmation.
      </p>
      <PermNote gate={admin} endpoint="DELETE /trend-sources/{id}" />
    </>
  );
}

/* ==========================================================================
 * Shell: vertical nav + search + save-state + section outlet
 * ======================================================================= */

type SaveAggregate = "Saved" | "Saving" | "Unsaved changes" | "Save failed";

export default function Settings() {
  const { workspaceId, workspace } = useSession();
  const [section, setSection] = useState<SectionId>("workspace");
  const [search, setSearch] = useState("");
  const [dirtyMap, setDirtyMap] = useState<Record<string, boolean>>({});
  const [savingCount, setSavingCount] = useState(0);
  const [saveFailed, setSaveFailed] = useState<string | null>(null);
  const [lastSavedAt, setLastSavedAt] = useState<string | null>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);

  /* Queries live in the PANELS so only the active section fetches. The shell
   * keeps shared reads needed by more than one section. */
  const ws = useWsQuery<Workspace>("");
  const members = useWsQuery<{ items: Member[] }>("/members");
  const policy = useWsQuery<AutonomyPolicy>("/planner/policy");
  const agents = useWsQuery<{ items: AgentConfigRow[] }>("/agents/config");
  const calendar = useWsQuery<CalendarState>("/planner/calendar");
  const safety = useWsQuery<{ safety: Safety }>("/safety");
  const costs = useWsQuery<CostSummary>("/costs");
  const intel = useWsQuery<CostIntelligence>("/costs/intelligence");
  const accounts = useWsQuery<{ items: SocialAccount[] }>("/publishing/accounts");
  const schedule = useWsQuery<{ items: ScheduleEntry[] }>("/calendar");
  const tts = useWsQuery<TtsStatus>("/connections/tts", { enabled: can("providers.manage") });
  const images = useWsQuery<ImagesStatus>("/connections/images", { enabled: can("providers.manage") });
  const videoEngine = useWsQuery<VideoEngineStatus>("/connections/video-engine", { enabled: can("providers.manage") });
  const musicPolicy = useWsQuery<MusicPolicy>("/music/policy");
  const musicProviders = useWsQuery<{ items: { key: string; available: boolean; detail: string }[]; available: string[] }>("/music/providers");
  const retention = useWsQuery<Retention>("/retention");
  const assetList = useWsQuery<{ items: AssetItem[] }>("/assets");
  const notifications = useWsQuery<{ items: Notification[]; count: number; unread: number; limit: number }>("/notifications?limit=25");
  const telegram = useWsQuery<TelegramStatus>("/telegram/status");
  const connections = useWsQuery<{ items: ConnectionRow[] }>("/connections", { enabled: can("providers.manage") });
  const webhooks = useWsQuery<WebhookList>("/webhooks");
  const sources = useWsQuery<SourceList>("/knowledge/sources");
  const trends = useWsQuery<TrendSourceList>("/trend-sources");
  const apiKeys = useWsQuery<{ items: ApiKeyRow[] }>("/api-keys");

  const reloadAll = useCallback(() => {
    ws.reload();
    members.reload();
    policy.reload();
    agents.reload();
    calendar.reload();
    safety.reload();
    costs.reload();
    intel.reload();
    accounts.reload();
    schedule.reload();
    tts.reload();
    images.reload();
    videoEngine.reload();
    musicPolicy.reload();
    musicProviders.reload();
    retention.reload();
    assetList.reload();
    notifications.reload();
    telegram.reload();
    connections.reload();
    webhooks.reload();
    sources.reload();
    trends.reload();
    apiKeys.reload();
  }, [ws, members, policy, agents, calendar, safety, costs, intel, accounts, schedule, tts, images, videoEngine, musicPolicy, musicProviders, retention, assetList, notifications, telegram, connections, webhooks, sources, trends, apiKeys]);

  const onDirty = useCallback((id: SectionId, dirty: boolean) => {
    setDirtyMap((prev) => (prev[id] === dirty ? prev : { ...prev, [id]: dirty }));
  }, []);
  const onSaved = useCallback(() => {
    setSaveFailed(null);
    setLastSavedAt(new Date().toISOString());
  }, []);
  const onSaveFailed = useCallback((message: string | null) => {
    setSaveFailed(message);
  }, []);

  const trackSave = useCallback(async <T,>(operation: () => Promise<T>): Promise<T> => {
    setSavingCount((count) => count + 1);
    try {
      const result = await operation();
      onSaved();
      return result;
    } catch (error) {
      onSaveFailed(error instanceof Error ? error.message : String(error));
      throw error;
    } finally {
      setSavingCount((count) => count - 1);
    }
  }, [onSaved, onSaveFailed]);

  const chrome: SectionChrome = useMemo(
    () => ({ onDirty, trackSave, onSaved, onSaveFailed }),
    [onDirty, trackSave, onSaved, onSaveFailed],
  );

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return SETTING_GROUPS;
    return SETTING_GROUPS.map((g) => ({
      ...g,
      sections: g.sections.filter((s) =>
        `${s.label} ${s.blurb} ${s.keywords} ${g.label}`.toLowerCase().includes(q),
      ),
    })).filter((g) => g.sections.length > 0);
  }, [search]);

  const matchCount = useMemo(
    () => filtered.reduce((n, g) => n + g.sections.length, 0),
    [filtered],
  );

  const active = SECTION_LOOKUP[section];

  const go = useCallback((id: SectionId) => {
    if (id === section) return;
    onDirty(section, false);
    setSection(id);
  }, [section, onDirty]);

  useEffect(() => {
    headingRef.current?.focus();
  }, [section]);

  if (!workspaceId) {
    return (
      <>
        <PageHeader title="Settings" description="Workspace configuration." />
        <Panel title="No workspace selected">
          <EmptyState
            title="Nothing to configure"
            description="Every setting here belongs to a workspace. Select one to load it."
          />
        </Panel>
      </>
    );
  }

  const aggregate: SaveAggregate = saveFailed
    ? "Save failed"
    : savingCount > 0
      ? "Saving"
      : Object.values(dirtyMap).some(Boolean)
        ? "Unsaved changes"
        : "Saved";

  const aggregateTone = aggregate === "Saved" ? "success" : aggregate === "Saving" ? "info" : aggregate === "Unsaved changes" ? "warning" : "danger";

  return (
    <>
      <PageHeader
        title="Settings"
        description={
          workspace?.name
            ? `${workspace.name} — workspace configuration.`
            : "Workspace configuration."
        }
        actions={<Button onClick={reloadAll}>Refresh</Button>}
      />

      <div className="ymset-savestate" role="status" aria-live="polite" aria-label="Save state">
        <Badge tone={aggregateTone} dot={aggregate !== "Saved"}>
          {aggregate}
        </Badge>
        <span>
          {aggregate === "Saved" && lastSavedAt
            ? `All changes saved (${lastSavedAt.replace("T", " ").replace("Z", " UTC")}).`
            : aggregate === "Saved"
              ? "No unsaved changes."
              : aggregate === "Saving"
                ? "A save is in flight — it resolves or fails, never spins forever."
                : aggregate === "Unsaved changes"
                  ? "Edits exist in a section below — marked with a dot in the nav. Switching sections discards them."
                  : `The last save was refused: ${saveFailed}`}
        </span>
      </div>

      <div className="ymset-mobilenav">
        <Select
          label="Settings section"
          value={section}
          onChange={(e) => go(e.target.value as SectionId)}
        >
          {SETTING_GROUPS.map((g) => (
            <optgroup key={g.id} label={g.label}>
              {g.sections.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.label}
                </option>
              ))}
            </optgroup>
          ))}
        </Select>
      </div>

      <div className="ymset-layout">
        <nav aria-label="Settings sections" className="ymset-nav">
          <div role="search" className="ymset-search">
            <Field
              label="Search settings"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Filter sections and controls"
            />
          </div>
          {search.trim() ? (
            <p className="ymset-searchcount" role="status">
              {matchCount === 0 ? "No section matches." : `${matchCount} section${matchCount === 1 ? "" : "s"} match${matchCount === 1 ? "es" : ""}.`}
            </p>
          ) : null}
          {filtered.map((g) => (
            <div key={g.id}>
              <h2 className="ymset-group-label">{g.label}</h2>
              <ul className="ymset-navlist">
                {g.sections.map((s) => (
                  <li key={s.id}>
                    <button
                      type="button"
                      className="ymset-navitem"
                      aria-current={section === s.id ? "true" : undefined}
                      onClick={() => go(s.id)}
                      title={s.blurb}
                    >
                      <span className="ymset-navitem-label">{s.label}</span>
                      {dirtyMap[s.id] ? (
                        <span className="ymset-dirtydot" title="Unsaved changes in this section" aria-label="unsaved changes" />
                      ) : null}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ))}
          {filtered.length === 0 ? (
            <p className="ym-hint">
              Nothing matches. Search covers section names, descriptions and
              control keywords — not values.
            </p>
          ) : null}
        </nav>

        <section aria-labelledby="ymset-heading">
          <Panel
            title={`${active.group} — ${active.label}`}
            subtitle={active.blurb}
            dense
          >
            <h2 id="ymset-heading" ref={headingRef} tabIndex={-1} className="ym-sr-only">
              {active.group}: {active.label}
            </h2>
            {section === "workspace" ? <WorkspacePanel workspace={ws} chrome={chrome} onSaved={ws.reload} /> : null}
            {section === "appearance" ? <AppearancePanel workspace={ws} /> : null}
            {section === "members" ? <MembersPanel query={members} /> : null}
            {section === "api-keys" ? <ApiKeysPanel query={apiKeys} chrome={chrome} /> : null}
            {section === "policy" ? <PolicyPanel query={policy} /> : null}
            {section === "agents" ? <AgentsPanel query={agents} chrome={chrome} /> : null}
            {section === "capacity" ? <CapacityPanel query={calendar} chrome={chrome} /> : null}
            {section === "budgets" ? <BudgetsPanel safety={safety} costs={costs} chrome={chrome} onSaved={() => { safety.reload(); costs.reload(); }} /> : null}
            {section === "costs" ? <CostsPanel summary={costs} intel={intel} /> : null}
            {section === "accounts" ? <AccountsPanel query={accounts} chrome={chrome} /> : null}
            {section === "scheduling" ? <SchedulingPanel query={schedule} /> : null}
            {section === "providers" ? <ProvidersPanel tts={tts} images={images} engine={videoEngine} chrome={chrome} /> : null}
            {section === "music" ? <MusicPanel policy={musicPolicy} providers={musicProviders} chrome={chrome} /> : null}
            {section === "storage" ? <StoragePanel retention={retention} assets={assetList} chrome={chrome} /> : null}
            {section === "inbox" ? <InboxPanel query={notifications} chrome={chrome} /> : null}
            {section === "telegram" ? <TelegramPanel query={telegram} chrome={chrome} /> : null}
            {section === "connections" ? <ConnectionsPanel query={connections} chrome={chrome} /> : null}
            {section === "webhooks" ? <WebhooksPanel query={webhooks} chrome={chrome} /> : null}
            {section === "sources" ? <SourcesPanel query={sources} chrome={chrome} /> : null}
            {section === "trends" ? <TrendsPanel query={trends} chrome={chrome} /> : null}
          </Panel>
        </section>
      </div>
    </>
  );
}
