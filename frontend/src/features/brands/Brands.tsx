/* Brands — BrandDNA only.
 *
 * ENDPOINTS (BrandDNA surface + the two white-label reads that are NOT
 * BrandDNA, kept visibly apart):
 *
 *   GET    /brands                       api/v1/brands.py `list_brands` (175)
 *   POST   /brands                       `create_brand` (193)              201
 *   GET    /brands/{id}                  `get_brand` (266)
 *   PUT    /brands/{id}                  `update_brand` (278)
 *   GET    /brands/presets               `list_presets` (210)
 *   GET    /brands/effective             `effective_policy` (233)
 *   POST   /brands/{id}/assets           `link_asset` (311)
 *   DELETE /brands/{id}/assets           `unlink_asset` (353)
 *   POST   /brands/{id}/verify           `verify` (383)
 *   GET    /brand                        api/v1/workspaces.py `get_brand` (171) --
 *                                         the LEGACY white-label chrome kit
 *                                         (app_name/accent/logo_path). It is a
 *                                         different object from BrandDNA and is
 *                                         labelled as such wherever it appears.
 *   GET    /brand/logo/file              api/v1/workspaces.py `brand_logo_file` (202)
 *   GET    /music/policy, /music/prefs   api/v1/music.py (131 / 201)
 *   GET    /lessons                      api/v1/lessons.py `list_lessons` (58)
 *
 * THE LEGACY CHROME PAGE IS NOT IMPORTED.
 *
 * `pages/Brand.tsx` is a chrome-settings screen for `workspace.settings_json`.
 * Nothing in this file imports it or its `components/ui` chrome; the only thing
 * borrowed from that legacy shape is the read of `GET /brand`, shown under its
 * own heading as "Workspace chrome".
 *
 * A RECOMMENDATION IS NOT A POLICY.
 *
 * This is the one rule the file is built around. Brand constraints come from
 * `GET /brands/effective` and only from there:
 *
 *   * `POLICY_FIELDS` is a CLOSED list of the typed `EffectiveCreativePolicy`
 *     fields (backend/app/engine/brand/policy.py). `policyEntriesFrom` walks
 *     that list and reads nothing else, so no other payload can become a
 *     policy row — not a lesson, not a performance metric, not a preset.
 *   * A `PolicyEntry` carries a `provenance` level that only the resolver emits
 *     (workspace | brand | campaign | content | platform). Nothing else in the
 *     backend produces that value for a field.
 *   * `Recommendation` is a different type with a different shape and is
 *     rendered by a different component into a `data-policy-kind="recommendation"`
 *     region, outside the `data-policy-kind="policy"` region.
 *
 * A recommendation can inform a human. It is never a constraint, and the UI
 * cannot express it as one.
 */

import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  Badge,
  Button,
  DataTable,
  DestructiveButton,
  EmptyState,
  Field,
  Grid,
  Modal,
  PageHeader,
  Panel,
  QueryBoundary,
  Select,
  StatTile,
  StatusBadge,
  Tabs,
  Textarea,
  cx,
  humanize,
  type Column,
  type Tone,
} from "../../design-system/primitives";
import { useMutation, useWsQuery } from "../../api/queries";
import { fetchWorkspaceLogo, wsApi } from "../../lib/api";
import { useSession } from "../../state/session";

/* ==========================================================================
 * Response shapes — typed from the backend routers named above
 * ======================================================================= */

/** `brands._brand_dto` — backend/app/api/v1/brands.py:136. */
export type BrandDto = {
  id: string;
  workspace_id: string;
  name: string;
  is_default: boolean;
  /** BRAND_STATUSES: active | archived */
  status: string;
  /** The BrandDNA document (backend/app/engine/brand/dna.py `BrandDNA`). */
  dna: BrandDna;
  assets: BrandAssetDto[];
  created_at: string;
  updated_at: string;
};

/** `brands._asset_dto` — backend/app/api/v1/brands.py:125. */
export type BrandAssetDto = {
  id: string;
  brand_id: string;
  /** BRAND_ASSET_ROLES. */
  asset_role: string;
  media_asset_id: string;
  label: string;
  created_at: string;
};

export type BrandList = { brands: BrandDto[] };
export type BrandDetail = { brand: BrandDto };
export type BrandPresetList = {
  presets: { id: string; name: string; builtin: boolean; preset: Record<string, unknown> }[];
};

/** `BrandDNA` — backend/app/engine/brand/dna.py:136. Extra fields are allowed. */
export type BrandDna = {
  logos?: string[];
  colors?: Record<string, string>;
  fonts?: Record<string, unknown>;
  typography?: Record<string, unknown>;
  caption_style?: Record<string, unknown>;
  cta_style?: Record<string, unknown>;
  watermark?: Record<string, unknown>;
  intro?: string;
  outro?: string;
  lower_thirds?: unknown[];
  motion_language?: Record<string, unknown>;
  music_prefs?: Record<string, unknown>;
  voice_identity?: Record<string, unknown>;
  avatar_identity?: Record<string, unknown>;
  visual_style?: Record<string, unknown>;
  broll_prefs?: Record<string, unknown>;
  thumbnail_style?: Record<string, unknown>;
  writing_tone?: string | Record<string, unknown>;
  vocabulary?: Record<string, unknown>;
  pronunciation_rules?: unknown[];
  forbidden_phrases?: string[];
  claims_policy?: Record<string, unknown>;
  platform_overrides?: Record<string, Record<string, unknown>>;
  [extra: string]: unknown;
};

/** `EffectiveCreativePolicy.as_dict()` — backend/app/engine/brand/policy.py:250. */
export type EffectivePolicy = {
  forbidden_phrases: string[];
  required_disclaimers: string[];
  logo_safe_zone: Record<string, unknown>;
  approved_voices: string[];
  approved_avatars: string[];
  brand_colors: string[];
  caption_style: Record<string, unknown>;
  cta_style: Record<string, unknown>;
  tone: string;
  vocabulary: Record<string, unknown>;
  pronunciation_rules: unknown[];
  platform: string | null;
  provenance: Record<string, string>;
  effective_config_id: string;
  dna_version: string;
  workspace_id: string;
  brand_id: string;
  subject: Record<string, unknown>;
  approved_logos: string[];
  watermark: Record<string, unknown>;
  thumbnail_style: Record<string, unknown>;
  fonts: Record<string, unknown>;
  claims_policy: Record<string, unknown>;
};

/** `effective_policy` response — backend/app/api/v1/brands.py:256. */
export type EffectiveResponse = {
  policy: EffectivePolicy;
  hard_constraints: {
    forbidden_phrases: string[];
    required_disclaimers: string[];
    logo_safe_zone: Record<string, unknown>;
    approved_voices: string[];
    approved_avatars: string[];
  };
  provenance: Record<string, string>;
  effective_config_id: string;
  dna_version: string;
  subject: Record<string, unknown>;
};

/** `BrandConsistencyReport.to_dict()` — backend/app/engine/brand/verifier.py:282. */
export type VerifyReport = {
  status: string;
  checks: unknown;
  artifact_kind: string;
  workspace_id: string;
  authoritative: boolean;
  effective_config_id: string;
  provenance: Record<string, string>;
  semantic: Record<string, unknown>;
  report_type: string;
};

/** `workspaces.resolve_brand` — backend/app/api/v1/workspaces.py:161. NOT BrandDNA. */
export type WorkspaceChrome = { app_name: string; accent: string; logo_path: string };

/** `_lesson_dto` — backend/app/api/v1/lessons.py:21. A RECOMMENDATION, NOT POLICY. */
export type LessonRow = {
  id: string;
  workspace_id: string;
  pattern_key: string;
  metric: string;
  description: string;
  scope: Record<string, unknown>;
  effect: Record<string, unknown>;
  confidence: number | null;
  sample_size: number | null;
  evidence_ids: string[];
  status: string;
  generation: number;
  created_at: string;
  last_validated_at: string;
  active: boolean;
};
export type LessonList = { items: LessonRow[] };

/** `get_music_prefs` — backend/app/api/v1/music.py:212. */
export type MusicPrefs = {
  workspace_id: string;
  prefs: Record<string, unknown>;
  keys: string[];
  stated: boolean;
  note: string;
};

/* ==========================================================================
 * Policy vs recommendation — the separation, as types
 * ======================================================================= */

/** `LEVELS` in backend/app/engine/brand/policy.py:28. Nothing else emits one. */
export type PolicyProvenance = "workspace" | "brand" | "campaign" | "content" | "platform";

/** The typed `EffectiveCreativePolicy` field names this screen reads. */
export type PolicyField =
  | "forbidden_phrases"
  | "required_disclaimers"
  | "logo_safe_zone"
  | "approved_voices"
  | "approved_avatars"
  | "brand_colors"
  | "caption_style"
  | "cta_style"
  | "tone"
  | "vocabulary"
  | "pronunciation_rules"
  | "approved_logos"
  | "watermark"
  | "thumbnail_style"
  | "fonts"
  | "claims_policy";

/**
 * A hard BrandDNA constraint. `provenance` is REQUIRED and typed to the
 * resolver's levels, so a value that did not come out of the inheritance
 * resolver cannot be expressed as one.
 */
export type PolicyEntry = {
  field: PolicyField;
  value: PolicyValue;
  provenance: PolicyProvenance;
  /** True for HARD_CONSTRAINT_KEYS — empty never overrides, see policy.py:32. */
  hard: boolean;
};

export type PolicyValue =
  | { kind: "text"; text: string }
  | { kind: "list"; items: string[] }
  | { kind: "object"; entries: { key: string; value: string }[] }
  | { kind: "none" };

/** A measured learning. Deliberately NOT assignable to `PolicyEntry`. */
export type Recommendation = {
  patternKey: string;
  text: string;
  metric: string;
  confidence: number | null;
  sampleSize: number | null;
  status: string;
  evidenceCount: number;
};

/** HARD_CONSTRAINT_KEYS — backend/app/engine/brand/policy.py:32. */
export const HARD_CONSTRAINT_FIELDS: readonly PolicyField[] = [
  "forbidden_phrases",
  "required_disclaimers",
  "logo_safe_zone",
  "approved_voices",
  "approved_avatars",
];

/** Every policy field this screen will render, in reading order. */
export const POLICY_FIELDS: readonly PolicyField[] = [
  ...HARD_CONSTRAINT_FIELDS,
  "brand_colors",
  "caption_style",
  "cta_style",
  "tone",
  "vocabulary",
  "pronunciation_rules",
  "approved_logos",
  "watermark",
  "thumbnail_style",
  "fonts",
  "claims_policy",
];

/** BRAND_ASSET_ROLES — backend/app/models/brand.py:32. */
export const BRAND_ASSET_ROLES: readonly string[] = [
  "logo",
  "watermark",
  "intro",
  "outro",
  "lower_third",
  "thumbnail_frame",
];

const PROVENANCE_LEVELS: readonly PolicyProvenance[] = [
  "workspace",
  "brand",
  "campaign",
  "content",
  "platform",
];

function toProvenanceLevel(raw: string | undefined): PolicyProvenance {
  const value = String(raw ?? "").trim().toLowerCase();
  return (PROVENANCE_LEVELS as readonly string[]).includes(value)
    ? (value as PolicyProvenance)
    : "workspace";
}

/** Shape an arbitrary policy value without inventing content for it. */
export function toPolicyValue(value: unknown): PolicyValue {
  if (value === null || value === undefined || value === "") return { kind: "none" };
  if (typeof value === "string") return { kind: "text", text: value };
  if (typeof value === "number" || typeof value === "boolean") {
    return { kind: "text", text: String(value) };
  }
  if (Array.isArray(value)) {
    const items = value.map((entry) =>
      typeof entry === "string" ? entry : JSON.stringify(entry),
    );
    return items.length ? { kind: "list", items } : { kind: "none" };
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>).map(
      ([key, entry]) => ({ key, value: formatScalar(entry) }),
    );
    return entries.length ? { kind: "object", entries } : { kind: "none" };
  }
  return { kind: "none" };
}

function formatScalar(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

/**
 * The ONLY way a policy row is created.
 *
 * It iterates `POLICY_FIELDS` — a closed list — and reads provenance out of the
 * resolver response. Any other payload in the app (a lesson, a performance
 * metric, a preset) is unreachable from here by construction, which is why a
 * recommendation cannot render as policy even if one is handed in.
 */
export function policyEntriesFrom(effective: EffectiveResponse | null): PolicyEntry[] {
  if (!effective || !effective.policy) return [];
  const policy = effective.policy;
  const provenance = effective.provenance ?? policy.provenance ?? {};
  return POLICY_FIELDS.map((field) => ({
    field,
    value: toPolicyValue(policy[field]),
    provenance: toProvenanceLevel(provenance[field]),
    hard: HARD_CONSTRAINT_FIELDS.includes(field),
  }));
}

/** Runtime guard used by the tests and by the renderer. */
export function isPolicyEntry(candidate: unknown): candidate is PolicyEntry {
  if (typeof candidate !== "object" || candidate === null) return false;
  const row = candidate as Record<string, unknown>;
  return (
    typeof row.field === "string" &&
    (POLICY_FIELDS as readonly string[]).includes(row.field) &&
    typeof row.provenance === "string" &&
    (PROVENANCE_LEVELS as readonly string[]).includes(row.provenance) &&
    typeof row.value === "object" &&
    row.value !== null &&
    typeof row.hard === "boolean"
  );
}

/** Lessons become recommendations. Never policy entries. */
export function toRecommendations(rows: LessonRow[] | null | undefined): Recommendation[] {
  if (!rows) return [];
  return rows
    .filter((row) => row && typeof row.description === "string" && row.description.trim())
    .map((row) => ({
      patternKey: row.pattern_key,
      text: row.description,
      metric: row.metric || "",
      confidence: typeof row.confidence === "number" ? row.confidence : null,
      sampleSize: typeof row.sample_size === "number" ? row.sample_size : null,
      status: row.status || "",
      evidenceCount: Array.isArray(row.evidence_ids) ? row.evidence_ids.length : 0,
    }));
}

/* ==========================================================================
 * DNA section descriptors
 * ======================================================================= */

type DnaSection = {
  id: string;
  label: string;
  /** Keys read out of the BrandDNA document, in render order. */
  keys: string[];
  note: string;
};

const IDENTITY_KEYS: DnaSection = {
  id: "identity",
  label: "Identity",
  keys: ["logos", "colors", "fonts", "typography", "visual_style", "thumbnail_style"],
  note: "References and tokens only. BrandDNA stores MediaAsset ids — never bytes, never base64.",
};

const CREATIVE_KEYS: DnaSection = {
  id: "creative",
  label: "Creative Rules",
  keys: [
    "caption_style",
    "cta_style",
    "watermark",
    "intro",
    "outro",
    "lower_thirds",
    "motion_language",
    "broll_prefs",
    "platform_overrides",
  ],
  note: "Platform fragments under platform_overrides are applied LAST by the inheritance resolver.",
};

const VOICE_KEYS: DnaSection = {
  id: "voice",
  label: "Voice",
  keys: ["writing_tone", "vocabulary", "pronunciation_rules", "voice_identity", "avatar_identity"],
  note: "voice_identity / avatar_identity hold reference ids, never provider payloads.",
};

/* ==========================================================================
 * Small formatters
 * ======================================================================= */

function policyTone(hard: boolean): Tone {
  return hard ? "warning" : "neutral";
}

function provenanceTone(level: PolicyProvenance): Tone {
  if (level === "workspace") return "neutral";
  if (level === "brand") return "info";
  return "handoff";
}

function createdAt(iso: string): string {
  return iso ? iso.replace("Z", " UTC").replace("T", " ").slice(0, 16) : "—";
}

const MONO: React.CSSProperties = { fontFamily: "var(--font-mono)" };

/* ==========================================================================
 * Renderers
 * ======================================================================= */

function PolicyValueCell({ value }: { value: PolicyValue }) {
  if (value.kind === "none") return <span className="ym-muted">not stated</span>;
  if (value.kind === "text") return <span>{value.text}</span>;
  if (value.kind === "list") {
    return (
      <span style={{ display: "inline-flex", gap: "var(--space-1)", flexWrap: "wrap" }}>
        {value.items.map((item, i) => (
          <Badge key={`${item}-${i}`} tone="neutral">
            {item}
          </Badge>
        ))}
      </span>
    );
  }
  return (
    <span style={{ display: "inline-flex", gap: "var(--space-1)", flexWrap: "wrap" }}>
      {value.entries.map((entry) => (
        <Badge key={entry.key} tone="neutral">
          {entry.key}: {entry.value}
        </Badge>
      ))}
    </span>
  );
}

/**
 * The policy table. It accepts `PolicyEntry[]` and nothing else, so the only way
 * a recommendation reaches this component is a type error.
 */
function PolicyTable({ entries, caption }: { entries: PolicyEntry[]; caption: string }) {
  const columns: Column<PolicyEntry>[] = [
    {
      key: "field",
      header: "Constraint",
      cell: (e) => (
        <span>
          <span style={MONO}>{e.field}</span>{" "}
          {e.hard ? (
            <Badge tone="warning" title="HARD_CONSTRAINT_KEYS: empty never overrides a lower level">
              hard
            </Badge>
          ) : null}
        </span>
      ),
    },
    { key: "value", header: "Effective value", cell: (e) => <PolicyValueCell value={e.value} /> },
    {
      key: "provenance",
      header: "Set by",
      cell: (e) => <Badge tone={provenanceTone(e.provenance)}>{e.provenance}</Badge>,
    },
  ];
  return (
    <div data-policy-kind="policy">
      <DataTable
        rows={entries}
        columns={columns}
        rowKey={(e) => e.field}
        caption={caption}
        empty="No policy field resolved"
        emptyHint="The resolver returned an empty policy. Nothing is constrained until the DNA states it."
      />
    </div>
  );
}

/**
 * Recommendations. A separate type, a separate region, an explicit label.
 * `role="note"` + `data-policy-kind="recommendation"` keeps it findable and
 * keeps it out of the policy table both visually and structurally.
 */
function RecommendationList({ items }: { items: Recommendation[] }) {
  return (
    <div data-policy-kind="recommendation" role="note" aria-label="Learning recommendations">
      <p className="ym-hint">
        <Badge tone="handoff">Recommendation</Badge> Measured learning. Advisory only — a recommendation is never
        applied as a BrandDNA constraint, and nothing below is enforced by the verifier.
      </p>
      <DataTable
        rows={items}
        columns={[
          {
            key: "rec",
            header: "Recommendation",
            cell: (r) => (
              <span>
                <Badge tone="handoff">Recommendation</Badge>{" "}
                <span style={MONO}>{r.patternKey}</span>
                <div className="ym-muted">{r.text}</div>
              </span>
            ),
          },
          { key: "metric", header: "Metric", cell: (r) => r.metric || <span className="ym-muted">not reported</span> },
          {
            key: "confidence",
            header: "Confidence",
            align: "right",
            cell: (r) => (r.confidence === null ? <span className="ym-muted">UNAVAILABLE</span> : r.confidence.toFixed(2)),
          },
          {
            key: "sample",
            header: "Sample",
            align: "right",
            cell: (r) => (r.sampleSize === null ? <span className="ym-muted">UNAVAILABLE</span> : r.sampleSize),
          },
          {
            key: "evidence",
            header: "Evidence",
            align: "right",
            cell: (r) =>
              r.evidenceCount === 0 ? <span className="ym-muted">UNAVAILABLE</span> : r.evidenceCount,
          },
          { key: "status", header: "Status", cell: (r) => <Badge tone="neutral">{humanize(r.status)}</Badge> },
        ]}
        rowKey={(r) => r.patternKey}
        caption="Learning recommendations"
        empty="No recommendation yet"
        emptyHint="GET /lessons returns nothing for this workspace. No preference was invented to fill the gap."
      />
    </div>
  );
}

function DnaSectionView({ dna, section }: { dna: BrandDna; section: DnaSection }) {
  const rows = section.keys.map((key) => ({
    key,
    value: toPolicyValue(dna[key]),
  }));
  return (
    <div>
      <p className="ym-hint">{section.note}</p>
      <DataTable
        rows={rows}
        columns={[
          { key: "key", header: "DNA field", cell: (r) => <span style={MONO}>{r.key}</span> },
          { key: "value", header: "Value", cell: (r) => <PolicyValueCell value={r.value} /> },
        ]}
        rowKey={(r) => r.key}
        caption={`${section.label} DNA fields`}
        empty="No field"
      />
    </div>
  );
}

function EmptyDna({ section }: { section: DnaSection }) {
  return (
    <EmptyState
      title={`${section.label} not stated`}
      description={`The BrandDNA document carries no ${section.label.toLowerCase()} fields. Nothing was defaulted into them.`}
    />
  );
}

/* ==========================================================================
 * Component
 * ======================================================================= */

type TabId = "overview" | "identity" | "creative" | "voice" | "music" | "assets" | "policies" | "effective";

const TABS: { id: TabId; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "identity", label: "Identity" },
  { id: "creative", label: "Creative Rules" },
  { id: "voice", label: "Voice" },
  { id: "music", label: "Music" },
  { id: "assets", label: "Assets" },
  { id: "policies", label: "Policies" },
  { id: "effective", label: "Effective Policy" },
];

export function Brands() {
  const { workspaceId } = useSession();
  const enabled = Boolean(workspaceId);

  const [tab, setTab] = useState<TabId>("overview");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [platform, setPlatform] = useState("");
  const [linkAssetId, setLinkAssetId] = useState("");
  const [linkRole, setLinkRole] = useState("logo");
  const [verifyArtifact, setVerifyArtifact] = useState('{"kind":"clip"}');
  const [createName, setCreateName] = useState("");
  const [showCreate, setShowCreate] = useState(false);

  const list = useWsQuery<BrandList>("/brands", { enabled });
  const presets = useWsQuery<BrandPresetList>("/brands/presets", { enabled });
  const lessons = useWsQuery<LessonList>("/lessons", { enabled });
  const chrome = useWsQuery<{ brand: WorkspaceChrome }>("/brand", { enabled });
  const musicPrefs = useWsQuery<MusicPrefs>("/music/prefs", { enabled });
  const musicPolicy = useWsQuery<{ generate: boolean; reason: string; brand_disabled: boolean }>("/music/policy", {
    enabled,
  });

  const brands = list.data === null ? null : Array.isArray(list.data.brands) ? list.data.brands : [];
  const activeId = selectedId ?? brands?.find((b) => b.is_default)?.id ?? brands?.[0]?.id ?? null;
  const active = brands?.find((b) => b.id === activeId) ?? null;
  /* A link list that came back without `assets` is "not reported", not []. */
  const activeAssets = Array.isArray(active?.assets) ? active.assets : [];

  /* `brands/effective` is a resolver, not a lookup: it re-resolves the whole
   * precedence chain per call, so the query key carries the platform. */
  const effectivePath = useMemo(() => {
    const params = new URLSearchParams();
    if (activeId) params.set("brand_id", activeId);
    if (platform) params.set("platform", platform);
    return `/brands/effective?${params.toString()}`;
  }, [activeId, platform]);

  const effective = useWsQuery<EffectiveResponse>(effectivePath, {
    enabled: enabled && Boolean(activeId),
  });

  const policyEntries = useMemo(() => policyEntriesFrom(effective.data), [effective.data]);
  const recommendations = useMemo(() => toRecommendations(lessons.data?.items), [lessons.data]);

  const createBrand = useMutation<{ name: string }, BrandDetail>(
    (args) => wsApi.post("/brands", { name: args.name }) as Promise<BrandDetail>,
    {
      onSuccess: (result) => {
        if (result?.brand) setSelectedId(result.brand.id);
        setCreateName("");
        setShowCreate(false);
        list.reload();
      },
    },
  );

  const renameBrand = useMutation<{ id: string; name: string }, BrandDetail>(
    (args) => wsApi.put(`/brands/${args.id}`, { name: args.name }) as Promise<BrandDetail>,
    { onSuccess: () => list.reload() },
  );

  const linkAsset = useMutation<{ brandId: string; mediaAssetId: string; role: string }, unknown>(
    (args) => wsApi.post(`/brands/${args.brandId}/assets`, {
      media_asset_id: args.mediaAssetId,
      asset_role: args.role,
    }),
    { onSuccess: () => list.reload() },
  );

  const unlinkAsset = useMutation<{ brandId: string; mediaAssetId: string; role: string }, unknown>(
    (args) => wsApi.del(
      `/brands/${args.brandId}/assets?media_asset_id=${encodeURIComponent(args.mediaAssetId)}&asset_role=${encodeURIComponent(args.role)}`,
    ),
    { onSuccess: () => list.reload() },
  );

  /* `useMutation` keeps no result, so the verdict is held here. It is a
   * VERIFIER verdict: the screen displays it and never authors it. */
  const [verifyReport, setVerifyReport] = useState<VerifyReport | null>(null);
  const verify = useMutation<{ brandId: string; artifact: unknown }, { report: VerifyReport }>(
    (args) => wsApi.post(`/brands/${args.brandId}/verify`, {
      artifact: args.artifact,
      artifact_kind: "clip",
    }) as Promise<{ report: VerifyReport }>,
    {
      onSuccess: (result) => setVerifyReport(result?.report ?? null),
      onError: () => setVerifyReport(null),
    },
  );

  const brandColumns: Column<BrandDto>[] = [
    { key: "name", header: "Brand", cell: (b) => b.name || b.id.slice(0, 8) },
    {
      key: "default",
      header: "Default",
      cell: (b) => (b.is_default ? <Badge tone="info">default</Badge> : <span className="ym-muted">—</span>),
    },
    { key: "status", header: "Status", cell: (b) => <StatusBadge status={b.status} /> },
    {
      key: "dna",
      header: "DNA fields",
      align: "right",
      cell: (b) => Object.keys(b.dna ?? {}).length,
    },
    {
      key: "assets",
      header: "Linked assets",
      align: "right",
      cell: (b) => ((b.assets?.length ?? 0) > 0 ? (b.assets?.length ?? 0) : <span className="ym-muted">—</span>),
    },
    { key: "updated", header: "Updated", cell: (b) => createdAt(b.updated_at), hideBelow: "md" },
  ];

  if (!workspaceId) {
    return (
      <>
        <PageHeader title="Brands" description="BrandDNA: identity, creative rules, voice and the effective policy." />
        <Panel title="No workspace selected">
          <EmptyState
            title="Nothing to resolve"
            description="BrandDNA is workspace-scoped. Select or create a workspace to load its brands."
          />
        </Panel>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Brands"
        description="BrandDNA documents and the effective creative policy the resolver produces from them."
        actions={<Button onClick={list.reload}>Refresh</Button>}
      />

      <Tabs tabs={TABS} active={tab} onChange={(id) => setTab(id as TabId)} />

      {/* ================= OVERVIEW ================= */}
      {tab === "overview" ? (
        <>
          <Panel
            title="Brands"
            subtitle="GET /brands lists active brands with their DNA payload."
            dense
            actions={
              <Button variant="primary" size="sm" onClick={() => setShowCreate(true)} disabled={createBrand.pending}>
                New brand
              </Button>
            }
          >
            <QueryBoundary query={list} skeletonRows={4}>
              {(d) => (
                <DataTable
                  rows={d.brands ?? []}
                  columns={brandColumns}
                  rowKey={(b) => b.id}
                  caption="Brands"
                  empty="No brand yet"
                  emptyHint="POST /brands with a name, then PUT /brands/{id} with a dna document. The workspace default supplies a baseline policy either way."
                  onRowClick={(b) => setSelectedId(b.id)}
                />
              )}
            </QueryBoundary>
          </Panel>

          <Panel title="Selected brand" dense>
            <QueryBoundary query={list} skeletonRows={2}>
              {() =>
                !active ? (
                  <EmptyState title="No brand selected" description="Create a brand to resolve a policy against it." />
                ) : (
                  <Grid min={180} gap="sm">
                    <StatTile label="Name" value={active.name || "unnamed"} source="GET /brands" />
                    <StatTile
                      label="Status"
                      value={<StatusBadge status={active.status} />}
                      tone={active.status === "active" ? "success" : "neutral"}
                      source="GET /brands"
                    />
                    <StatTile
                      label="Default"
                      value={active.is_default ? "Yes" : "No"}
                      source="GET /brands"
                    />
                    <StatTile
                      label="DNA fields stated"
                      value={Object.keys(active.dna ?? {}).length}
                      source="BrandDNA document"
                    />
                    <StatTile
                      label="Linked assets"
                      value={activeAssets.length ? activeAssets.length : undefined}
                      unavailable={activeAssets.length === 0}
                      hint="MediaAsset references, never bytes"
                      source="GET /brands"
                    />
                    <StatTile
                      label="DNA version"
                      value={effective.data?.dna_version || undefined}
                      unavailable={!effective.data?.dna_version}
                      hint="sha256 of the resolved document"
                      source="GET /brands/effective"
                    />
                  </Grid>
                )
              }
            </QueryBoundary>
          </Panel>

          <Panel title="Creative presets" subtitle="GET /brands/presets — built-in templates an operator can start from." dense>
            <QueryBoundary query={presets} skeletonRows={3}>
              {(d) => (
                <DataTable
                  rows={d.presets ?? []}
                  columns={[
                    { key: "name", header: "Preset", cell: (p) => p.name || p.id },
                    {
                      key: "origin",
                      header: "Origin",
                      cell: (p) => <Badge tone={p.builtin ? "info" : "neutral"}>{p.builtin ? "built-in" : "workspace"}</Badge>,
                    },
                    {
                      key: "keys",
                      header: "Template keys",
                      cell: (p) => (
                        <span style={{ display: "inline-flex", gap: "var(--space-1)", flexWrap: "wrap" }}>
                          {Object.keys(p.preset ?? {}).map((k) => (
                            <Badge key={k} tone="neutral">
                              {k}
                            </Badge>
                          ))}
                        </span>
                      ),
                    },
                  ]}
                  rowKey={(p) => p.id}
                  caption="Creative presets"
                  empty="No preset"
                  emptyHint="Presets are creative templates, not constraints: applying one never changes the effective policy on its own."
                />
              )}
            </QueryBoundary>
          </Panel>

          <Panel
            title="Workspace chrome"
            subtitle="GET /brand — the legacy white-label kit. This is NOT BrandDNA."
            dense
          >
            <QueryBoundary query={chrome} skeletonRows={2}>
              {(d) => {
                const kit = d.brand ?? { app_name: "", accent: "", logo_path: "" };
                return (
                  <Grid min={200} gap="sm">
                    <StatTile label="App name" value={kit.app_name || undefined} unavailable={!kit.app_name} source="GET /brand" />
                    <StatTile label="Accent" value={kit.accent || undefined} unavailable={!kit.accent} source="GET /brand" />
                    <StatTile
                      label="Logo"
                      value={kit.logo_path ? "Uploaded" : undefined}
                      unavailable={!kit.logo_path}
                      hint="Stored on the workspace, not in BrandDNA"
                      source="GET /brand"
                    />
                    <div>
                      {kit.logo_path ? (
                        <WorkspaceLogo workspaceId={workspaceId} />
                      ) : (
                        <p className="ym-hint">No logo uploaded. POST /brand/logo accepts PNG/JPG/WEBP up to 2MB.</p>
                      )}
                    </div>
                  </Grid>
                );
              }}
            </QueryBoundary>
          </Panel>
        </>
      ) : null}

      {/* ================= IDENTITY / CREATIVE / VOICE ================= */}
      {tab === "identity" || tab === "creative" || tab === "voice" ? (
        <Panel title={TABS.find((t) => t.id === tab)?.label} subtitle="BrandDNA document, as stored." dense>
          <QueryBoundary query={list} skeletonRows={6}>
            {() => {
              const section =
                tab === "identity" ? IDENTITY_KEYS : tab === "creative" ? CREATIVE_KEYS : VOICE_KEYS;
              if (!active) return <EmptyDna section={section} />;
              const empty = section.keys.every((k) => {
                const v = active.dna?.[k];
                return v === undefined || v === null || v === "" ||
                  (Array.isArray(v) && v.length === 0) ||
                  (typeof v === "object" && !Array.isArray(v) && Object.keys(v as object).length === 0);
              });
              if (empty) return <EmptyDna section={section} />;
              return <DnaSectionView dna={active.dna ?? {}} section={section} />;
            }}
          </QueryBoundary>
        </Panel>
      ) : null}

      {/* ================= MUSIC ================= */}
      {tab === "music" ? (
        <>
          <Panel title="Music preferences stated by the brand" subtitle="GET /music/prefs reads BrandDNA.music_prefs." dense>
            <QueryBoundary query={musicPrefs} skeletonRows={3}>
              {(d) => {
                const keys = Array.isArray(d.keys) ? d.keys : [];
                const prefs = d.prefs ?? {};
                return (
                  <div>
                    <Grid min={180} gap="sm">
                      <StatTile
                        label="Preferences stated"
                        value={d.stated ? "Yes" : "No"}
                        tone={d.stated ? "info" : "neutral"}
                        hint={d.stated ? undefined : "No preference stated is not an instruction to pick one"}
                        source="GET /music/prefs"
                      />
                      <StatTile
                        label="Recognised keys"
                        value={keys.length}
                        unavailable={keys.length === 0}
                        hint={keys.length === 0 ? "The backend recognised no music_prefs keys" : undefined}
                        source="GET /music/prefs"
                      />
                    </Grid>
                    <p className="ym-hint">{d.note}</p>
                    <DataTable
                      rows={keys.map((k) => ({ key: k, value: toPolicyValue(prefs[k]) }))}
                      columns={[
                        { key: "key", header: "Preference", cell: (r) => <span style={MONO}>{r.key}</span> },
                        { key: "value", header: "Brand value", cell: (r) => <PolicyValueCell value={r.value} /> },
                      ]}
                      rowKey={(r) => r.key}
                      caption="Brand music preferences"
                      empty="No music preference"
                      emptyHint="The brand stated none. An unset preference is never filled in with a default genre."
                    />
                  </div>
                );
              }}
            </QueryBoundary>
          </Panel>

          <Panel title="Workspace music opt-in" subtitle="GET /music/policy — unset is not enabled, and a brand can refuse." dense>
            <QueryBoundary query={musicPolicy} skeletonRows={2}>
              {(d) => (
                <Grid min={200} gap="sm">
                  <StatTile
                    label="Generate"
                    value={d.generate ? "Allowed" : "Not allowed"}
                    tone={d.generate ? "success" : "neutral"}
                    hint={`reason: ${d.reason}`}
                    source="GET /music/policy"
                  />
                  <StatTile
                    label="Brand refusal"
                    value={d.brand_disabled ? "Brand disabled" : "Not disabled"}
                    tone={d.brand_disabled ? "warning" : "neutral"}
                    source="GET /music/policy"
                  />
                </Grid>
              )}
            </QueryBoundary>
          </Panel>
        </>
      ) : null}

      {/* ================= ASSETS ================= */}
      {tab === "assets" ? (
        <>
          <Panel title="Linked brand assets" subtitle="MediaAsset references. Bytes never travel through these routes." dense>
            <QueryBoundary query={list} skeletonRows={3}>
              {() =>
                !active ? (
                  <EmptyState title="No brand selected" description="Link assets once a brand exists." />
                ) : activeAssets.length === 0 ? (
                  <EmptyState
                    title="No linked asset"
                    description="Link a MediaAsset to a role with POST /brands/{id}/assets. A brand with no logo has no approved_logos constraint."
                  />
                ) : (
                  <DataTable
                    rows={activeAssets}
                    columns={[
                      { key: "role", header: "Role", cell: (a) => <Badge tone="info">{humanize(a.asset_role)}</Badge> },
                      { key: "label", header: "Label", cell: (a) => a.label || <span className="ym-muted">—</span> },
                      { key: "ref", header: "Media asset", cell: (a) => <span style={MONO}>{a.media_asset_id.slice(0, 12)}</span> },
                      { key: "created", header: "Linked", cell: (a) => createdAt(a.created_at), hideBelow: "md" },
                      {
                        key: "remove",
                        header: "",
                        align: "right",
                        cell: (a) => (
                          <DestructiveButton
                            confirmLabel={`Unlink ${a.asset_role} ${a.media_asset_id.slice(0, 8)} from ${active.name || active.id.slice(0, 8)}? The MediaAsset itself is not deleted.`}
                            onConfirm={() => void unlinkAsset.run({ brandId: active.id, mediaAssetId: a.media_asset_id, role: a.asset_role })}
                            loading={unlinkAsset.pending}
                          >
                            Unlink
                          </DestructiveButton>
                        ),
                      },
                    ]}
                    rowKey={(a) => a.id}
                    caption="Brand asset links"
                    empty="No linked asset"
                  />
                )
              }
            </QueryBoundary>
          </Panel>

          <Panel title="Link an asset" dense>
            <QueryBoundary query={list} skeletonRows={1}>
              {() =>
                !active ? (
                  <EmptyState title="No brand selected" />
                ) : (
                  <form
                    onSubmit={(e) => {
                      e.preventDefault();
                      if (!linkAssetId.trim()) return;
                      void linkAsset.run({ brandId: active.id, mediaAssetId: linkAssetId.trim(), role: linkRole });
                      setLinkAssetId("");
                    }}
                    style={{ display: "flex", gap: "var(--space-2)", alignItems: "flex-end", flexWrap: "wrap" }}
                  >
                    <Field
                      label="Media asset id"
                      value={linkAssetId}
                      onChange={(e) => setLinkAssetId(e.target.value)}
                      placeholder="MediaAsset id from /assets/media"
                      style={{ flex: "1 1 260px" }}
                    />
                    <Select label="Role" value={linkRole} onChange={(e) => setLinkRole(e.target.value)}>
                      {BRAND_ASSET_ROLES.map((role) => (
                        <option key={role} value={role}>
                          {humanize(role)}
                        </option>
                      ))}
                    </Select>
                    <Button type="submit" variant="primary" loading={linkAsset.pending} disabled={!linkAssetId.trim()}>
                      Link
                    </Button>
                  </form>
                )
              }
            </QueryBoundary>
            {linkAsset.error ? (
              <p className="ym-error" role="alert">
                The backend refused the link: {linkAsset.error}
              </p>
            ) : null}
          </Panel>
        </>
      ) : null}

      {/* ================= POLICIES ================= */}
      {tab === "policies" ? (
        <>
          <Panel
            title="Hard constraints"
            subtitle="GET /brands/effective — hard_constraints. These are what every consumer must honour."
            dense
          >
            <QueryBoundary query={effective} skeletonRows={5}>
              {() => (
                <PolicyTable
                  entries={policyEntries.filter((e) => e.hard)}
                  caption="Hard BrandDNA constraints"
                />
              )}
            </QueryBoundary>
          </Panel>

          <Panel
            title="Recommendations from measured learning"
            subtitle="GET /lessons. Advisory. Never a constraint."
            dense
          >
            <QueryBoundary query={lessons} skeletonRows={4}>
              {() => <RecommendationList items={recommendations} />}
            </QueryBoundary>
          </Panel>

          <Panel title="Verify an artifact" subtitle="POST /brands/{id}/verify — the resolver and verifier, not the UI, decide." dense>
            <QueryBoundary query={list} skeletonRows={1}>
              {() =>
                !active ? (
                  <EmptyState title="No brand selected" />
                ) : (
                  <form
                    onSubmit={(e) => {
                      e.preventDefault();
                      let artifact: unknown;
                      try {
                        artifact = JSON.parse(verifyArtifact);
                      } catch {
                        artifact = { text: verifyArtifact };
                      }
                      void verify.run({ brandId: active.id, artifact });
                    }}
                    style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}
                  >
                    <Textarea
                      label="Artifact (JSON)"
                      rows={4}
                      value={verifyArtifact}
                      onChange={(e) => setVerifyArtifact(e.target.value)}
                    />
                    <p className="ym-hint">
                      The verifier checks the artifact against the resolved policy. It reports; it never edits.
                    </p>
                    <div>
                      <Button type="submit" variant="primary" loading={verify.pending}>
                        Verify
                      </Button>
                    </div>
                  </form>
                )
              }
            </QueryBoundary>
            {verify.error ? (
              <p className="ym-error" role="alert">
                The verifier refused the request: {verify.error}
              </p>
            ) : null}
            {verifyReport ? (
              <div data-verify-report={verifyReport.status}>
                <Grid min={170} gap="sm">
                  <StatTile
                    label="Verdict"
                    value={verifyReport.status}
                    tone={verifyReport.status === "PASS" ? "success" : verifyReport.status === "FAIL" ? "danger" : "warning"}
                    source="POST /brands/{id}/verify"
                  />
                  <StatTile
                    label="Authoritative"
                    value={verifyReport.authoritative ? "Yes" : "No"}
                    hint="Set by the verifier, not by this screen"
                    source="POST /brands/{id}/verify"
                  />
                  <StatTile
                    label="Effective config"
                    value={verifyReport.effective_config_id || undefined}
                    unavailable={!verifyReport.effective_config_id}
                    source="POST /brands/{id}/verify"
                  />
                </Grid>
              </div>
            ) : null}
          </Panel>
        </>
      ) : null}

      {/* ================= EFFECTIVE POLICY ================= */}
      {tab === "effective" ? (
        <>
          <Panel title="Resolution inputs" dense>
            <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "flex-end", flexWrap: "wrap" }}>
              <Select
                label="Platform fragment"
                value={platform}
                onChange={(e) => setPlatform(e.target.value)}
              >
                <option value="">No platform</option>
                {["youtube", "tiktok", "instagram", "twitter", "facebook"].map((p) => (
                  <option key={p} value={p}>
                    {humanize(p)}
                  </option>
                ))}
              </Select>
              <p className="ym-hint">
                A platform fragment is applied LAST by the resolver (BrandDNA.platform_overrides).
              </p>
              {!activeId ? (
                <EmptyState title="No brand to resolve" description="Create a brand first; the resolver needs a subject." />
              ) : null}
            </div>
          </Panel>

          <Panel title="Effective policy" subtitle="Everything the resolver produced, with the level that set each field." dense>
            <QueryBoundary query={effective} skeletonRows={8}>
              {() => (
                <div>
                  <Grid min={180} gap="sm">
                    <StatTile
                      label="Brand"
                      value={effective.data?.policy.brand_id ? effective.data.policy.brand_id.slice(0, 8) : undefined}
                      unavailable={!effective.data?.policy.brand_id}
                      source="GET /brands/effective"
                    />
                    <StatTile
                      label="DNA version"
                      value={effective.data?.dna_version || undefined}
                      unavailable={!effective.data?.dna_version}
                      source="GET /brands/effective"
                    />
                    <StatTile
                      label="Effective config"
                      value={effective.data?.effective_config_id || undefined}
                      unavailable={!effective.data?.effective_config_id}
                      source="GET /brands/effective"
                    />
                    <StatTile
                      label="Platform"
                      value={effective.data?.policy.platform ?? undefined}
                      unavailable={!effective.data?.policy.platform}
                      hint={effective.data?.policy.platform ? undefined : "No platform fragment applied"}
                      source="GET /brands/effective"
                    />
                    <StatTile
                      label="Hard constraints set"
                      value={policyEntries.filter((e) => e.hard && e.value.kind !== "none").length}
                      unavailable={!effective.data}
                      hint="Hard fields with a stated value"
                      source="GET /brands/effective"
                    />
                    <StatTile
                      label="Recommendations applied"
                      value="None"
                      hint="A recommendation is never written into the policy."
                      tone="handoff"
                      source="GET /lessons"
                    />
                  </Grid>
                  <PolicyTable entries={policyEntries} caption="Effective creative policy" />
                </div>
              )}
            </QueryBoundary>
          </Panel>
        </>
      ) : null}

      {/* ---- create brand ------------------------------------------------- */}
      <Modal
        open={showCreate}
        onClose={() => setShowCreate(false)}
        title="New brand"
        footer={
          <>
            <Button variant="ghost" onClick={() => setShowCreate(false)}>
              Cancel
            </Button>
            <Button
              variant="primary"
              loading={createBrand.pending}
              disabled={!createName.trim()}
              onClick={() => void createBrand.run({ name: createName.trim() })}
            >
              Create
            </Button>
          </>
        }
      >
        <Field
          label="Brand name"
          value={createName}
          onChange={(e) => setCreateName(e.target.value)}
          hint="POST /brands creates an empty BrandDNA. Editing the document is PUT /brands/{id} with a full dna object."
        />
        {createBrand.error ? (
          <p className="ym-error" role="alert">
            The backend refused the create: {createBrand.error}
          </p>
        ) : null}
      </Modal>

      {/* ---- rename ------------------------------------------------------- */}
      {active ? (
        <RenameControl
          key={active.id}
          brand={active}
          pending={renameBrand.pending}
          error={renameBrand.error}
          onRename={(name) => void renameBrand.run({ id: active.id, name })}
        />
      ) : null}
    </>
  );
}

/* ==========================================================================
 * Small pieces kept out of the main tree
 * ======================================================================= */

function RenameControl({
  brand,
  pending,
  error,
  onRename,
}: {
  brand: BrandDto;
  pending: boolean;
  error: string | null;
  onRename: (name: string) => void;
}) {
  const [name, setName] = useState(brand.name);
  return (
    <Panel title="Rename brand" dense>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (name.trim()) onRename(name.trim());
        }}
        style={{ display: "flex", gap: "var(--space-2)", alignItems: "flex-end", flexWrap: "wrap" }}
      >
        <Field label="Name" value={name} onChange={(e) => setName(e.target.value)} style={{ flex: "1 1 220px" }} />
        <Button type="submit" variant="primary" loading={pending} disabled={!name.trim() || name.trim() === brand.name}>
          Save name
        </Button>
      </form>
      {error ? (
        <p className="ym-error" role="alert">
          The backend refused the rename: {error}
        </p>
      ) : null}
    </Panel>
  );
}

function WorkspaceLogo({ workspaceId }: { workspaceId: string }) {
  const [loaded, setLoaded] = useState<{
    workspaceId: string;
    url: string | null;
    failed: boolean;
  } | null>(null);

  useEffect(() => {
    let active = true;
    let objectUrl: string | null = null;
    void Promise.resolve()
      .then(() => fetchWorkspaceLogo(workspaceId))
      .then((blob) => {
        if (!active) return;
        objectUrl = URL.createObjectURL(blob);
        setLoaded({ workspaceId, url: objectUrl, failed: false });
      })
      .catch(() => {
        if (active) setLoaded({ workspaceId, url: null, failed: true });
      });

    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [workspaceId]);

  if (loaded?.workspaceId !== workspaceId) {
    return <p className="ym-muted" role="status">Loading workspace logo…</p>;
  }
  if (loaded.failed || !loaded.url) {
    return <p className="ym-error" role="status">Workspace logo unavailable.</p>;
  }
  return (
    <img
      src={loaded.url}
      alt="Workspace logo"
      style={{ maxWidth: "100%", maxHeight: 120 }}
    />
  );
}

export { PolicyTable, RecommendationList };

export default Brands;
