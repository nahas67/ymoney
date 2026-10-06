/* Community — one desk for everything a viewer says back.
 *
 *   GET  /inbox/platforms                       api/v1/inbox.py  list_platforms
 *   GET  /inbox/autonomy                        api/v1/inbox.py  get_autonomy
 *   GET  /inbox/analytics                       api/v1/inbox.py  community_analytics
 *   GET  /inbox/conversations?…                 api/v1/inbox.py  list_conversations
 *   GET  /inbox/conversations/{id}              api/v1/inbox.py  get_conversation
 *   GET  /inbox/interactions?…                  api/v1/inbox.py  list_interactions
 *   GET  /inbox/interactions/{id}               api/v1/inbox.py  get_interaction
 *   GET  /inbox/actions?…                       api/v1/inbox.py  list_actions
 *   GET  /inbox/opportunities?…                 api/v1/inbox.py  list_opportunities
 *   GET  /inbox/insights?limit=…                api/v1/inbox.py  list_insights
 *   POST /inbox/sync                            api/v1/inbox.py  sync_inbox
 *   POST /inbox/interactions/{id}/read          api/v1/inbox.py  set_read
 *   POST /inbox/interactions/{id}/classify      api/v1/inbox.py  classify_interaction
 *   POST /inbox/interactions/{id}/draft         api/v1/inbox.py  draft_interaction
 *   POST /inbox/conversations/{id}/reply        api/v1/inbox.py  reply_conversation
 *   POST /inbox/actions/{id}/approve            api/v1/inbox.py  approve_action
 *   POST /inbox/actions/{id}/reject             api/v1/inbox.py  reject_action
 *   POST /inbox/actions/{id}/send               api/v1/inbox.py  send_action
 *   POST /inbox/bulk                            api/v1/inbox.py  bulk_action
 *   POST /inbox/opportunities/{id}/convert      api/v1/inbox.py  convert_opportunity
 *   POST /inbox/opportunities/{id}/dismiss      api/v1/inbox.py  dismiss_opportunity
 *
 * ======================= WHAT THIS SCREEN DELIBERATELY DOES NOT DO ========
 *
 * It renders ONLY attributes the API explicitly returns: `sentiment`, `intent`,
 * `is_question`, `classifications[]`, `moderation[]`, `moderation_state` and
 * `priority` are all columns or DTO fields on `models.community.SocialInteraction`.
 * There is no sentiment-about-a-person, no inferred demographic, no inferred age
 * or gender, and no scoring of the author — the backend produces no such field,
 * and a screen that inferred one would be asserting a fact about a person that
 * nobody measured. `sentiment` is therefore labelled as a property of the MESSAGE
 * (`_interaction_dto` stores it from the classifier run over `text`), and it is
 * rendered muted so it never reads as a verdict on the reader.
 *
 * Approving a reply uses `reviews.approve`, NOT `publish.approve`. The backend
 * gate on `POST /inbox/actions/{id}/approve` is `require_workspace_role("member")`
 * — the same threshold `reviews.approve` carries in
 * `services/capabilities._MINIMUM_ROLE`. There is no community-specific
 * capability in that table, and `publish.approve` requires `admin`, which would
 * hide an action a member may legitimately take. This is affordance only: the
 * route still answers 403 to anyone below `member`.
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
  Panel,
  PageHeader,
  QueryBoundary,
  Select,
  StatTile,
  StatusBadge,
  Tabs,
  Textarea,
  Toggle,
  humanize,
  type Column,
  type Tone,
} from "../../design-system/primitives";
import { useMutation, useWsQuery } from "../../api/queries";
import { wsApi } from "../../lib/api";
import { blockedReason, can, useSession } from "../../state/session";

/* ==========================================================================
 * Response shapes — every one typed from api/v1/inbox.py's DTOs
 * ======================================================================= */

/* `models.community.CLASSIFICATION_LABELS` — the backend 422s anything else. */
export const CLASSIFICATION_LABELS = [
  "QUESTION",
  "POSITIVE",
  "NEGATIVE",
  "FEEDBACK",
  "SUPPORT",
  "LEAD",
  "SPAM",
  "ABUSE",
  "COLLABORATION",
  "CONTENT_REQUEST",
  "OTHER",
] as const;

/* `models.community.INTERACTION_STATUSES` */
export const INTERACTION_STATUSES = [
  "unread",
  "read",
  "classified",
  "drafted",
  "replied",
  "escalated",
  "ignored",
  "spam",
] as const;

/* `models.community.PRIORITIES` */
export const PRIORITIES = ["low", "normal", "high", "urgent"] as const;

/* `models.community.AUTONOMY_MODES` */
export const AUTONOMY_MODES = ["DRAFT_ONLY", "LOW_RISK_AUTO", "APPROVAL_REQUIRED", "DISABLED"] as const;

/* `_interaction_dto` — `is_unread` is derived, `sentiment` is about the text. */
export type InteractionRow = {
  id: string;
  workspace_id: string;
  platform: string;
  account_id: string;
  remote_id: string;
  kind: string;
  text: string;
  author_remote_id: string;
  author_name: string;
  parent_interaction_id: string | null;
  thread_id: string;
  conversation_id: string | null;
  post_remote_id: string;
  published_post_id: string | null;
  content_item_id: string | null;
  campaign_id: string | null;
  status: string;
  unread: boolean;
  moderation_state: string;
  /** Classifier output over `text`. NOT a property of `author_name`. */
  sentiment: string;
  intent: string;
  priority: string;
  is_question: boolean;
  classifications: unknown[];
  moderation: unknown[];
  remote_created_at: string;
  created_at: string;
  updated_at: string;
};
export type InteractionList = { items: InteractionRow[]; next_cursor: string | null };

/* `_conversation_dto` — `last_interaction` is the newest message, or null. */
export type ConversationRow = {
  id: string;
  platform: string;
  account_id: string;
  thread_key: string;
  title: string;
  participant_remote_id: string;
  participant_name: string;
  published_post_id: string | null;
  last_interaction_at: string;
  unread_count: number;
  status: string;
  priority: string;
  last_interaction: {
    id: string;
    kind: string;
    platform: string;
    author_name: string;
    status: string;
    text: string;
    created_at: string;
  } | null;
  created_at: string;
  updated_at: string;
};
export type ConversationList = { items: ConversationRow[] };
export type ConversationDetail = { conversation: ConversationRow; interactions: InteractionRow[] };

/* `_action_dto` — `badge` is the backend's own provenance word. */
export type CommunityActionRow = {
  id: string;
  interaction_id: string;
  conversation_id: string | null;
  account_id: string;
  platform: string;
  action_type: string;
  mode: string;
  state: string;
  origin: string;
  /** AI DRAFT | HUMAN EDITED | AUTO SENT | SENT */
  badge: string;
  draft_text: string;
  final_text: string;
  brand_check: Record<string, unknown>;
  approval_user_id: string | null;
  rejected_user_id: string | null;
  is_mock: boolean;
  remote_reply_id: string;
  error: string;
  sent_at: string;
  created_at: string;
  updated_at: string;
};
export type ActionList = { items: CommunityActionRow[] };

/* `GET /inbox/interactions/{id}` */
export type InteractionDetail = {
  interaction: InteractionRow;
  linked_publication: {
    id: string;
    title: string;
    remote_url: string;
    campaign_id: string | null;
    platform: string;
    remote_post_id: string;
  } | null;
  thread: InteractionRow[];
  actions: CommunityActionRow[];
};

/* `_opportunity_dto` */
export type OpportunityRow = {
  id: string;
  opportunity_type: string;
  title: string;
  detail: string;
  evidence_count: number;
  source_interaction_ids: string[];
  confidence: string;
  state: string;
  converted_idea_json: Record<string, unknown>;
  converted_opportunity_id: string | null;
  created_at: string;
  updated_at: string;
};
export type OpportunityList = { items: OpportunityRow[] };

/* `_insight_dto` + the endpoint's own `suggestions_available` flag. */
export type InsightList = {
  items: {
    id: string;
    topic_key: string;
    topic: string;
    representative_text: string;
    platforms: string[];
    evidence_count: number;
    confidence: string;
    state: string;
    created_at: string;
    updated_at: string;
  }[];
  suggestions: unknown[];
  suggestions_available: boolean;
};

/* `GET /inbox/platforms` — `available: false` is the registry being absent. */
export type InboxPlatforms = {
  available: boolean;
  platforms: { platform?: string; key?: string; name?: string; capabilities?: unknown }[];
  capabilities: Record<string, unknown>;
  observed: string[];
  error?: string;
  detail?: string;
};

/* `GET /inbox/autonomy` */
export type AutonomyPayload = {
  autonomy: { mode: string; classes: string[]; caps: Record<string, number> };
  modes: string[];
  classes: string[];
  defaults: { mode?: string; classes?: string[]; caps?: Record<string, number> };
};

/* `GET /inbox/analytics` — `available: false` means the engine is absent, and
   `metrics` is then EMPTY rather than zero. */
export type CommunityAnalytics = {
  available: boolean;
  metrics: Record<string, number | string | boolean>;
  kpis: unknown[];
  totals?: Record<string, number>;
  error?: string;
};

/* ==========================================================================
 * Helpers
 * ======================================================================= */

function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.replace("Z", " UTC").replace("T", " ");
}

function qs(params: Record<string, string | boolean | undefined>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === "" || v === false) continue;
    p.set(k, String(v));
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}

/** A classification is either a bare label or `{label, confidence, …}`. */
export function labelOf(entry: unknown): string {
  if (typeof entry === "string") return entry;
  if (entry && typeof entry === "object") {
    const rec = entry as Record<string, unknown>;
    const label = rec.label ?? rec.name;
    if (typeof label === "string" && label) return label;
  }
  return "";
}

export function confidenceOf(entry: unknown): number | null {
  if (!entry || typeof entry !== "object") return null;
  const v = (entry as Record<string, unknown>).confidence;
  return typeof v === "number" ? v : null;
}

const PRIORITY_TONE: Record<string, Tone> = {
  urgent: "danger",
  high: "warning",
  normal: "info",
  low: "neutral",
};

function priorityTone(priority: string): Tone {
  return PRIORITY_TONE[(priority ?? "").toLowerCase()] ?? "neutral";
}

/* ==========================================================================
 * Filters + list
 * ======================================================================= */

type Filters = {
  platform: string;
  status: string;
  classification: string;
  priority: string;
  unread: boolean;
  search: string;
  questionsOnly: boolean;
};

const NO_FILTERS: Filters = {
  platform: "",
  status: "",
  classification: "",
  priority: "",
  unread: false,
  search: "",
  questionsOnly: false,
};

function FilterPanel({
  filters,
  setFilters,
  platformOptions,
  classOptions,
  platformsError,
  registryUnavailable,
}: {
  filters: Filters;
  setFilters: (f: Filters) => void;
  platformOptions: string[];
  classOptions: string[];
  platformsError: string | null;
  registryUnavailable: boolean;
}) {
  const set = (patch: Partial<Filters>) => setFilters({ ...filters, ...patch });

  return (
    <Panel title="Filters" dense>
      <Grid min={150} gap="sm">
        <Select
          label="Platform"
          value={filters.platform}
          onChange={(e) => set({ platform: e.target.value })}
        >
          <option value="">All platforms</option>
          {platformOptions.map((p) => (
            <option key={p} value={p}>
              {humanize(p)}
            </option>
          ))}
        </Select>
        <Select label="Status" value={filters.status} onChange={(e) => set({ status: e.target.value })}>
          <option value="">Any status</option>
          {INTERACTION_STATUSES.map((s) => (
            <option key={s} value={s}>
              {humanize(s)}
            </option>
          ))}
        </Select>
        <Select
          label="Classification"
          value={filters.classification}
          onChange={(e) => set({ classification: e.target.value })}
        >
          <option value="">Any classification</option>
          {classOptions.map((c) => (
            <option key={c} value={c}>
              {humanize(c)}
            </option>
          ))}
        </Select>
        <Select label="Priority" value={filters.priority} onChange={(e) => set({ priority: e.target.value })}>
          <option value="">Any priority</option>
          {PRIORITIES.map((p) => (
            <option key={p} value={p}>
              {humanize(p)}
            </option>
          ))}
        </Select>
      </Grid>
      <Field
        label="Search text or author"
        value={filters.search}
        placeholder="Server-side ilike on text and author name"
        onChange={(e) => set({ search: e.target.value })}
      />
      <Grid min={150} gap="sm">
        <Toggle
          checked={filters.unread}
          onChange={(v) => set({ unread: v })}
          label="Unread only"
        />
        <Toggle
          checked={filters.questionsOnly}
          onChange={(v) => set({ questionsOnly: v, classification: v ? "QUESTION" : "" })}
          label="Questions only"
        />
      </Grid>
      <Button onClick={() => setFilters(NO_FILTERS)}>Clear filters</Button>
      {platformsError ? (
        <p className="ym-error">
          The platform registry could not be read ({platformsError}). The platform
          filter offers only platforms this workspace has actually received rows
          from.
        </p>
      ) : null}
      {registryUnavailable ? (
        <p className="ym-hint">
          The platform registry reported <code>available: false</code>. Platform
          capabilities are unknown here; nothing is inferred.
        </p>
      ) : null}
    </Panel>
  );
}

/* ==========================================================================
 * Response editor — suggested reply, approval, history
 * ======================================================================= */

function ActionBadge({ action }: { action: CommunityActionRow }) {
  const tone: Tone =
    action.badge === "HUMAN EDITED"
      ? "warning"
      : action.badge === "AUTO SENT"
        ? "success"
        : action.state === "sent"
          ? "success"
          : "info";
  return <Badge tone={tone}>{action.badge}</Badge>;
}

function brandCheckSummary(check: Record<string, unknown>): string[] {
  const out: string[] = [];
  const forbidden = check.forbidden_hits;
  if (Array.isArray(forbidden) && forbidden.length) {
    out.push(`forbidden: ${forbidden.map((f) => String(f)).join(", ")}`);
  }
  const disclaimers = check.required_disclaimers;
  if (Array.isArray(disclaimers) && disclaimers.length) {
    out.push(`required disclaimer: ${disclaimers.map((d) => String(d)).join(", ")}`);
  }
  if (typeof check.tone === "string" && check.tone) out.push(`tone: ${check.tone}`);
  return out;
}

function ResponsePanel({
  interaction,
  actions,
  onChanged,
}: {
  interaction: InteractionRow;
  actions: CommunityActionRow[];
  onChanged: () => void;
}) {
  const [reply, setReply] = useState("");
  const [edited, setEdited] = useState(false);
  const latest = actions[0] ?? null;
  const decidable = latest !== null && (latest.state === "draft" || latest.state === "pending_approval");

  /* Approving a reply is `reviews.approve`-adjacent: the route gate on
     `POST /inbox/actions/{id}/approve` is `require_workspace_role("member")`,
     which is exactly the threshold `reviews.approve` carries in
     `services/capabilities._MINIMUM_ROLE`. There is no community capability in
     that table, and `publish.approve` requires `admin`, which would hide an
     action a member may legitimately take. Affordance only — the route still
     answers 403 to anyone below `member`. */
  const mayDecide = can("reviews.approve");
  const decideBlock = blockedReason("reviews.approve");

  const id = interaction.id;
  const conversationId = interaction.conversation_id;
  const latestId = latest?.id ?? "";

  const markRead = useMutation<boolean, void>(
    (read) => wsApi.post(`/inbox/interactions/${id}/read`, { read }) as Promise<void>,
    { onSuccess: onChanged },
  );
  const classify = useMutation<void, void>(
    () => wsApi.post(`/inbox/interactions/${id}/classify`, {}) as Promise<void>,
    { onSuccess: onChanged },
  );
  const regenerate = useMutation<void, void>(
    () => wsApi.post(`/inbox/interactions/${id}/draft`, { regenerate: true }) as Promise<void>,
    { onSuccess: onChanged },
  );
  const approve = useMutation<void, void>(
    () => wsApi.post(`/inbox/actions/${latestId}/approve`, {}) as Promise<void>,
    { onSuccess: onChanged },
  );
  const reject = useMutation<void, void>(
    () => wsApi.post(`/inbox/actions/${latestId}/reject`, {}) as Promise<void>,
    { onSuccess: onChanged },
  );
  const queue = useMutation<{ text: string; interaction_id: string }, void>(
    (body) =>
      wsApi.post(`/inbox/conversations/${conversationId}/reply`, {
        text: body.text,
        interaction_id: body.interaction_id,
        send: false,
      }) as Promise<void>,
    { onSuccess: onChanged },
  );
  const sendAction = useMutation<{ text?: string }, void>(
    (body) => wsApi.post(`/inbox/actions/${latestId}/send`, body ?? {}) as Promise<void>,
    { onSuccess: onChanged },
  );

  const brandLines = latest ? brandCheckSummary(latest.brand_check ?? {}) : [];
  const refusal = markRead.error ?? classify.error ?? regenerate.error ?? queue.error ?? sendAction.error;

  return (
    <Panel
      title="Suggested reply"
      subtitle="A manual draft never auto-sends: POST /inbox/interactions/{id}/draft passes auto=false."
      dense
      actions={
        <>
          <Button
            size="sm"
            loading={markRead.pending}
            onClick={() => void markRead.run(interaction.status === "unread")}
          >
            {interaction.status === "unread" ? "Mark read" : "Mark unread"}
          </Button>
          <Button size="sm" loading={classify.pending} onClick={() => void classify.run(undefined)}>
            Classify
          </Button>
          <Button size="sm" loading={regenerate.pending} onClick={() => void regenerate.run(undefined)}>
            Regenerate draft
          </Button>
        </>
      }
    >
      {latest ? (
        <p className="ym-hint">
          <ActionBadge action={latest} /> <StatusBadge status={latest.state} />{" "}
          <Badge tone="neutral">{humanize(latest.action_type)}</Badge>
          {latest.is_mock ? (
            <Badge tone="mock" title="A local stand-in recorded the send; nothing reached the platform.">
              MOCK RECEIPT
            </Badge>
          ) : null}
          {latest.remote_reply_id ? (
            <span className="ym-notif-detail">remote reply {latest.remote_reply_id}</span>
          ) : null}
        </p>
      ) : (
        <p className="ym-muted">
          No action yet. Regenerate a draft to produce one — the community engine
          must be available or the backend answers 503.
        </p>
      )}

      <Textarea
        label="Reply text"
        value={reply}
        rows={4}
        placeholder="AI draft or your own words. Editing it marks the send as HUMAN EDITED server-side."
        onChange={(e) => {
          setReply(e.target.value);
          setEdited(true);
        }}
      />
      {mayDecide ? (
        <Grid min={170} gap="sm">
          {conversationId ? (
            <Button
              loading={queue.pending}
              disabled={reply.trim().length === 0}
              onClick={() => void queue.run({ text: reply.trim(), interaction_id: id })}
            >
              Queue draft
            </Button>
          ) : (
            <p className="ym-hint">
              Not linked to a conversation, so no reply can be queued against it.
            </p>
          )}
          {latest && reply.trim().length > 0 ? (
            <Button
              variant="primary"
              loading={sendAction.pending}
              onClick={() => void sendAction.run({ text: reply.trim() })}
            >
              Send this draft
            </Button>
          ) : null}
        </Grid>
      ) : (
        <p className="ym-hint">{decideBlock}</p>
      )}
      {edited ? (
        <p className="ym-hint">
          Edited by a human. The backend records origin=human_edited on the send.
        </p>
      ) : null}

      {decidable ? (
        <Grid min={170} gap="sm">
          {mayDecide ? (
            <>
              <Button variant="primary" loading={approve.pending} onClick={() => void approve.run(undefined)}>
                Approve reply
              </Button>
              <DestructiveButton
                confirmLabel="Reject this draft? It will never be sent."
                onConfirm={() => void reject.run(undefined)}
                loading={reject.pending}
              >
                Reject
              </DestructiveButton>
            </>
          ) : (
            <p className="ym-hint">{decideBlock}</p>
          )}
        </Grid>
      ) : null}

      <p className="ym-label">Brand check</p>
      {brandLines.length ? (
        brandLines.map((line) => (
          <p key={line} className="ym-notif-detail">
            {line}
          </p>
        ))
      ) : (
        <p className="ym-muted">
          {latest ? "No violation recorded in this draft." : "Draft a reply to run the brand policy check."}
        </p>
      )}
      {latest?.error ? <p className="ym-error">Last send error: {latest.error}</p> : null}
      {refusal ? <p className="ym-error">{refusal}</p> : null}
      {approve.error || reject.error ? <p className="ym-error">{approve.error ?? reject.error}</p> : null}
    </Panel>
  );
}

/* ==========================================================================
 * Interaction detail
 * ======================================================================= */

function InteractionDetailPanel({
  interactionId,
  onChanged,
}: {
  interactionId: string | null;
  onChanged: () => void;
}) {
  const detail = useWsQuery<InteractionDetail>(
    `/inbox/interactions/${interactionId ?? "none"}`,
    { enabled: interactionId !== null },
  );

  return (
    <QueryBoundary query={detail} skeletonRows={4}>
      {(d) => (
        <>
          <Panel
            title={d.interaction.author_name || "Unknown author"}
            subtitle={d.interaction.remote_id ? `remote ${d.interaction.remote_id}` : undefined}
            dense
          >
            <Grid min={140} gap="sm">
              <Badge tone="info">{humanize(d.interaction.platform)}</Badge>
              <Badge tone="neutral">{humanize(d.interaction.kind)}</Badge>
              <StatusBadge status={d.interaction.status} />
              <Badge tone={priorityTone(d.interaction.priority)}>{humanize(d.interaction.priority)}</Badge>
              <StatusBadge status={d.interaction.moderation_state} />
              {d.interaction.is_question ? <Badge tone="info">question</Badge> : null}
            </Grid>
            <p>{d.interaction.text || <span className="ym-muted">—</span>}</p>
            <p className="ym-hint">{when(d.interaction.remote_created_at || d.interaction.created_at)}</p>
            {d.linked_publication ? (
              <p className="ym-notif-detail">
                On post: <strong>{d.linked_publication.title || d.linked_publication.remote_post_id}</strong>
                {d.linked_publication.platform ? ` · ${humanize(d.linked_publication.platform)}` : ""}
                {d.linked_publication.remote_url ? (
                  <>
                    {" · "}
                    <a href={d.linked_publication.remote_url} target="_blank" rel="noreferrer">
                      open
                    </a>
                  </>
                ) : null}
              </p>
            ) : null}
          </Panel>

          <Panel
            title="Classification"
            subtitle="Exactly what the backend stored on the row. Nothing is derived here."
            dense
          >
            {d.interaction.classifications.length ? (
              <Grid min={120} gap="sm">
                {d.interaction.classifications.map((c, i) => {
                  const conf = confidenceOf(c);
                  return (
                    <Badge key={`${labelOf(c)}-${i}`} tone="info" title="From SocialInteraction.classifications_json">
                      {labelOf(c)}
                      {conf !== null ? ` ${Math.round(conf * 100)}%` : ""}
                    </Badge>
                  );
                })}
              </Grid>
            ) : (
              <p className="ym-muted">
                No labels stored. The classifier has not run on this interaction.
              </p>
            )}
            <Grid min={160} gap="sm">
              <StatTile
                label="Message sentiment"
                value={d.interaction.sentiment ? humanize(d.interaction.sentiment) : "UNAVAILABLE"}
                unavailable={!d.interaction.sentiment}
                tone="neutral"
                hint="The classifier's reading of the message text — not a trait of the author."
                source="SocialInteraction.sentiment"
              />
              <StatTile
                label="Intent"
                value={d.interaction.intent ? humanize(d.interaction.intent) : "UNAVAILABLE"}
                unavailable={!d.interaction.intent}
                hint="A label about what the message asks for. Nothing about who wrote it."
                source="SocialInteraction.intent"
              />
              <StatTile
                label="Asked a question"
                value={d.interaction.is_question ? "yes" : "no"}
                hint="A boolean the backend stores on the row."
                source="SocialInteraction.is_question"
              />
            </Grid>
            {d.interaction.moderation.length ? (
              <>
                <p className="ym-label">Moderation record</p>
                {d.interaction.moderation.map((m, i) => {
                  const rec = (m ?? {}) as Record<string, unknown>;
                  const verdict = typeof rec.verdict === "string" ? rec.verdict : "UNRECORDED";
                  const reason = typeof rec.reason === "string" ? rec.reason : typeof rec.rule === "string" ? rec.rule : "";
                  return (
                    <p key={i} className="ym-notif-detail">
                      <Badge tone={verdict === "BLOCK_ACTION" ? "danger" : verdict === "ALLOW" ? "success" : "warning"}>
                        {humanize(verdict)}
                      </Badge>{" "}
                      {reason}
                    </p>
                  );
                })}
              </>
            ) : (
              <p className="ym-muted">No moderation evidence stored for this interaction.</p>
            )}
          </Panel>

          {d.thread.length ? (
            <Panel title={`Thread (${d.thread.length} earlier)`} dense>
              {d.thread.map((t) => (
                <div key={t.id} className="ym-notif-item">
                  <div className="ym-notif-title">{t.author_name || "Unknown author"}</div>
                  <div className="ym-notif-detail">{t.text}</div>
                  <div className="ym-hint">{when(t.created_at)}</div>
                </div>
              ))}
            </Panel>
          ) : null}

          <ResponsePanel
            interaction={d.interaction}
            actions={d.actions}
            onChanged={onChanged}
          />

          <Panel title={`Response history (${d.actions.length})`} dense>
            {d.actions.length ? (
              d.actions.map((a) => (
                <div key={a.id} className="ym-notif-item">
                  <div className="ym-notif-title">
                    <ActionBadge action={a} /> <StatusBadge status={a.state} />{" "}
                    <Badge tone="neutral">{humanize(a.action_type)}</Badge>
                    <span className="ym-hint">{when(a.created_at)}</span>
                  </div>
                  <div className="ym-notif-detail">{a.final_text || a.draft_text || "—"}</div>
                  {a.sent_at ? <div className="ym-hint">sent {when(a.sent_at)}</div> : null}
                  {a.error ? <div className="ym-error">{a.error}</div> : null}
                </div>
              ))
            ) : (
              <EmptyState
                title="No response history"
                description="Drafts, approvals and sends are recorded per interaction."
              />
            )}
          </Panel>
        </>
      )}
    </QueryBoundary>
  );
}

/* ==========================================================================
 * Conversations tab
 * ======================================================================= */

function ConversationPanel({ conversationId }: { conversationId: string | null }) {
  const conv = useWsQuery<ConversationDetail>(
    `/inbox/conversations/${conversationId ?? "none"}`,
    { enabled: conversationId !== null },
  );
  return (
    <QueryBoundary query={conv} skeletonRows={3}>
      {(d) => (
        <Panel
          title={d.conversation.participant_name || d.conversation.title || "Conversation"}
          dense
        >
          <Grid min={140} gap="sm">
            <Badge tone="info">{humanize(d.conversation.platform)}</Badge>
            <StatusBadge status={d.conversation.status} />
            <Badge tone={priorityTone(d.conversation.priority)}>{humanize(d.conversation.priority)}</Badge>
            <Badge tone="neutral">{d.conversation.unread_count} unread</Badge>
          </Grid>
          {d.interactions.length ? (
            d.interactions.map((m) => (
              <div key={m.id} className="ym-notif-item">
                <div className="ym-notif-title">
                  {m.author_name || "Unknown author"}{" "}
                  <StatusBadge status={m.status} />
                  {m.is_question ? <Badge tone="info">question</Badge> : null}
                </div>
                <div className="ym-notif-detail">{m.text || "—"}</div>
                <div className="ym-hint">{when(m.created_at)}</div>
              </div>
            ))
          ) : (
            <EmptyState title="No messages in this thread" description="The conversation exists but nothing has been ingested into it." />
          )}
        </Panel>
      )}
    </QueryBoundary>
  );
}

/* ==========================================================================
 * Screen
 * ======================================================================= */

export function Community() {
  const { workspaceId, workspace } = useSession();
  const [tab, setTab] = useState("inbox");
  const [listMode, setListMode] = useState<"conversations" | "interactions">("conversations");
  const [filters, setFilters] = useState<Filters>(NO_FILTERS);
  const [selectedConversation, setSelectedConversation] = useState<string | null>(null);
  const [selectedInteraction, setSelectedInteraction] = useState<string | null>(null);

  const platforms = useWsQuery<InboxPlatforms>("/inbox/platforms");
  const autonomy = useWsQuery<AutonomyPayload>("/inbox/autonomy");
  const analytics = useWsQuery<CommunityAnalytics>("/inbox/analytics");

  /* The path must be the FIRST argument and must begin with a literal so the
     contract scanner can resolve it. The query suffix is CONCATENATED rather
     than interpolated: an interpolation that does not itself carry a `?` is
     read as another path SEGMENT, which invents `/inbox/conversations{param}`. */
  const conversations = useWsQuery<ConversationList>(
    "/inbox/conversations" +
      qs({ platform: filters.platform || undefined, unread: filters.unread || undefined, limit: "50" }),
  );
  const interactions = useWsQuery<InteractionList>(
    "/inbox/interactions" +
      qs({
        platform: filters.platform || undefined,
        status: filters.status || undefined,
        classification: filters.classification || undefined,
        priority: filters.priority || undefined,
        unread: filters.unread || undefined,
        search: filters.search || undefined,
        limit: "50",
      }),
  );
  const actions = useWsQuery<ActionList>("/inbox/actions?limit=50");
  const opportunities = useWsQuery<OpportunityList>("/inbox/opportunities?limit=50");
  const insights = useWsQuery<InsightList>("/inbox/insights?limit=50");

  const mayTriage = can("reviews.approve");
  const triageBlock = blockedReason("reviews.approve");

  const sync = useMutation<void, void>(
    () => wsApi.post("/inbox/sync", filters.platform ? { platform: filters.platform } : {}) as Promise<void>,
    {
      onSuccess: () => {
        conversations.reload();
        interactions.reload();
        analytics.reload();
      },
    },
  );

  const platformOptions = useMemo(() => {
    const keys = new Set<string>(platforms.data?.observed ?? []);
    for (const p of platforms.data?.platforms ?? []) {
      const k = p.platform ?? p.key ?? p.name;
      if (typeof k === "string" && k) keys.add(k);
    }
    return [...keys].sort();
  }, [platforms.data]);

  const classOptions = autonomy.data?.classes?.length
    ? autonomy.data.classes
    : [...CLASSIFICATION_LABELS];

  const reloadLists = () => {
    conversations.reload();
    interactions.reload();
    actions.reload();
    analytics.reload();
  };

  if (!workspaceId) {
    return (
      <>
        <PageHeader title="Community" description="One inbox for comments, mentions, questions and opportunities." />
        <Panel title="No workspace selected">
          <EmptyState
            title="Nothing to read"
            description="Interactions, conversations and opportunities are workspace-scoped. Select a workspace to load them."
          />
        </Panel>
      </>
    );
  }

  const conversationColumns: Column<ConversationRow>[] = [
    {
      key: "who",
      header: "Participant",
      cell: (c) => <strong>{c.participant_name || c.title || "Conversation"}</strong>,
    },
    { key: "platform", header: "Platform", cell: (c) => humanize(c.platform) },
    { key: "status", header: "Status", cell: (c) => <StatusBadge status={c.status} /> },
    {
      key: "unread",
      header: "Unread",
      align: "right",
      cell: (c) => (c.unread_count > 0 ? <Badge tone="info">{c.unread_count}</Badge> : <span className="ym-muted">0</span>),
    },
    {
      key: "preview",
      header: "Latest",
      cell: (c) => (c.last_interaction ? c.last_interaction.text || "—" : <span className="ym-muted">no message</span>),
      hideBelow: "md",
    },
    { key: "when", header: "Last activity", cell: (c) => when(c.last_interaction_at), hideBelow: "lg" },
  ];

  const interactionColumns: Column<InteractionRow>[] = [
    {
      key: "who",
      header: "Author",
      cell: (r) => (
        <span>
          {r.unread ? <Badge tone="info" title="status is unread">new</Badge> : null}{" "}
          <strong>{r.author_name || "Unknown author"}</strong>
        </span>
      ),
    },
    { key: "platform", header: "Platform", cell: (r) => humanize(r.platform) },
    { key: "status", header: "Status", cell: (r) => <StatusBadge status={r.status} /> },
    { key: "priority", header: "Priority", cell: (r) => <Badge tone={priorityTone(r.priority)}>{humanize(r.priority)}</Badge>, hideBelow: "md" },
    { key: "question", header: "Question", cell: (r) => (r.is_question ? <Badge tone="info">question</Badge> : <span className="ym-muted">no</span>) },
    {
      key: "labels",
      header: "Classified as",
      cell: (r) =>
        r.classifications.length ? (
          r.classifications.map((c, i) => (
            <Badge key={`${labelOf(c)}-${i}`} tone="info">
              {labelOf(c)}
            </Badge>
          ))
        ) : (
          <span className="ym-muted" title="The classifier has not run on this row">
            not classified
          </span>
        ),
      hideBelow: "lg",
    },
    { key: "text", header: "Message", cell: (r) => r.text || <span className="ym-muted">—</span>, hideBelow: "md" },
  ];

  return (
    <>
      <PageHeader
        title="Community"
        description={
          workspace?.name ? `${workspace.name} — comments, questions and opportunities in one desk.` : "Comments, questions and opportunities in one desk."
        }
        actions={
          <>
            <Button onClick={reloadLists}>Refresh</Button>
            {mayTriage ? (
              <Button variant="primary" loading={sync.pending} onClick={() => void sync.run(undefined)}>
                Sync now
              </Button>
            ) : (
              <span className="ym-hint">{triageBlock}</span>
            )}
          </>
        }
      />

      <Panel
        title="Community pulse"
        subtitle="Metrics come from the community engine. An absent engine is UNAVAILABLE, never zero."
        dense
      >
        <QueryBoundary query={analytics} skeletonRows={2}>
          {(d) =>
            d.available === false ? (
              <p className="ym-hint">
                The community engine is not available{d.error ? ` (${d.error})` : ""}.
                Every figure below is reported as UNAVAILABLE rather than as zero.
              </p>
            ) : (
              <Grid min={170} gap="sm">
                {Object.entries(d.metrics)
                  .filter(([k, v]) => typeof v === "number")
                  .slice(0, 8)
                  .map(([k, v]) => (
                    <StatTile
                      key={k}
                      label={humanize(k)}
                      value={typeof v === "number" ? v : "UNAVAILABLE"}
                      source="GET /inbox/analytics"
                    />
                  ))}
              </Grid>
            )
          }
        </QueryBoundary>
        <Grid min={170} gap="sm">
          <StatTile
            label="Open opportunities"
            value={opportunities.data === null ? undefined : opportunities.data.items.filter((o) => o.state === "open").length}
            unavailable={opportunities.data === null}
            source="GET /inbox/opportunities"
          />
          <StatTile
            label="Awaiting approval"
            value={
              actions.data === null
                ? undefined
                : actions.data.items.filter((a) => a.state === "pending_approval").length
            }
            unavailable={actions.data === null}
            tone="warning"
            source="GET /inbox/actions"
          />
          <StatTile
            label="Autonomy mode"
            value={autonomy.data ? autonomy.data.autonomy.mode : "UNAVAILABLE"}
            unavailable={autonomy.data === null}
            tone={autonomy.data?.autonomy.mode === "DISABLED" ? "neutral" : "info"}
            hint="Replies stay drafts unless the mode and the policy gate allow a send."
            source="GET /inbox/autonomy"
          />
          <StatTile
            label="Platforms observed"
            value={platforms.data === null ? undefined : (platforms.data.observed?.length ?? 0)}
            unavailable={platforms.data === null}
            source="GET /inbox/platforms"
          />
        </Grid>
        {autonomy.error ? (
          <p className="ym-error">
            The autonomy configuration could not be read ({autonomy.error}), so
            what a reply is allowed to do without a human is unknown here.
          </p>
        ) : null}
      </Panel>

      <Tabs
        tabs={[
          { id: "inbox", label: "Inbox", count: conversations.data?.items.length },
          { id: "opportunities", label: "Opportunities", count: opportunities.data?.items.length },
          { id: "insights", label: "Insights", count: insights.data?.items.length },
          { id: "history", label: "Response history", count: actions.data?.items.length },
        ]}
        active={tab}
        onChange={setTab}
      />

      {tab === "inbox" ? (
        <>
          <FilterPanel
            filters={filters}
            setFilters={setFilters}
            platformOptions={platformOptions}
            classOptions={classOptions}
            platformsError={platforms.error}
            registryUnavailable={platforms.data?.available === false}
          />

          <Panel
            title="Threads"
            dense
            actions={
              <Select
                label="List"
                value={listMode}
                onChange={(e) => setListMode(e.target.value === "interactions" ? "interactions" : "conversations")}
              >
                <option value="conversations">Conversations</option>
                <option value="interactions">Interactions</option>
              </Select>
            }
          >
            {listMode === "conversations" ? (
              <QueryBoundary query={conversations} skeletonRows={5}>
                {(d) => (
                  <DataTable
                    rows={d.items ?? []}
                    columns={conversationColumns}
                    rowKey={(c) => c.id}
                    caption="Conversations"
                    maxHeight={460}
                    empty="No conversation"
                    emptyHint="Threads land here once a platform with comment-read access is connected and a sync runs."
                    onRowClick={(c) => {
                      setSelectedConversation(c.id);
                      setSelectedInteraction(null);
                    }}
                  />
                )}
              </QueryBoundary>
            ) : (
              <QueryBoundary query={interactions} skeletonRows={5}>
                {(d) => (
                  <DataTable
                    rows={d.items ?? []}
                    columns={interactionColumns}
                    rowKey={(r) => r.id}
                    caption="Interactions"
                    maxHeight={460}
                    empty="Nothing matches these filters"
                    emptyHint="Loosen the platform, status, classification or search filters. An empty result here is a real answer, not a failed read."
                    onRowClick={(r) => {
                      setSelectedInteraction(r.id);
                      setSelectedConversation(null);
                    }}
                  />
                )}
              </QueryBoundary>
            )}
          </Panel>

          {selectedInteraction ? (
            <InteractionDetailPanel
              interactionId={selectedInteraction}
              onChanged={() => {
                interactions.reload();
                conversations.reload();
                actions.reload();
              }}
            />
          ) : selectedConversation ? (
            <ConversationPanel conversationId={selectedConversation} />
          ) : (
            <Panel title="Detail">
              <EmptyState
                title="Pick a row"
                description="A conversation opens its whole thread; an interaction opens its classification, suggested reply and response history."
              />
            </Panel>
          )}
        </>
      ) : null}

      {tab === "opportunities" ? (
        <Panel title="Opportunities" subtitle="Lead, partnership and repeated-request signals the engine recorded." dense>
          <QueryBoundary query={opportunities} skeletonRows={4}>
            {(d) => (
              <DataTable
                rows={d.items ?? []}
                rowKey={(o) => o.id}
                caption="Community opportunities"
                empty="No opportunity recorded"
                emptyHint="Signals appear once the community engine has evidence — a repeated question or an explicit partnership ask."
                columns={[
                  { key: "type", header: "Type", cell: (o) => humanize(o.opportunity_type) },
                  { key: "title", header: "Signal", cell: (o) => o.title || <span className="ym-muted">—</span> },
                  {
                    key: "confidence",
                    header: "Confidence",
                    cell: (o) => (
                      <Badge tone={o.confidence === "high" ? "success" : o.confidence === "medium" ? "warning" : "neutral"}>
                        {humanize(o.confidence)}
                      </Badge>
                    ),
                  },
                  { key: "evidence", header: "Signals", align: "right", cell: (o) => o.evidence_count },
                  { key: "state", header: "State", cell: (o) => <StatusBadge status={o.state} /> },
                  { key: "detail", header: "Detail", cell: (o) => o.detail || <span className="ym-muted">—</span>, hideBelow: "md" },
                ]}
              />
            )}
          </QueryBoundary>
        </Panel>
      ) : null}

      {tab === "insights" ? (
        <Panel title="Insights" dense>
          <QueryBoundary query={insights} skeletonRows={4}>
            {(d) => (
              <>
                {d.suggestions_available === false ? (
                  <p className="ym-hint">
                    The community engine is unavailable, so no content suggestion can
                    be produced. None is invented here.
                  </p>
                ) : null}
                <DataTable
                  rows={d.items ?? []}
                  rowKey={(i) => i.id}
                  caption="Community insights"
                  empty="No insight yet"
                  emptyHint="Repeated topics aggregate here once there is enough evidence behind them."
                  columns={[
                    { key: "topic", header: "Topic", cell: (i) => i.topic || i.topic_key || <span className="ym-muted">—</span> },
                    { key: "evidence", header: "Evidence", align: "right", cell: (i) => i.evidence_count },
                    {
                      key: "confidence",
                      header: "Confidence",
                      cell: (i) => (
                        <Badge tone={i.confidence === "high" ? "success" : i.confidence === "medium" ? "warning" : "neutral"}>
                          {humanize(i.confidence)}
                        </Badge>
                      ),
                    },
                    { key: "platforms", header: "Platforms", cell: (i) => (i.platforms ?? []).map((p) => humanize(p)).join(", ") || <span className="ym-muted">—</span>, hideBelow: "md" },
                    { key: "quote", header: "Representative", cell: (i) => i.representative_text || <span className="ym-muted">—</span>, hideBelow: "lg" },
                  ]}
                />
              </>
            )}
          </QueryBoundary>
        </Panel>
      ) : null}

      {tab === "history" ? (
        <Panel title="Response history" subtitle="Every queued, approved, rejected and sent community action, workspace-wide." dense>
          <QueryBoundary query={actions} skeletonRows={6}>
            {(d) => (
              <DataTable
                rows={d.items ?? []}
                rowKey={(a) => a.id}
                caption="Community action history"
                maxHeight={560}
                empty="No community action yet"
                emptyHint="A draft, approval or send is recorded here. Nothing auto-sends under DRAFT_ONLY."
                columns={[
                  { key: "platform", header: "Platform", cell: (a) => humanize(a.platform) },
                  { key: "type", header: "Action", cell: (a) => humanize(a.action_type) },
                  { key: "badge", header: "Provenance", cell: (a) => <ActionBadge action={a} /> },
                  { key: "state", header: "State", cell: (a) => <StatusBadge status={a.state} /> },
                  { key: "mode", header: "Autonomy", cell: (a) => humanize(a.mode), hideBelow: "md" },
                  { key: "mock", header: "Receipt", cell: (a) => (a.is_mock ? <Badge tone="mock">MOCK</Badge> : a.remote_reply_id ? <Badge tone="live">REMOTE ID</Badge> : <span className="ym-muted">none</span>) },
                  { key: "text", header: "Text", cell: (a) => a.final_text || a.draft_text || <span className="ym-muted">—</span>, hideBelow: "lg" },
                  { key: "when", header: "Created", cell: (a) => when(a.created_at), hideBelow: "lg" },
                ]}
              />
            )}
          </QueryBoundary>
        </Panel>
      ) : null}
    </>
  );
}

export default Community;