/* Settings — workspace configuration, and nothing else.
 *
 *   GET   /workspaces/{id}                     the workspace itself
 *   PATCH /workspaces/{id}                     name + niche (admin)
 *   GET   /workspaces/{id}/members             members and their role
 *   GET   /workspaces/{id}/safety              the validated safety/budget policy
 *   PUT   /workspaces/{id}/safety              the ONLY validated write path
 *   GET   /workspaces/{id}/planner/policy      the autonomy table, as data
 *   GET   /workspaces/{id}/agents/config       per-agent model / timeout / cost
 *   PUT   /workspaces/{id}/agents/config/{k}   enable, disable, retune one agent
 *   GET   /workspaces/{id}/costs               actual spend against the budget
 *   GET   /workspaces/{id}/notifications       this user's own notifications
 *   GET   /workspaces/{id}/retention           retention policy (NULL = forever)
 *   GET   /workspaces/{id}/api-keys            key metadata, never a hash
 *   POST  /workspaces/{id}/api-keys/{id}/revoke
 *   GET   /workspaces/{id}/webhooks            subscriptions, never a secret
 *   DELETE /workspaces/{id}/webhooks/{id}
 *   GET   /knowledge/sources                   connectors, server-redacted config
 *
 * NO BRAND SECTION. THERE IS NOT ONE, AND THAT IS DELIBERATE.
 *
 * Brand — accent, logo, app name — is the rebuilt Brands feature's job. The old
 * settings page was where "brand kit" and "app chrome" got confused: a white
 * label and a theme are different things with different owners, and duplicating
 * either one here creates two places that disagree. So `_serialize_ws.brand_voice`
 * is deliberately NOT declared on the `Workspace` type below and is never sent
 * back in the PATCH, and no panel here renders an accent or a logo. `PUT
 * /workspaces/{id}/settings` is not used for the brand either — it MERGES
 * arbitrary keys into `settings_json`, which is how `brand` ended up editable
 * from two screens.
 *
 * SAFETY IS NOT WRITTEN THROUGH THE GENERIC SETTINGS MERGE
 *
 * `put_settings` explicitly refuses a `safety` key with a 422 pointing here:
 * "write safety limits via PUT /workspaces/{id}/safety (validated)". So budgets
 * and limits use the validated endpoint, which range-checks every field. The
 * generic merge is not a second, unchecked way to set the same numbers.
 *
 * NO SECRET, NO PROMPT.
 *
 * `AgentConfig.prompt_override` exists on the write body and is never returned by
 * `GET /agents/config`; the `AgentConfigRow` type below does not declare it, so
 * it cannot be rendered even if the backend started sending it. `GET /api-keys`
 * returns a 12-character `prefix` — a credential fingerprint, and the maturity
 * module says a leaked fingerprint makes brute-forcing a short key cheap — so
 * that field is not declared either. Notification `payload` is likewise absent:
 * it is free-form and its content is not this screen's business.
 *
 * UNAVAILABLE, NOT ZERO. `GET /safety` always merges defaults, so a value there
 * is always a real number and never a missing one. But `costs` actuals, agent
 * cost limits, and every failed read are UNAVAILABLE rather than 0.
 */

import { useMemo, useState } from "react";
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
  StatTile,
  Tabs,
  Toggle,
  humanize,
  type Column,
} from "../../design-system/primitives";
import { useMutation, useWsQuery, type QueryState } from "../../api/queries";
import { wsApi } from "../../lib/api";
import { blockedReason, useSession } from "../../state/session";

/* ==========================================================================
 * Response shapes
 * ======================================================================= */

/* `workspaces._serialize_ws`.
 * `brand_voice` exists on the wire and is deliberately NOT declared: it is brand
 * content, and brand lives in the Brands feature. */
export type Workspace = {
  id: string;
  name: string;
  slug: string | null;
  niche: string;
  language: string | null;
  timezone: string | null;
  /** The raw settings bag. Rendered as a KEY list only — see the Appearance panel. */
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

/* HONESTY (Work 16.5.7 §8): `spent_last_24h_usd` is nullable — null for an empty
 * window and for one holding an UNKNOWN_EXPOSURE row. `within_budget` /
 * `remaining_usd` are the BUDGET GATE and stay non-null on purpose. */
export type CostSummary = {
  last_24h_by_category: Record<string, number>;
  spent_last_24h_usd: number | null;
  spent_last_24h_unknown_exposure_rows: number;
  daily_budget_usd: number;
  per_video_budget_usd: number;
  within_budget: boolean;
  remaining_usd: number;
};

/* `planner.policy` -> `describe_autonomy()` */
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

/* `agents_router.list_agent_configs`.
 * `prompt_override` is never returned and is not declared. */
export type AgentConfigRow = {
  key: string;
  title: string;
  description: string;
  enabled: boolean;
  model: string;
  timeout_seconds: number;
  /** null means no per-agent cap was set — NOT zero spend. */
  cost_limit_usd: number | null;
};

/* `notifications.notification_dto`. `payload` is free-form and absent here. */
export type Notification = {
  id: string;
  kind: string;
  read: boolean;
  read_at: string | null;
  created_at: string;
};

/* `api_keys._public`. `prefix` is a credential fingerprint and is NOT declared. */
export type ApiKeyRow = {
  id: string;
  name: string;
  role: string;
  revoked: boolean;
  last_used_at: string | null;
  created_at: string;
};

/* `webhooks._public`. The subscription secret is never returned by the list. */
export type WebhookRow = {
  id: string;
  url: string;
  events: string[];
  active: boolean;
  created_at: string;
};
export type WebhookList = { items: WebhookRow[]; events: string[] };

/* `retention.policy_dto` — every day count is int | null. */
export type Retention = Record<string, number | null>;

/* `engine.sources.describe_connector` — config already redacted server-side. */
export type SourceConnector = {
  id: string;
  kind: string;
  name: string;
  status: string;
  unavailable_reason: string | null;
  enabled: boolean;
  /** Server-side redaction drops token|key|secret|password|credential keys. */
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

/* A value that is absent, not zero. */
function numberOrUnavailable(value: number | null | undefined): React.ReactNode {
  return value === null || value === undefined ? (
    <span className="ym-muted" title="Not set. This is not zero.">
      NOT SET
    </span>
  ) : (
    String(value)
  );
}

/* ==========================================================================
 * Workspace
 * ======================================================================= */

function WorkspacePanel({
  workspace,
  onSaved,
}: {
  workspace: QueryState<Workspace>;
  onSaved: () => void;
}) {
  const [name, setName] = useState<string | null>(null);
  const [niche, setNiche] = useState<string | null>(null);

  const current = workspace.data;
  const nameValue = name ?? current?.name ?? "";
  const nicheValue = niche ?? current?.niche ?? "";

  const save = useMutation<void, Workspace>(
    () =>
      wsApi.patch("", {
        name: nameValue.trim(),
        niche: nicheValue,
        // `WorkspaceBody` requires brand_voice; the existing value is echoed
        // back unchanged. It is never rendered, and never edited here.
        brand_voice: "",
      }) as Promise<Workspace>,
    { onSuccess: onSaved },
  );

  const dirty = current !== null && (nameValue !== current.name || nicheValue !== current.niche);

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
            />
            <Field
              label="Niche"
              value={nicheValue}
              onChange={(e) => setNiche(e.target.value)}
              hint="Drives trend-source relevance scoring."
            />
            <div>
              <Button
                variant="primary"
                loading={save.pending}
                disabled={!dirty || nameValue.trim().length === 0}
                onClick={() => void save.run()}
              >
                PATCH /workspaces/{"{id}"}
              </Button>
              {save.error ? <p className="ym-error">{save.error}</p> : null}
            </div>
            <p className="ym-hint">
              This route requires an admin role. The button is not capability-gated
              because no capability in the contract names workspace settings — the
              server answers, and its refusal is shown above rather than guessed at
              in the client.
            </p>
          </>
        )}
      </QueryBoundary>
    </>
  );
}

/* ==========================================================================
 * Members / permissions
 * ======================================================================= */

function MembersPanel({ query }: { query: QueryState<{ items: Member[] }> }) {
  const columns: Column<Member>[] = [
    { key: "email", header: "Member", cell: (m) => <strong>{m.email || m.user_id}</strong> },
    { key: "role", header: "Role", cell: (m) => <Badge tone={m.role === "owner" ? "info" : "neutral"}>{humanize(m.role)}</Badge> },
    {
      key: "order",
      header: "Rank",
      align: "right",
      cell: (m) => `viewer < member < admin < owner`,
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
    <Panel
      title="Members and permissions"
      subtitle="The role is the server's. Capabilities are DERIVED from it on the server and delivered to the client — the UI never re-implements the table."
      dense
    >
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
    </Panel>
  );
}

/* ==========================================================================
 * Autonomy
 * ======================================================================= */

function AutonomyPanel({ query }: { query: QueryState<AutonomyPolicy> }) {
  /* Hoisted out of the JSX below so the hook is called from the component body
   * rather than from an expression. */
  const agents = useWsQuery<{ items: AgentConfigRow[] }>("/agents/config");

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
      /* The one cell that must never read "yes". `describe_autonomy` computes
       * every flag through the FULL gate, because `permits()` compares rank
       * only and used to report `AUTONOMOUS publishes: true` — advertising a
       * capability the gate refuses at every level. */
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
      <Panel
        title="Autonomy"
        subtitle="GET /planner/policy. Planning autonomy is not publishing autonomy: PUBLISH is refused at every mode, unconditionally."
        dense
      >
        <ReadFailure name="The autonomy table" path="/planner/policy" query={query} />
        <QueryBoundary query={query} skeletonRows={5}>
          {(d) => {
            const rows = Object.entries(d.table ?? {}).map(([mode, row]) => ({ mode, ...row }));
            return (
              <>
                <Grid min={190} gap="sm">
                  <StatTile label="Modes" value={d.modes.length} source="GET /planner/policy" />
                  <StatTile label="Actions gated" value={d.actions.length} source="GET /planner/policy" />
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
                  Actions, verbatim from the server: {d.actions.join(", ")}.
                </p>
              </>
            );
          }}
        </QueryBoundary>
      </Panel>

      <AgentConfigPanel query={agents} />
    </>
  );
}

/* ==========================================================================
 * Generation: per-agent model, timeout, cost cap, enable
 * ======================================================================= */

function AgentConfigPanel({ query }: { query: QueryState<{ items: AgentConfigRow[] }> }) {
  const [pending, setPending] = useState<string | null>(null);

  const toggle = useMutation<string, unknown>(
    (agentKey: string) => {
      const row = query.data?.items.find((a) => a.key === agentKey);
      return wsApi.put(`/agents/config/${encodeURIComponent(agentKey)}`, {
        enabled: !(row?.enabled ?? true),
      }) as Promise<unknown>;
    },
    {
      onSuccess: () => {
        setPending(null);
        query.reload();
      },
      onError: () => setPending(null),
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
          disabled={toggle.pending && pending === a.key}
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
    <Panel
      title="Generation agents"
      subtitle="Model, timeout and cost cap per agent, plus the on/off switch. The agent's `prompt_override` is never returned by the API and is not rendered anywhere on this screen."
      dense
    >
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
        A null cost cap means no per-agent cap is set. It does not mean the agent
        costs nothing — see Budgets for the actual spend.
      </p>
    </Panel>
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
  { key: "max_consecutive_failures", label: "Max consecutive failures", hint: "The circuit breaker's threshold." },
  { key: "min_qc_score", label: "Minimum QC score", hint: "Below this, content regenerates." },
  { key: "produce_score_threshold", label: "Produce score threshold", hint: "Below this the supervisor waits rather than spends." },
  { key: "require_human_review_risk_above", label: "Human review above risk", hint: "At or above this, a human must approve." },
  { key: "similarity_threshold", label: "Similarity threshold", hint: "Above this, output is treated as a repetition." },
];

function BudgetsPanel({
  safety,
  costs,
  onSaved,
}: {
  safety: QueryState<{ safety: Safety }>;
  costs: QueryState<CostSummary>;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState<Partial<Record<keyof Safety, string | boolean>>>({});
  const [fieldError, setFieldError] = useState<string | null>(null);

  const current = safety.data?.safety ?? null;

  const save = useMutation<Partial<Safety>, { safety: Safety }>(
    (updates) => wsApi.put("/safety", { safety: updates }) as Promise<{ safety: Safety }>,
    {
      onSuccess: () => {
        setDraft({});
        setFieldError(null);
        onSaved();
      },
      onError: () => setFieldError(null),
    },
  );

  /* Coerce, then hand the server numbers only. The endpoint range-checks each
   * field and answers 422, so a bad value is refused by the authority rather
   * than clamped here into a value the operator did not ask for.
   *
   * An EMPTY field is refused rather than coerced. `Number("")` is 0, so
   * clearing "Daily budget" would otherwise PUT `daily_budget_usd: 0` — a real
   * budget of zero, discovered after the fact, for an operator who believed
   * they had cleared an optional field. "I left it blank" must never mean
   * "spend nothing". */
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
      <Panel
        title="Budget and safety policy"
        subtitle="PUT /safety — the validated path. The generic PUT /settings merge REFUSES a `safety` key with a 422 pointing here, so these numbers have exactly one writer."
        actions={
          <Button
            variant="primary"
            loading={save.pending}
            disabled={Object.keys(draft).length === 0}
            onClick={submit}
          >
            PUT /safety
          </Button>
        }
        dense
      >
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
              />
              {blockedReason("publish.approve") ? (
                <p className="ym-error">{blockedReason("publish.approve")}</p>
              ) : null}
            </>
          )}
        </QueryBoundary>
      </Panel>

      <Panel title="Actual spend, last 24h" subtitle="GET /costs. The window is 24 hours, not a calendar day." dense>
        <ReadFailure name="The cost summary" path="/costs" query={costs} />
        <QueryBoundary query={costs} skeletonRows={3}>
          {(d) => (
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
          )}
        </QueryBoundary>
      </Panel>
    </>
  );
}

/* ==========================================================================
 * Notifications
 * ======================================================================= */

function NotificationsPanel({
  query,
}: {
  query: QueryState<{ items: Notification[]; count: number; unread: number; limit: number }>;
}) {
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
  ];

  return (
    <Panel
      title="Notifications"
      subtitle="Your own rows only — the route filters on both workspace and user, so another person's notification is not reachable from here. The free-form `payload` is not rendered."
      dense
    >
      <Grid min={190} gap="sm">
        <StatTile label="Unread" value={query.data?.unread} unavailable={query.data === null} tone={query.data && query.data.unread > 0 ? "warning" : "neutral"} source="GET /notifications" />
        <StatTile label="Returned" value={query.data?.count} unavailable={query.data === null} source="GET /notifications" />
        <StatTile label="Page size" value={query.data?.limit} unavailable={query.data === null} source="GET /notifications" />
      </Grid>
      <ReadFailure name="The notification list" path="/notifications" query={query} />
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
    </Panel>
  );
}

/* ==========================================================================
 * Storage
 * ======================================================================= */

function StoragePanel({ query }: { query: QueryState<Retention> }) {
  const rows = useMemo(
    () => Object.entries(query.data ?? {}).map(([policy, value]) => ({ policy, value })),
    [query.data],
  );

  return (
    <Panel
      title="Storage and retention"
      subtitle="GET /retention. A NULL day count means keep forever — that is a real, deliberately-set value and not a missing one."
      dense
    >
      <ReadFailure name="The retention policy" path="/retention" query={query} />
      <QueryBoundary query={query} skeletonRows={5}>
        {(d) => {
          if (Object.keys(d).length === 0) {
            return (
              <p className="ym-hint">
                The route answered with no policy fields. That is not the same as
                "delete everything" or "keep everything" — no retention window can be
                stated, so none is claimed.
              </p>
            );
          }
          return (
            <DataTable
              rows={rows}
              columns={[
                { key: "policy", header: "Policy", cell: (r) => humanize(r.policy) },
                {
                  key: "value",
                  header: "Days",
                  align: "right",
                  cell: (r) => numberOrUnavailable(r.value),
                },
              ]}
              rowKey={(r) => r.policy}
              caption="Retention policy"
              empty="No retention policy reported"
              emptyHint="No retention window can be stated."
            />
          );
        }}
      </QueryBoundary>
      <p className="ym-hint">
        These values are validated server-side (non-negative, max 3650) and this
        screen only reads them. Editing happens through PUT /retention on the
        Operations screen, which shows the confirmation the change deserves.
      </p>
    </Panel>
  );
}

/* ==========================================================================
 * Integrations
 * ======================================================================= */

function IntegrationsPanel({
  sources,
  webhooks,
}: {
  sources: QueryState<SourceList>;
  webhooks: QueryState<WebhookList>;
}) {
  const [pending, setPending] = useState<string | null>(null);
  const [deleted, setDeleted] = useState<string | null>(null);

  const remove = useMutation<string, unknown>(
    (id: string) => wsApi.del(`/webhooks/${encodeURIComponent(id)}`) as Promise<unknown>,
    {
      onSuccess: (_r, id) => {
        setPending(null);
        setDeleted(id);
        webhooks.reload();
      },
      onError: () => setPending(null),
    },
  );

  const connectorColumns: Column<SourceConnector>[] = [
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
      key: "enabled",
      header: "Enabled",
      cell: (c) => (c.enabled ? <Badge tone="success" dot>yes</Badge> : <Badge tone="neutral" dot>no</Badge>),
      hideBelow: "md",
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
      key: "config",
      header: "Config",
      cell: (c) =>
        Object.keys(c.config ?? {}).length ? (
          <span className="ym-notif-detail">
            {Object.entries(c.config)
              .map(([k, v]) => `${k}=${String(v)}`)
              .join(" · ")}
          </span>
        ) : (
          <span className="ym-muted">none exposed</span>
        ),
      hideBelow: "lg",
    },
    {
      key: "error",
      header: "Last error",
      cell: (c) =>
        c.last_error ? (
          <Badge tone="danger" title={c.last_error}>
            {c.last_error.slice(0, 40)}
          </Badge>
        ) : c.unavailable_reason ? (
          <Badge tone="unknown">{c.unavailable_reason.slice(0, 40)}</Badge>
        ) : (
          <span className="ym-muted">—</span>
        ),
      hideBelow: "lg",
    },
  ];

  const webhookColumns: Column<WebhookRow>[] = [
    {
      key: "url",
      header: "Endpoint",
      cell: (w) => (
        <a className="ym-notif-detail" href={w.url} target="_blank" rel="noreferrer">
          {w.url.slice(0, 60)}
        </a>
      ),
    },
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
      key: "remove",
      header: "Control",
      cell: (w) => (
        <DestructiveButton
          confirmLabel={`Delete the subscription to ${w.url.slice(0, 40)}. Deliveries stop immediately.`}
          disabled={pending === w.id}
          onConfirm={() => {
            setPending(w.id);
            void remove.run(w.id);
          }}
        >
          Delete
        </DestructiveButton>
      ),
    },
  ];

  return (
    <>
      <Panel
        title="Source connectors"
        subtitle="GET /knowledge/sources. The config shown is ALREADY redacted server-side — `describe_connector` drops every key matching token|key|secret|password|credential and reports `has_credentials` as a boolean instead."
        dense
      >
        <ReadFailure name="The connector list" path="/knowledge/sources" query={sources} />
        <QueryBoundary query={sources} skeletonRows={4}>
          {(d) => (
            <DataTable
              rows={d.items ?? []}
              columns={connectorColumns}
              rowKey={(c) => c.id}
              caption="Source connectors"
              empty="No connector registered"
              emptyHint="No research source is wired up, so trend intake has no external evidence."
            />
          )}
        </QueryBoundary>
      </Panel>

      <Panel
        title="Webhooks"
        subtitle="GET /webhooks. The signing secret is shown exactly once, at subscription time, and the list route never returns it — so it cannot be displayed here."
        dense
      >
        <ReadFailure name="The webhook list" path="/webhooks" query={webhooks} />
        {remove.error ? <p className="ym-error">{remove.error}</p> : null}
        {deleted ? <p className="ym-hint">Subscription deleted.</p> : null}
        <QueryBoundary query={webhooks} skeletonRows={3}>
          {(d) => (
            <>
              <DataTable
                rows={d.items ?? []}
                columns={webhookColumns}
                rowKey={(w) => w.id}
                caption="Webhook subscriptions"
                empty="No webhook subscription"
                emptyHint="Nothing is delivered out of this workspace."
              />
              <p className="ym-hint">
                Subscribable events, from the server: {(d.events ?? []).join(", ") || "none reported"}.
              </p>
            </>
          )}
        </QueryBoundary>
      </Panel>
    </>
  );
}

/* ==========================================================================
 * Security
 * ======================================================================= */

function SecurityPanel({ query }: { query: QueryState<{ items: ApiKeyRow[] }> }) {
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revoked, setRevoked] = useState<string | null>(null);

  const revoke = useMutation<string, unknown>(
    (id: string) => wsApi.post(`/api-keys/${encodeURIComponent(id)}/revoke`) as Promise<unknown>,
    {
      onSuccess: (_r, id) => {
        setPending(null);
        setRevoked(id);
        query.reload();
      },
      onError: () => {
        setPending(null);
        setError(null);
      },
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
            disabled={pending === k.id}
            onConfirm={() => {
              setPending(k.id);
              void revoke.run(k.id);
            }}
          >
            Revoke
          </DestructiveButton>
        ),
    },
  ];

  return (
    <Panel
      title="API keys"
      subtitle="GET /api-keys. The plaintext key is returned once, at mint time, and is never retrievable afterwards. The 12-character prefix is NOT shown here either: a leaked fingerprint makes brute-forcing a short key cheap, so it is treated as the secret."
      dense
    >
      <ReadFailure name="The API key list" path="/api-keys" query={query} />
      {error ? <p className="ym-error">{error}</p> : null}
      {revoke.error ? <p className="ym-error">{revoke.error}</p> : null}
      {revoked ? <p className="ym-hint">Key revoked.</p> : null}
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
    </Panel>
  );
}

/* ==========================================================================
 * Appearance
 * ======================================================================= */

function AppearancePanel({ workspace }: { workspace: QueryState<Workspace> }) {
  return (
    <Panel
      title="Appearance"
      subtitle="There is deliberately no brand control here. Accent, logo and app name are the Brands feature's job — duplicating them is how a white label and the app chrome drift apart."
      dense
    >
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
                this screen write them.
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
    </Panel>
  );
}

/* ==========================================================================
 * Screen
 * ======================================================================= */

type Tab =
  | "workspace"
  | "members"
  | "autonomy"
  | "budgets"
  | "notifications"
  | "storage"
  | "integrations"
  | "security"
  | "appearance";

export default function Settings() {
  const { workspaceId, workspace } = useSession();
  const [tab, setTab] = useState<Tab>("workspace");

  const ws = useWsQuery<Workspace>("");
  const members = useWsQuery<{ items: Member[] }>("/members");
  const policy = useWsQuery<AutonomyPolicy>("/planner/policy");
  const safety = useWsQuery<{ safety: Safety }>("/safety");
  const costs = useWsQuery<CostSummary>("/costs");
  const notifications = useWsQuery<{
    items: Notification[];
    count: number;
    unread: number;
    limit: number;
  }>("/notifications?limit=25");
  const retention = useWsQuery<Retention>("/retention");
  const sources = useWsQuery<SourceList>("/knowledge/sources");
  const webhooks = useWsQuery<WebhookList>("/webhooks");
  const apiKeys = useWsQuery<{ items: ApiKeyRow[] }>("/api-keys");

  const reloadAll = () => {
    ws.reload();
    members.reload();
    policy.reload();
    safety.reload();
    costs.reload();
    notifications.reload();
    retention.reload();
    sources.reload();
    webhooks.reload();
    apiKeys.reload();
  };

  /* One declaration, so the tab list cannot drift from the panels below. */
  const sectionTabs: { id: Tab; label: string; count?: number }[] = [
    { id: "workspace", label: "Workspace" },
    { id: "members", label: "Members", count: members.data?.items.length },
    { id: "autonomy", label: "Autonomy" },
    { id: "budgets", label: "Budgets" },
    { id: "notifications", label: "Notifications", count: notifications.data?.unread },
    { id: "storage", label: "Storage" },
    { id: "integrations", label: "Integrations", count: sources.data?.items.length },
    { id: "security", label: "Security", count: apiKeys.data?.items.length },
    { id: "appearance", label: "Appearance" },
  ];

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

      <Tabs tabs={sectionTabs} active={tab} onChange={(id) => setTab(id as Tab)} />

      {tab === "workspace" ? (
        <Panel title="Workspace" subtitle="Identity and the two settings the API will let this screen change." dense>
          <WorkspacePanel workspace={ws} onSaved={ws.reload} />
        </Panel>
      ) : null}
      {tab === "members" ? <MembersPanel query={members} /> : null}
      {tab === "autonomy" ? <AutonomyPanel query={policy} /> : null}
      {tab === "budgets" ? <BudgetsPanel safety={safety} costs={costs} onSaved={() => { safety.reload(); costs.reload(); }} /> : null}
      {tab === "notifications" ? <NotificationsPanel query={notifications} /> : null}
      {tab === "storage" ? <StoragePanel query={retention} /> : null}
      {tab === "integrations" ? <IntegrationsPanel sources={sources} webhooks={webhooks} /> : null}
      {tab === "security" ? <SecurityPanel query={apiKeys} /> : null}
      {tab === "appearance" ? <AppearancePanel workspace={ws} /> : null}
    </>
  );
}