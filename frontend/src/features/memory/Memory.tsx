/* Memory — one canonical knowledge surface, replacing the overlapping legacy
 * Memory AND Knowledge screens.
 *
 *   GET  /knowledge/memories?type=&status=&scope=&q=      the store (GlobalMemory.list)
 *   POST /knowledge/memories                              store one typed memory
 *   POST /knowledge/memories/{id}/verify                  human verification
 *   POST /knowledge/memories/{id}/disable                 disable (idempotent)
 *   POST /knowledge/memories/{id}/supersede               point at a replacement
 *   GET  /knowledge/graph?node_type=&limit=               related entities + edges
 *   GET  /knowledge/sources                               connectors (config redacted)
 *   GET  /knowledge/sources/{id}/documents                the documents a connector ingested
 *   GET  /planner/memory?topic=                           what the planner already trusts
 *
 * ---------------------------------------------------------------------------
 * THE CORRECTNESS POINT: EVIDENCE IS NOT INTERPRETATION
 * ---------------------------------------------------------------------------
 * Two different things live in this store and the difference decides whether an
 * operator may act on them:
 *
 *   EVIDENCE       — something that happened or was ingested, with a reference
 *                    you can go and look at. A source document. A provenanced
 *                    memory row (one carrying `evidence_ids` or `source_ids`).
 *
 *   INTERPRETATION — a claim a system GENERATED from evidence. A CREATIVE_LESSON
 *                    derived from measured posts, an audience insight aggregated
 *                    from interactions, a platform learning mirrored from a
 *                    pattern. True or not, it is not itself a thing that
 *                    happened, and rendering it in the same shape as evidence is
 *                    how a machine's guess gets cited as a fact.
 *
 * Three backend facts make the split derivable rather than a UI opinion:
 *
 *  1. `GlobalMemory.store()` computes `provenanced = bool(source_ids or
 *     evidence_ids)` and stores a row with NO provenance as `UNVERIFIED`. The
 *     backend already refuses to call an unsourced claim evidence.
 *  2. The type vocabulary separates the two families. `RESEARCH_FACT`,
 *     `SOURCE`, `BRAND_KNOWLEDGE`, `USER_APPROVED_KNOWLEDGE`, `ENTITY` and
 *     `RELATIONSHIP` record what was found; `CONTENT_RESULT`,
 *     `CREATIVE_LESSON`, `AUDIENCE_INSIGHT`, `COMMUNITY_INSIGHT`,
 *     `PLATFORM_LEARNING` and `EXPERIMENT_RESULT` are written by engines that
 *     DERIVED them from measured outcomes (`planning/feedback.py`,
 *     `knowledge/community_bridge.py`, the Learning Agent, the experiment
 *     analyzer). Anything outside both lists is shown with its raw facts and NO
 *     category claim, because guessing here is the failure being prevented.
 *  3. `status` / `effective_status` / `conflict_group` / `superseded_by` /
 *     `freshness` are all server-computed and are rendered verbatim.
 *
 * So this screen never presents a derived claim with the same badge, panel or
 * tone as evidence, and it always shows the `evidence_ids` an interpretation was
 * built from — or says the derivation is untraceable.
 */

import { useMemo, useState, type ReactNode } from "react";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  Field,
  Grid,
  Modal,
  PageHeader,
  Panel,
  QueryBoundary,
  Select,
  StatTile,
  Tabs,
  Textarea,
  humanize,
  type Column,
  type Tone,
} from "../../design-system/primitives";
import { useMutation, useWsQuery } from "../../api/queries";
import { wsApi } from "../../lib/api";
import { blockedReason, can, useSession } from "../../state/session";

/* ==========================================================================
 * Response shapes
 * ======================================================================= */

/* `engine/knowledge/memory.py::TYPES` — the backend 422s anything else. */
export const MEMORY_TYPES = [
  "RESEARCH_FACT",
  "SOURCE",
  "CONTENT_RESULT",
  "AUDIENCE_INSIGHT",
  "COMMUNITY_INSIGHT",
  "BRAND_KNOWLEDGE",
  "CREATIVE_LESSON",
  "EXPERIMENT_RESULT",
  "PLATFORM_LEARNING",
  "ENTITY",
  "RELATIONSHIP",
  "USER_APPROVED_KNOWLEDGE",
] as const;

/* `engine/knowledge/freshness.py::ALL_STATUSES` — a status filter outside this
   tuple returns 422, so the filter only ever offers these values. */
export const ALL_MEMORY_STATUSES = [
  "ACTIVE",
  "CONFLICTED",
  "SUPERSEDED",
  "UNVERIFIED",
  "DISABLED",
  "FRESH",
  "AGING",
  "STALE",
] as const;

/* `engine/knowledge/freshness.py::BANDS` — filterable, but only on ACTIVE rows. */

/* `engine/knowledge/graph.py::NODE_TYPES` — an unknown node_type returns 422. */
export const NODE_TYPES = [
  "Brand",
  "Campaign",
  "Content",
  "Scene",
  "Topic",
  "Entity",
  "Source",
  "Claim",
  "AudienceInsight",
  "CommunityInsight",
  "Experiment",
  "CreativeLesson",
  "Publication",
] as const;

/* Types written by an engine that DERIVED them from a measured outcome. */
const DERIVED_TYPES = new Set([
  "CONTENT_RESULT",
  "CREATIVE_LESSON",
  "AUDIENCE_INSIGHT",
  "COMMUNITY_INSIGHT",
  "PLATFORM_LEARNING",
  "EXPERIMENT_RESULT",
]);

/* Types that record something found, ingested or declared. */
const EVIDENCE_TYPES = new Set([
  "RESEARCH_FACT",
  "SOURCE",
  "BRAND_KNOWLEDGE",
  "USER_APPROVED_KNOWLEDGE",
  "ENTITY",
  "RELATIONSHIP",
]);

/* `memory.py::_to_dict` — the read model, verbatim. */
export type MemoryRow = {
  id: string;
  workspace_id: string;
  brand_id: string | null;
  type: string;
  content: string;
  topic: string;
  topic_key: string;
  scope: string;
  platform: string;
  source_ids: string[];
  evidence_ids: unknown[];
  /** Clamped to [0,1]. Evidence QUALITY, not effect size. */
  confidence: number;
  /** FRESH | AGING | STALE, recomputed on read. */
  freshness: string;
  /** ACTIVE | CONFLICTED | SUPERSEDED | UNVERIFIED | DISABLED */
  status: string;
  /** Lifecycle status when set, else the freshness band. */
  effective_status: string;
  origin: string;
  content_hash: string;
  /** Non-empty ⇒ this row is one side of an unresolved contradiction. */
  conflict_group: string;
  last_verified_at: string | null;
  /** Set when a replacement took over. Content and history are preserved. */
  superseded_by: string | null;
  use_count: number;
  last_used_at: string | null;
  /** Service-owned; `store()` never accepts it. */
  related_json: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
};

export type MemoryList = { items: MemoryRow[] };

/* `engine/knowledge/graph.py::_node_dict` / `_edge_dict` */
export type GraphNode = {
  id: string;
  workspace_id: string;
  node_type: string;
  ref_id: string;
  node_key: string;
  label: string;
  topic_key: string;
  meta: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
};

export type GraphEdge = {
  id: string;
  workspace_id: string;
  from_node_id: string;
  to_node_id: string;
  relationship: string;
  weight: number;
  evidence_ids: string[];
  meta: Record<string, unknown>;
  created_at: string | null;
};

export type GraphResponse = { nodes: GraphNode[]; edges: GraphEdge[] };

/* `engine/sources/__init__.py::describe_connector` — config_json never leaves. */
export type SourceConnector = {
  id: string;
  kind: string;
  name: string;
  status: string;
  unavailable_reason: string;
  enabled: boolean;
  /** Redacted: token|key|secret|password|credential keys are stripped. */
  config: Record<string, unknown>;
  has_credentials: boolean;
  implemented: boolean;
  requires_credentials: boolean;
  title: string;
  blurb: string;
  doc_count: number;
  last_sync_at: string | null;
  last_error: string;
  last_cursor: string;
};

export type SourceList = { items: SourceConnector[] };

/* `knowledge.py::_document_dto` — content is preview-sized by the backend. */
export type SourceDocument = {
  id: string;
  connector_id: string;
  remote_id: string;
  title: string;
  mime_type: string;
  author: string;
  state: string;
  checksum: string;
  remote_created_at: string;
  remote_updated_at: string;
  created_at: string;
  updated_at: string;
  content: string;
};

export type DocumentList = { items: SourceDocument[] };

/* `planner.py::memory` — `MemoryBrief.to_dict()` plus the raw rows. */
export type PlannerMemory = {
  workspace_id: string;
  used_ids: string[];
  memory_count: number;
  /** Topics memory already covers, so the planner does not re-research. */
  settled_topics: string[];
  needs_revalidation: MemoryRow[];
  memories: MemoryRow[];
};

/* ==========================================================================
 * Provenance classification
 * ======================================================================= */

export type MemoryClass = "EVIDENCE" | "INTERPRETATION" | "UNCLASSIFIED";

/**
 * Classify one row from backend facts only.
 *
 * `provenanced` is the backend's own test (`store()`: `bool(source_ids or
 * evidence_ids)`). A type in the derived family is an interpretation even when
 * it cites evidence, because citing evidence does not turn a conclusion into an
 * observation. Anything outside both vocabularies is UNCLASSIFIED — this screen
 * declines to guess which side of the line a type it has never heard of falls on.
 */
export function classifyMemory(row: Pick<MemoryRow, "type" | "source_ids" | "evidence_ids">): MemoryClass {
  if (DERIVED_TYPES.has(row.type)) return "INTERPRETATION";
  if (EVIDENCE_TYPES.has(row.type)) return "EVIDENCE";
  const provenanced = (row.source_ids ?? []).length > 0 || (row.evidence_ids ?? []).length > 0;
  return provenanced ? "EVIDENCE" : "UNCLASSIFIED";
}

/** True on the backend's own definition. `store()` marks these UNVERIFIED. */
export function isProvenanced(row: Pick<MemoryRow, "source_ids" | "evidence_ids">): boolean {
  return (row.source_ids ?? []).length > 0 || (row.evidence_ids ?? []).length > 0;
}

const CLASS_TONE: Record<MemoryClass, Tone> = {
  EVIDENCE: "live",
  INTERPRETATION: "info",
  UNCLASSIFIED: "neutral",
};

const CLASS_COPY: Record<MemoryClass, string> = {
  EVIDENCE: "Something that happened or was ingested, with a reference you can open.",
  INTERPRETATION: "A claim a system generated from evidence. Not itself evidence.",
  UNCLASSIFIED: "Type outside both vocabularies; no category is claimed for it.",
};

/**
 * The badge that separates the two families.
 *
 * The wrapper carries `data-kind` deliberately: the distinction must be
 * machine-checkable, not only a colour, so a regression that swaps the two is
 * visible in a test rather than only to a designer's eye.
 */
export function ClassBadge({ row }: { row: Pick<MemoryRow, "type" | "source_ids" | "evidence_ids"> }) {
  const cls = classifyMemory(row);
  return (
    <span data-kind={cls}>
      <Badge tone={CLASS_TONE[cls]} title={CLASS_COPY[cls]}>
        {cls === "EVIDENCE" ? "STORED EVIDENCE" : cls === "INTERPRETATION" ? "GENERATED INTERPRETATION" : "UNCLASSIFIED"}
      </Badge>
    </span>
  );
}

/** The lifecycle/freshness state, kept distinct from the category badge. */
function StateBadge({ row }: { row: MemoryRow }) {
  const status = row.effective_status || row.status;
  const tone: Tone =
    status === "CONFLICTED"
      ? "danger"
      : status === "SUPERSEDED"
        ? "neutral"
        : status === "UNVERIFIED"
          ? "warning"
          : status === "DISABLED"
            ? "neutral"
            : status === "STALE"
              ? "warning"
              : status === "AGING"
                ? "info"
                : "success";
  return (
    <Badge tone={tone} title={`status=${row.status} · effective_status=${status} · freshness=${row.freshness}`}>
      {humanize(status)}
    </Badge>
  );
}

function Unavailable({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <span className="ym-muted" title={title}>
      {children ?? "UNAVAILABLE"}
    </span>
  );
}

function isoDay(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.replace("Z", "").slice(0, 10);
}

/** `evidence_ids` entries are stored loosely (a str or a dict). Never guess. */
function evidenceRef(value: unknown): string {
  if (typeof value === "string") return value;
  if (value && typeof value === "object") {
    const rec = value as Record<string, unknown>;
    const ref = rec.ref_id ?? rec.source_id ?? rec.id;
    return ref ? String(ref) : JSON.stringify(rec).slice(0, 80);
  }
  return String(value);
}

/* ==========================================================================
 * Memory tab
 * ======================================================================= */

const MEMBERSHIP_WRITE = "content.write";

function MemoryStore() {
  const [type, setType] = useState("");
  const [status, setStatus] = useState("");
  const [scope, setScope] = useState("");
  const [q, setQ] = useState("");
  const [applied, setApplied] = useState("");
  const [storing, setStoring] = useState(false);

  const params = new URLSearchParams();
  if (applied) params.set("q", applied);
  if (type) params.set("type", type);
  if (status) params.set("status", status);
  if (scope.trim()) params.set("scope", scope.trim());
  const query = `/knowledge/memories?${params.toString()}`;

  const memories = useWsQuery<MemoryList>(query);

  const rows = useMemo(() => {
    const items = memories.data?.items ?? [];
    return {
      all: items,
      evidence: items.filter((m) => classifyMemory(m) === "EVIDENCE"),
      interpretation: items.filter((m) => classifyMemory(m) === "INTERPRETATION"),
      conflicted: items.filter((m) => m.effective_status === "CONFLICTED" || !!m.conflict_group),
      superseded: items.filter((m) => m.effective_status === "SUPERSEDED" || !!m.superseded_by),
      unprovenanced: items.filter((m) => !isProvenanced(m)),
      stale: items.filter((m) => m.freshness === "STALE"),
      evidenceCount: items.reduce((n, m) => n + ((m.evidence_ids ?? []).length + (m.source_ids ?? []).length), 0),
    };
  }, [memories.data]);

  const columns: Column<MemoryRow>[] = [
    {
      key: "kind",
      header: "Kind",
      cell: (m) => <ClassBadge row={m} />,
    },
    { key: "type", header: "Type", cell: (m) => <Badge tone="neutral">{humanize(m.type)}</Badge> },
    {
      key: "content",
      header: "Memory",
      cell: (m) => (
        <span>
          <strong>{m.content?.slice(0, 140) || "—"}</strong>
          {m.topic ? <span className="ym-notif-detail"> · {m.topic}</span> : null}
        </span>
      ),
    },
    { key: "state", header: "State", cell: (m) => <StateBadge row={m} /> },
    {
      key: "scope",
      header: "Scope",
      cell: (m) =>
        m.scope ? <Badge tone="info">{m.scope}</Badge> : <span className="ym-muted">workspace-wide</span>,
      hideBelow: "lg",
    },
    {
      key: "prov",
      header: "Provenance",
      align: "right",
      cell: (m) => {
        const e = (m.evidence_ids ?? []).length;
        const s = (m.source_ids ?? []).length;
        if (e + s === 0) {
          return <Unavailable title="No evidence_ids and no source_ids — `store()` records this as UNVERIFIED.">none</Unavailable>;
        }
        return (
          <span title={`evidence_ids: ${e} · source_ids: ${s}`}>
            {e} ev{s > 0 ? ` · ${s} src` : ""}
          </span>
        );
      },
    },
    {
      key: "freshness",
      header: "Freshness",
      cell: (m) => (
        <span title={`freshness=${m.freshness} · last_verified_at=${m.last_verified_at ?? "never"}`}>
          {humanize(m.freshness)}
          {m.last_verified_at ? <span className="ym-muted"> · verified {isoDay(m.last_verified_at)}</span> : null}
        </span>
      ),
      hideBelow: "md",
    },
    {
      key: "conflict",
      header: "Conflict",
      cell: (m) =>
        m.conflict_group ? (
          <Badge tone="danger" title="Both sides of a contradiction are kept; nothing is deleted or overwritten.">
            {m.conflict_group}
          </Badge>
        ) : (
          <span className="ym-muted">—</span>
        ),
      hideBelow: "lg",
    },
    {
      key: "superseded",
      header: "Superseded by",
      cell: (m) =>
        m.superseded_by ? (
          <code title="Content and history are preserved; only the status and the pointer change.">
            {m.superseded_by.slice(0, 8)}
          </code>
        ) : (
          <span className="ym-muted">—</span>
        ),
      hideBelow: "lg",
    },
    {
      key: "confidence",
      header: "Confidence",
      align: "right",
      cell: (m) => (
        <span title="Evidence quality, clamped to [0,1] by the backend. Not an effect size and not a probability of being right.">
          {Number(m.confidence).toFixed(2)}
        </span>
      ),
      hideBelow: "md",
    },
  ];

  return (
    <>
      <Panel title="At a glance" subtitle="GET /knowledge/memories" dense>
        <Grid min={160} gap="sm">
          <StatTile
            label="Memories returned"
            value={rows.all.length}
            unavailable={memories.data === null}
            hint="After the active filters, not the whole store."
            source="GET /knowledge/memories"
          />
          <StatTile
            label="Stored evidence"
            value={rows.evidence.length}
            unavailable={memories.data === null}
            tone="live"
            hint="Recorded what was found or ingested."
            source="type + provenance"
          />
          <StatTile
            label="Generated interpretation"
            value={rows.interpretation.length}
            unavailable={memories.data === null}
            tone="info"
            hint="Derived from evidence. Not evidence itself."
            source="memory type vocabulary"
          />
          <StatTile
            label="Unprovenanced"
            value={rows.unprovenanced.length}
            unavailable={memories.data === null}
            tone={rows.unprovenanced.length > 0 ? "warning" : "neutral"}
            hint="No evidence_ids and no source_ids — a claim the backend stored as UNVERIFIED."
            source="GlobalMemory.store"
          />
          <StatTile
            label="Conflicted"
            value={rows.conflicted.length}
            unavailable={memories.data === null}
            tone={rows.conflicted.length > 0 ? "danger" : "neutral"}
            hint="Both sides preserved under one conflict group."
            source="conflict_group / effective_status"
          />
          <StatTile
            label="Superseded"
            value={rows.superseded.length}
            unavailable={memories.data === null}
            hint="Replaced, not deleted."
            source="superseded_by"
          />
          <StatTile
            label="Stale"
            value={rows.stale.length}
            unavailable={memories.data === null}
            tone={rows.stale.length > 0 ? "warning" : "neutral"}
            hint="FRESH < 7d, AGING < 30d, STALE beyond. Measured from last_verified_at, else created_at."
            source="freshness band"
          />
          <StatTile
            label="Provenance refs"
            value={rows.evidenceCount}
            unavailable={memories.data === null}
            hint="evidence_ids + source_ids summed across the returned rows."
            source="GET /knowledge/memories"
          />
        </Grid>
      </Panel>

      <Panel title="Filters" dense>
        <Grid min={170} gap="sm">
          <Select label="Type" value={type} onChange={(e) => setType(e.target.value)}>
            <option value="">All types</option>
            {MEMORY_TYPES.map((t) => (
              <option key={t} value={t}>
                {humanize(t)}
              </option>
            ))}
          </Select>
          <Select label="State" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">Any state</option>
            {ALL_MEMORY_STATUSES.map((s) => (
              <option key={s} value={s}>
                {humanize(s)}
              </option>
            ))}
          </Select>
          <Field label="Scope" value={scope} onChange={(e) => setScope(e.target.value)} placeholder="workspace" hint="Exact match on the scope column." />
          <Field label="Search content and topic" value={q} onChange={(e) => setQ(e.target.value)} placeholder="keyword" />
          <div>
            <Button
              variant="primary"
              onClick={() => {
                setScope(scope.trim());
                setApplied(q.trim());
              }}
            >
              Apply
            </Button>
          </div>
        </Grid>
        <p className="ym-hint">
          A lifecycle state (ACTIVE, CONFLICTED, SUPERSEDED, UNVERIFIED, DISABLED)
          filters the stored status column. A band (FRESH, AGING, STALE) filters
          the recomputed freshness of ACTIVE rows. Any other value is refused by
          the backend with 422, so only those eight are offered.
        </p>
      </Panel>

      <Panel title="Memory store" subtitle={query} dense>
        <QueryBoundary
          query={memories}
          skeletonRows={6}
          empty="No memory matches"
          emptyHint="Nothing in this workspace matches the active filters. A memory appears once an engine stores one or an operator does."
        >
          {() => (
            <DataTable
              rows={rows.all}
              columns={columns}
              rowKey={(m) => m.id}
              caption="Workspace memory store"
              maxHeight={620}
              empty="No memory matches"
              emptyHint="Nothing matches the active filters."
            />
          )}
        </QueryBoundary>
      </Panel>

      <Panel title="Row actions" subtitle="POST /knowledge/memories/{id}/{verify,disable,supersede} — the backend owns every transition" dense>
        <MemoryActions memories={rows.all} onDone={memories.reload} />
        <Button onClick={() => setStoring(true)}>Store a memory</Button>
      </Panel>

      {storing ? (
        <StoreMemoryModal
          onClose={() => setStoring(false)}
          onDone={() => {
            setStoring(false);
            memories.reload();
          }}
        />
      ) : null}
    </>
  );
}

/** Verify / disable / supersede for one selected row. */
function MemoryActions({ memories, onDone }: { memories: MemoryRow[]; onDone: () => void }) {
  const allowed = can(MEMBERSHIP_WRITE);
  const blocked = blockedReason(MEMBERSHIP_WRITE);
  const [selected, setSelected] = useState("");
  const [replacement, setReplacement] = useState("");

  const act = useMutation<string, MemoryRow>(
    (action) =>
      wsApi.post(
        `/knowledge/memories/${selected}/${action}`,
        action === "supersede" ? { replacement_id: replacement.trim() } : {},
      ) as Promise<MemoryRow>,
    { onSuccess: onDone },
  );

  const target = memories.find((m) => m.id === selected);

  return (
    <Grid min={200} gap="sm">
      <Select label="Memory" value={selected} onChange={(e) => setSelected(e.target.value)}>
        <option value="">Select a memory…</option>
        {memories.map((m) => (
          <option key={m.id} value={m.id}>
            {m.type} — {m.content?.slice(0, 48) || m.id.slice(0, 8)}
          </option>
        ))}
      </Select>
      <Field
        label="Replacement id (supersede only)"
        value={replacement}
        onChange={(e) => setReplacement(e.target.value)}
        hint="Both rows must exist in this workspace and be different."
      />
      <div>
        <Button disabled={!selected || !allowed} loading={act.pending} onClick={() => void act.run("verify")} title={blocked ?? "Records a human verification and refreshes the age anchor."}>
          Verify
        </Button>
        <Button disabled={!selected || !allowed} loading={act.pending} onClick={() => void act.run("disable")} title={blocked ?? "Sets DISABLED. The row is never deleted."}>
          Disable
        </Button>
        <Button
          disabled={!selected || !replacement.trim() || !allowed}
          loading={act.pending}
          onClick={() => void act.run("supersede")}
          title={blocked ?? "Marks the target SUPERSEDED and points it at the replacement."}
        >
          Supersede
        </Button>
      </div>
      {blocked ? <p className="ym-hint">{blocked}</p> : null}
      {target ? (
        <p className="ym-hint">
          Selected: <strong>{humanize(target.type)}</strong> · state {humanize(target.effective_status)}
          {target.superseded_by ? ` · superseded by ${target.superseded_by.slice(0, 8)}` : ""}
        </p>
      ) : null}
      {act.error ? <p className="ym-error">{act.error}</p> : null}
    </Grid>
  );
}

function StoreMemoryModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [type, setType] = useState<string>("RESEARCH_FACT");
  const [content, setContent] = useState("");
  const [topic, setTopic] = useState("");
  const [scope, setScope] = useState("");
  const [platform, setPlatform] = useState("");
  const [origin, setOrigin] = useState("");
  const [confidence, setConfidence] = useState("0.5");
  const [evidence, setEvidence] = useState("");

  const create = useMutation<void, MemoryRow>(
    () =>
      wsApi.post("/knowledge/memories", {
        type,
        content: content.trim(),
        topic: topic.trim(),
        scope: scope.trim(),
        platform: platform.trim(),
        origin: origin.trim() || "operator",
        confidence: Number(confidence),
        evidence_ids: evidence
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
      }) as Promise<MemoryRow>,
    { onSuccess: onDone },
  );

  return (
    <Modal
      open
      onClose={onClose}
      title="Store a memory"
      width={620}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" loading={create.pending} disabled={!content.trim()} onClick={() => void create.run()}>
            POST /knowledge/memories
          </Button>
        </>
      }
    >
      <p className="ym-hint">
        A row stored with no evidence and no source comes back as{" "}
        <strong>UNVERIFIED</strong>. The backend does that on purpose, and this
        screen will keep labelling it as a claim rather than evidence.
      </p>
      <Textarea label="Content" value={content} onChange={(e) => setContent(e.target.value)} />
      <Grid min={160} gap="sm">
        <Select label="Type" value={type} onChange={(e) => setType(e.target.value)}>
          {MEMORY_TYPES.map((t) => (
            <option key={t} value={t}>
              {humanize(t)}
            </option>
          ))}
        </Select>
        <Field label="Topic" value={topic} onChange={(e) => setTopic(e.target.value)} hint="Same topic + same content hashes to an existing row instead of duplicating it." />
        <Field label="Scope" value={scope} onChange={(e) => setScope(e.target.value)} placeholder="workspace" />
        <Field label="Platform" value={platform} onChange={(e) => setPlatform(e.target.value)} />
        <Field label="Origin" value={origin} onChange={(e) => setOrigin(e.target.value)} placeholder="operator" />
        <Field label="Confidence" type="number" min={0} max={1} step="0.05" value={confidence} onChange={(e) => setConfidence(e.target.value)} hint="Evidence quality, not effect size." />
        <Field label="Evidence ids" value={evidence} onChange={(e) => setEvidence(e.target.value)} placeholder="comma separated" hint="Blank stores the row as UNVERIFIED." />
      </Grid>
      {create.error ? <p className="ym-error">{create.error}</p> : null}
    </Modal>
  );
}

/* ==========================================================================
 * Evidence tab
 * ==========================================================================
 * What can be opened and checked: the documents a connector ingested, and the
 * memories that cite something.
 */

function EvidenceTab() {
  const sources = useWsQuery<SourceList>("/knowledge/sources");
  const [connectorId, setConnectorId] = useState("");
  const active = connectorId || sources.data?.items?.[0]?.id || "";

  const documents = useWsQuery<DocumentList>(
    active ? `/knowledge/sources/${encodeURIComponent(active)}/documents` : "",
    { enabled: !!active },
  );

  const sourceColumns: Column<SourceConnector>[] = [
    { key: "name", header: "Connector", cell: (s) => <strong>{s.name || s.title || s.kind}</strong> },
    { key: "kind", header: "Kind", cell: (s) => <Badge tone="neutral">{humanize(s.kind)}</Badge>, hideBelow: "md" },
    {
      key: "status",
      header: "Health",
      cell: (s) => (
        <Badge tone={s.status === "AVAILABLE" ? "success" : s.status === "DISABLED" ? "neutral" : "warning"} title={s.unavailable_reason || undefined}>
          {humanize(s.status)}
        </Badge>
      ),
    },
    {
      key: "creds",
      header: "Credentials",
      cell: (s) =>
        s.has_credentials ? (
          <Badge tone="success">present</Badge>
        ) : s.requires_credentials ? (
          <Badge tone="warning" title="This connector needs credentials it does not have.">missing</Badge>
        ) : (
          <span className="ym-muted">not required</span>
        ),
      hideBelow: "lg",
    },
    {
      key: "implemented",
      header: "Implemented",
      cell: (s) =>
        s.implemented ? (
          <Badge tone="success">yes</Badge>
        ) : (
          <Badge tone="unknown" title="Catalogued but not implemented in this deployment.">no</Badge>
        ),
      hideBelow: "lg",
    },
    { key: "docs", header: "Live documents", align: "right", cell: (s) => s.doc_count },
    {
      key: "sync",
      header: "Last sync",
      cell: (s) => (s.last_sync_at ? isoDay(s.last_sync_at) : <Unavailable title="This connector has never synced." />),
      hideBelow: "md",
    },
    {
      key: "error",
      header: "Last error",
      cell: (s) => (s.last_error ? <span className="ym-error">{s.last_error}</span> : <span className="ym-muted">—</span>),
      hideBelow: "lg",
    },
  ];

  const docColumns: Column<SourceDocument>[] = [
    { key: "title", header: "Document", cell: (d) => <strong>{d.title || d.remote_id || d.id.slice(0, 8)}</strong> },
    {
      key: "state",
      header: "State",
      cell: (d) => <Badge tone={d.state === "active" || d.state === "updated" ? "success" : "neutral"}>{humanize(d.state)}</Badge>,
    },
    { key: "mime", header: "Type", cell: (d) => (d.mime_type ? humanize(d.mime_type) : <Unavailable title="No MIME type recorded." />), hideBelow: "md" },
    { key: "author", header: "Author", cell: (d) => d.author || <span className="ym-muted">—</span>, hideBelow: "lg" },
    {
      key: "remote",
      header: "Remote id",
      cell: (d) => (d.remote_id ? <code>{d.remote_id}</code> : <Unavailable title="No remote identifier was recorded." />),
      hideBelow: "md",
    },
    {
      key: "checksum",
      header: "Checksum",
      cell: (d) => (d.checksum ? <code>{d.checksum.slice(0, 16)}</code> : <Unavailable title="No checksum recorded." />),
      hideBelow: "lg",
    },
    {
      key: "updated",
      header: "Updated",
      cell: (d) => isoDay(d.updated_at || d.remote_updated_at),
      hideBelow: "md",
    },
  ];

  return (
    <>
      <Panel
        title="Stored evidence"
        subtitle="Source connectors and the documents they ingested"
        dense
      >
        <p className="ym-hint">
          <span data-kind="EVIDENCE">
            <Badge tone="live">STORED EVIDENCE</Badge>
          </span>{" "}
          A document here is a thing that was ingested and can be opened. Nothing
          on this tab is a conclusion. A connector marked UNAVAILABLE has no
          documents behind it, and the backend says so itself.
        </p>
        <QueryBoundary
          query={sources}
          skeletonRows={5}
          empty="No source connector registered"
          emptyHint="A connector appears here once an admin registers one. Registering is an admin action the server enforces."
        >
          {(d) => (
            <DataTable
              rows={d.items ?? []}
              columns={sourceColumns}
              rowKey={(s) => s.id}
              caption="Source connectors"
              maxHeight={420}
              empty="No source connector registered"
              emptyHint="No connector exists for this workspace."
            />
          )}
        </QueryBoundary>
      </Panel>

      <Panel title="Ingested documents" subtitle="GET /knowledge/sources/{id}/documents" dense>
        {active ? (
          <>
            <Select label="Connector" value={active} onChange={(e) => setConnectorId(e.target.value)}>
              {(sources.data?.items ?? []).map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name || s.title || s.kind}
                </option>
              ))}
            </Select>
            <QueryBoundary
              query={documents}
              skeletonRows={5}
              empty="No document ingested"
              emptyHint="Documents appear once a sync completes. Deleted rows stay visible with their state rather than vanishing."
            >
              {(d) => (
                <DataTable
                  rows={d.items ?? []}
                  columns={docColumns}
                  rowKey={(doc) => doc.id}
                  caption="Ingested source documents"
                  maxHeight={480}
                  empty="No document ingested"
                  emptyHint="This connector has produced no document yet."
                />
              )}
            </QueryBoundary>
          </>
        ) : (
          <EmptyState title="No connector to inspect" description="Register a source connector first." />
        )}
      </Panel>

      <Panel
        title="Unprovenanced claims"
        subtitle="Rows the backend stored as UNVERIFIED — claims, not evidence"
        dense
      >
        <UnprovenancedClaims />
      </Panel>
    </>
  );
}

/** Memories with no evidence_ids and no source_ids, straight from the store. */
function UnprovenancedClaims() {
  const memories = useWsQuery<MemoryList>("/knowledge/memories?limit=200");

  const columns: Column<MemoryRow>[] = [
    { key: "kind", header: "Kind", cell: (m) => <ClassBadge row={m} /> },
    { key: "type", header: "Type", cell: (m) => humanize(m.type) },
    { key: "content", header: "Claim", cell: (m) => m.content || "—" },
    { key: "state", header: "State", cell: (m) => <StateBadge row={m} /> },
    { key: "origin", header: "Origin", cell: (m) => (m.origin ? humanize(m.origin) : <Unavailable title="No origin string was recorded." />), hideBelow: "md" },
  ];

  return (
    <QueryBoundary
      query={memories}
      skeletonRows={4}
      empty="Every memory carries provenance"
      emptyHint="No row in this workspace lacks both evidence_ids and source_ids."
    >
      {(d) => {
        const unprovenanced = (d.items ?? []).filter((m) => !isProvenanced(m));
        return (
          <>
            <p className="ym-hint">
              {unprovenanced.length} of {(d.items ?? []).length} returned memories
              carry no evidence and no source. <code>store()</code> records those
              as <strong>UNVERIFIED</strong>, and they stay visibly separate from
              evidence here — an unsourced claim is not a finding.
            </p>
            <DataTable
              rows={unprovenanced}
              columns={columns}
              rowKey={(m) => m.id}
              caption="Unprovenanced memory rows"
              maxHeight={380}
              empty="Every memory carries provenance"
              emptyHint="No row lacks evidence or a source."
            />
          </>
        );
      }}
    </QueryBoundary>
  );
}

/* ==========================================================================
 * Interpretation tab
 * ======================================================================= */

function InterpretationTab() {
  const memories = useWsQuery<MemoryList>("/knowledge/memories?limit=200");

  const columns: Column<MemoryRow>[] = [
    {
      key: "kind",
      header: "Kind",
      cell: (m) => <ClassBadge row={m} />,
    },
    { key: "type", header: "Derived type", cell: (m) => <Badge tone="info">{humanize(m.type)}</Badge> },
    { key: "claim", header: "Claim", cell: (m) => <strong>{m.content || "—"}</strong> },
    {
      key: "derived_from",
      header: "Derived from",
      cell: (m) => {
        const refs = (m.evidence_ids ?? []).map(evidenceRef).filter(Boolean);
        if (refs.length === 0) {
          return (
            <Unavailable title="This interpretation cites no evidence, so its derivation cannot be checked.">
              UNTRACEABLE
            </Unavailable>
          );
        }
        return (
          <span title={refs.join(", ")}>
            {refs.length} evidence ref{refs.length === 1 ? "" : "s"}
            <span className="ym-notif-detail"> · {refs[0].slice(0, 20)}</span>
          </span>
        );
      },
    },
    {
      key: "sources",
      header: "Sources",
      align: "right",
      cell: (m) =>
        (m.source_ids ?? []).length === 0 ? (
          <Unavailable title="No source_ids recorded.">none</Unavailable>
        ) : (
          m.source_ids.length
        ),
      hideBelow: "md",
    },
    { key: "state", header: "State", cell: (m) => <StateBadge row={m} /> },
    {
      key: "freshness",
      header: "Freshness",
      cell: (m) => (
        <span title={`freshness=${m.freshness} · last_verified_at=${m.last_verified_at ?? "never"}`}>
          {humanize(m.freshness)}
        </span>
      ),
      hideBelow: "md",
    },
    {
      key: "confidence",
      header: "Confidence",
      align: "right",
      cell: (m) => (
        <span title="Evidence quality, not effect size and not a probability that the claim is true.">
          {Number(m.confidence).toFixed(2)}
        </span>
      ),
      hideBelow: "lg",
    },
    { key: "origin", header: "Origin", cell: (m) => (m.origin ? humanize(m.origin) : <Unavailable title="No origin recorded." />), hideBelow: "lg" },
  ];

  return (
    <>
      <Panel
        title="Generated interpretation"
        subtitle="Claims a system generated from evidence — never evidence itself"
        dense
      >
        <p className="ym-hint">
          <span data-kind="INTERPRETATION">
            <Badge tone="info">GENERATED INTERPRETATION</Badge>
          </span>{" "}
          Every row here was written by an engine that derived it from measured
          outcomes. Cite it as a conclusion, never as an observation, and check
          the <strong>Derived from</strong> column before acting on it — a claim
          marked UNTRACEABLE cites nothing at all.
        </p>
        <QueryBoundary
          query={memories}
          skeletonRows={5}
          empty="No generated interpretation stored"
          emptyHint="These rows appear once an engine writes one — a measured outcome, a validated pattern, a promoted community insight."
        >
          {(d) => {
            const derived = (d.items ?? []).filter((m) => classifyMemory(m) === "INTERPRETATION");
            const untraceable = derived.filter((m) => (m.evidence_ids ?? []).length === 0);
            return (
              <>
                <Grid min={180} gap="sm">
                  <StatTile
                    label="Interpretations"
                    value={derived.length}
                    unavailable={false}
                    tone="info"
                    hint="Derived from measured outcomes."
                    source="memory type vocabulary"
                  />
                  <StatTile
                    label="Untraceable"
                    value={untraceable.length}
                    tone={untraceable.length > 0 ? "warning" : "neutral"}
                    hint="Cite no evidence, so the derivation cannot be checked."
                    source="evidence_ids"
                  />
                  <StatTile
                    label="Stale interpretations"
                    value={derived.filter((m) => m.freshness === "STALE").length}
                    tone={derived.some((m) => m.freshness === "STALE") ? "warning" : "neutral"}
                    hint="Derived 30+ days ago and never re-verified."
                    source="freshness band"
                  />
                </Grid>
                <DataTable
                  rows={derived}
                  columns={columns}
                  rowKey={(m) => m.id}
                  caption="Generated interpretations"
                  maxHeight={560}
                  empty="No generated interpretation stored"
                  emptyHint="No derived row exists for this workspace."
                />
              </>
            );
          }}
        </QueryBoundary>
      </Panel>
    </>
  );
}

/* ==========================================================================
 * Entities tab
 * ======================================================================= */

function EntitiesTab() {
  const [nodeType, setNodeType] = useState("");
  const graph = useWsQuery<GraphResponse>(
    `/knowledge/graph?limit=200${nodeType ? `&node_type=${encodeURIComponent(nodeType)}` : ""}`,
  );

  const nodeColumns: Column<GraphNode>[] = [
    { key: "label", header: "Entity", cell: (n) => <strong>{n.label || n.node_key}</strong> },
    { key: "type", header: "Type", cell: (n) => <Badge tone="neutral">{humanize(n.node_type)}</Badge> },
    {
      key: "ref",
      header: "Ref",
      cell: (n) => (n.ref_id ? <code>{n.ref_id.slice(0, 20)}</code> : <Unavailable title="No ref_id recorded." />),
      hideBelow: "md",
    },
    { key: "topic", header: "Topic key", cell: (n) => n.topic_key || <span className="ym-muted">—</span>, hideBelow: "lg" },
    { key: "updated", header: "Updated", cell: (n) => isoDay(n.updated_at), hideBelow: "md" },
  ];

  const edgeColumns: Column<GraphEdge>[] = [
    { key: "rel", header: "Relationship", cell: (e) => <Badge tone="info">{humanize(e.relationship)}</Badge> },
    { key: "from", header: "From", cell: (e) => <code>{e.from_node_id.slice(0, 10)}</code> },
    { key: "to", header: "To", cell: (e) => <code>{e.to_node_id.slice(0, 10)}</code>, hideBelow: "sm" },
    {
      key: "weight",
      header: "Weight",
      align: "right",
      cell: (e) => Number(e.weight).toFixed(2),
      hideBelow: "md",
    },
    {
      key: "ev",
      header: "Evidence",
      align: "right",
      cell: (e) =>
        (e.evidence_ids ?? []).length === 0 ? (
          <Unavailable title="This edge cites no evidence.">none</Unavailable>
        ) : (
          e.evidence_ids.length
        ),
      hideBelow: "md",
    },
  ];

  return (
    <>
      <Panel title="Related entities" subtitle="GET /knowledge/graph" dense>
        <Select label="Node type" value={nodeType} onChange={(e) => setNodeType(e.target.value)}>
          <option value="">All node types</option>
          {NODE_TYPES.map((t) => (
            <option key={t} value={t}>
              {humanize(t)}
            </option>
          ))}
        </Select>
        <p className="ym-hint">
          Only edges whose <em>both</em> endpoints are inside the returned node
          set are included, so there are no dangling references. A type outside
          this list is refused by the backend with 422.
        </p>
        <QueryBoundary
          query={graph}
          skeletonRows={6}
          empty="No entity in the graph"
          emptyHint="Nodes appear as memories, content and publications are linked into the graph."
        >
          {(d) => (
            <DataTable
              rows={d.nodes ?? []}
              columns={nodeColumns}
              rowKey={(n) => n.id}
              caption="Knowledge graph nodes"
              maxHeight={420}
              empty="No entity in the graph"
              emptyHint="Nothing has been linked into the graph for this workspace."
            />
          )}
        </QueryBoundary>
      </Panel>

      <Panel title="Relationships" subtitle="Internal edges between the nodes above" dense>
        <QueryBoundary query={graph} skeletonRows={4}>
          {(d) => (
            <DataTable
              rows={d.edges ?? []}
              columns={edgeColumns}
              rowKey={(e) => e.id}
              caption="Knowledge graph edges"
              maxHeight={420}
              empty="No relationship recorded"
              emptyHint="An edge appears when two nodes are linked by a known relationship."
            />
          )}
        </QueryBoundary>
      </Panel>
    </>
  );
}

/* ==========================================================================
 * Planner tab
 * ======================================================================= */

function PlannerTab() {
  const [topic, setTopic] = useState("");
  const params = new URLSearchParams();
  if (topic.trim()) params.set("topic", topic.trim());
  const brief = useWsQuery<PlannerMemory>(`/planner/memory?${params.toString()}`);

  const settledColumns: Column<string>[] = [
    { key: "topic", header: "Settled topic", cell: (t) => <strong>{t}</strong> },
  ];

  return (
    <>
      <Panel
        title="What the planner already trusts"
        subtitle="GET /planner/memory — consulted before any new research"
        dense
      >
        <Field
          label="Topic (optional)"
          value={topic}
          onChange={(e) => setTopic(e.target.value)}
          placeholder="topic key"
          hint="Narrows the brief to one topic."
        />
        <QueryBoundary query={brief} skeletonRows={4}>
          {(d) => (
            <>
              <Grid min={170} gap="sm">
                <StatTile
                  label="Memories consulted"
                  value={d.memory_count}
                  hint="Rows the planner reads before planning."
                  source="GET /planner/memory · memory_count"
                />
                <StatTile
                  label="Settled topics"
                  value={(d.settled_topics ?? []).length}
                  tone={(d.settled_topics ?? []).length > 0 ? "success" : "neutral"}
                  hint="Topics memory already covers, so the planner does not re-research them."
                  source="GET /planner/memory · settled_topics"
                />
                <StatTile
                  label="Needs revalidation"
                  value={(d.needs_revalidation ?? []).length}
                  tone={(d.needs_revalidation ?? []).length > 0 ? "warning" : "neutral"}
                  hint="Memories the planner flags as no longer trustworthy."
                  source="GET /planner/memory · needs_revalidation"
                />
                <StatTile
                  label="Cited ids"
                  value={(d.used_ids ?? []).length}
                  hint="Memory ids recorded on the plan item as actually used."
                  source="GET /planner/memory · used_ids"
                />
              </Grid>
              <DataTable
                rows={d.settled_topics ?? []}
                columns={settledColumns}
                rowKey={(t) => t}
                caption="Settled topics"
                maxHeight={220}
                empty="No settled topic"
                emptyHint="A topic becomes settled once memory covers it; the planner then skips re-researching it."
              />
            </>
          )}
        </QueryBoundary>
      </Panel>

      <Panel title="Flagged for revalidation" subtitle="Memory the planner no longer fully trusts" dense>
        <QueryBoundary query={brief} skeletonRows={4}>
          {(d) => (
            <DataTable
              rows={d.needs_revalidation ?? []}
              rowKey={(m) => m.id}
              caption="Memories needing revalidation"
              maxHeight={420}
              empty="Nothing flagged"
              emptyHint="The planner flags nothing in this workspace right now."
              columns={[
                { key: "kind", header: "Kind", cell: (m) => <ClassBadge row={m} /> },
                { key: "type", header: "Type", cell: (m) => humanize(m.type) },
                { key: "content", header: "Memory", cell: (m) => m.content || "—" },
                { key: "state", header: "State", cell: (m) => <StateBadge row={m} /> },
                {
                  key: "fresh",
                  header: "Freshness",
                  cell: (m) => humanize(m.freshness),
                  hideBelow: "md",
                },
              ]}
            />
          )}
        </QueryBoundary>
      </Panel>
    </>
  );
}

/* ==========================================================================
 * Screen
 * ======================================================================= */

const TABS = [
  { id: "memory", label: "Memory" },
  { id: "evidence", label: "Evidence" },
  { id: "interpretation", label: "Interpretation" },
  { id: "entities", label: "Entities" },
  { id: "planner", label: "Planner view" },
] as const;

type TabId = (typeof TABS)[number]["id"];

export function Memory() {
  const { workspaceId, workspace } = useSession();
  const [tab, setTab] = useState<TabId>("memory");

  if (!workspaceId) {
    return (
      <>
        <PageHeader title="Memory" description="One canonical knowledge surface." />
        <Panel title="No workspace selected">
          <EmptyState title="Nothing to remember" description="Memory is workspace-isolated. Select a workspace first." />
        </Panel>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Memory"
        description={
          workspace?.name
            ? `${workspace.name} — memory, provenance and freshness. Stored evidence and generated interpretation are never shown as the same thing.`
            : "Memory, provenance and freshness. Stored evidence and generated interpretation are never shown as the same thing."
        }
      />

      <Tabs tabs={TABS.map((t) => ({ id: t.id, label: t.label }))} active={tab} onChange={(id) => setTab(id as TabId)} />

      {tab === "memory" ? <MemoryStore /> : null}
      {tab === "evidence" ? <EvidenceTab /> : null}
      {tab === "interpretation" ? <InterpretationTab /> : null}
      {tab === "entities" ? <EntitiesTab /> : null}
      {tab === "planner" ? <PlannerTab /> : null}
    </>
  );
}

export default Memory;
