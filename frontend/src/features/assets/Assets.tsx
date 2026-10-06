/* Assets — the real media library, over the real asset endpoints.
 *
 * ENDPOINTS (all workspace-scoped, all typed from the backend router that owns
 * them; nothing here is guessed):
 *
 *   GET  /assets/media?limit=&type=   api/v1/content.py `assets_router.list_media`
 *                                      dto `_media_dto` (line 1303) — the
 *                                      canonical MediaAsset reference rows.
 *   GET  /assets                       api/v1/content.py `assets_router.list_assets`
 *                                      (line 1225) — rendered videos + operator
 *                                      uploads, i.e. the things a render produced.
 *   GET  /avatars                      api/v1/ugc.py `avatars_router.list_avatars`
 *                                      (line 320), dto `profile_dto`
 *                                      (engine/avatar/service.py:325).
 *   GET  /music/policy                 api/v1/music.py `get_music_policy` (line 131).
 *   POST /assets/broll/search          api/v1/content.py `broll_search` (line 1858).
 *
 * "KIND" IS A VIEW, NOT A BACKEND FIELD.
 *
 * `MediaAsset` has exactly two vocabularies — `ASSET_TYPES` and `ASSET_ORIGINS`
 * (backend/app/models/assets.py:18). The screen's kinds (video, image, audio,
 * voice, music, generated, source, avatars) are therefore expressed as a filter
 * over those two vocabularies plus, for avatars, a different endpoint. Nothing
 * here invents a `type` the backend would reject with a 422.
 *
 * GENERATED vs SOURCE IS `origin`, NOT A GUESS.
 *
 * `origin` is written by the pipeline: `generated`/`render` for something this
 * system made, everything else (upload, stock, import, record, proxy) for
 * something it received. That distinction is read, never inferred.
 *
 * PROVENANCE IS NEVER RENDERED RAW.
 *
 * A provenance value (a provider download URL, a storage key, a stock `preview`
 * or `page_url`) can carry a credential: the backend itself refuses to cache
 * signed URLs and lists the exact query keys that mark one
 * (backend/app/services/media_cache.py `_SIGNED_QUERY_KEYS`, and
 * `SIGNED_URL_PROVIDERS = {"coverr"}`). `redactProvenance` mirrors that key set
 * and strips the whole query string plus any userinfo whenever one of them is
 * present. The rendered cell therefore shows host + path (or `…redacted`) and
 * the secret never reaches the DOM.
 */

import { useMemo, useState, type FormEvent, type ReactNode } from "react";
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
  Textarea,
  humanize,
  type Column,
  type Tone,
} from "../../design-system/primitives";
import { useMutation, useWsQuery } from "../../api/queries";
import { mediaFileUrl, videoThumbUrl, wsApi } from "../../lib/api";
import { useSession } from "../../state/session";

/* ==========================================================================
 * Response shapes — typed from the backend routers named above
 * ======================================================================= */

/** `assets_router._media_dto` — backend/app/api/v1/content.py:1303. */
export type MediaAsset = {
  id: string;
  workspace_id: string;
  /** ASSET_TYPES: video|audio|image|subtitle|generated_image|generated_video|voice|avatar|thumbnail|other */
  type: string;
  /** ASSET_ORIGINS: upload|render|stock|generated|import|record|proxy */
  origin: string;
  provider: string;
  storage_key: string;
  mime_type: string;
  duration_seconds: number | null;
  width: number | null;
  height: number | null;
  checksum: string;
  created_at: string;
};
export type MediaList = { total: number; items: MediaAsset[] };

/** `assets_router.list_assets` — backend/app/api/v1/content.py:1250. */
export type RenderAsset = {
  id: string;
  type: string;
  title: string;
  engine: string;
  status: string;
  aspect_ratio: string | null;
  resolution: string | null;
  size_bytes: number | null;
  is_mock: boolean;
  variant_label: string;
  video_id: string | null;
  created_at: string;
};
export type RenderList = {
  items?: RenderAsset[];
  capabilities: { upload: boolean; note: string };
};

/** `profile_dto` — backend/app/engine/avatar/service.py:325. */
export type AvatarProfile = {
  id: string;
  workspace_id: string;
  name: string | null;
  profile: Record<string, unknown>;
  source_asset_ref: string | null;
  consent_state: string;
  consent: Record<string, unknown>;
  provider: string;
  status: string;
  created_at: string;
};
export type AvatarList = { total: number; items: AvatarProfile[] };

/** `get_music_policy` payload — backend/app/api/v1/music.py:112. */
export type MusicPolicy = {
  workspace_id: string;
  generate: boolean;
  reason: string;
  brand_disabled: boolean;
  provider_key: string;
  configured: boolean;
  forbidden_genres: string[];
  prefs: Record<string, unknown>;
};

/** `broll_search` response — backend/app/api/v1/content.py:1866. */
export type StockHit = {
  video_id: string;
  preview: string;
  duration: number | null;
  author: string;
  page_url: string;
};

/* ==========================================================================
 * Provenance redaction
 *
 * The key set is the backend's own (media_cache._SIGNED_QUERY_KEYS). It is
 * duplicated, not imported, because the frontend must not import backend Python;
 * the test pins it so a backend change that adds a key is a visible diff.
 * ======================================================================= */

export const SIGNED_QUERY_KEYS: readonly string[] = [
  "amz-signature", "x-amz-signature", "x-amz-credential", "x-amz-algorithm",
  "x-goog-signature", "x-goog-credential", "gcp-signature", "signature",
  "sig", "token", "access_token", "id_token", "jwt", "auth", "authorization",
  "key", "apikey", "api_key", "policy", "expires", "se", "st", "sk", "sp",
  "hdnts", "x-amz-date", "x-amz-expires", "download_token", "stoken",
];

export type Redaction = {
  /** What may be rendered: host + path, or `path…redacted`. */
  display: string;
  /** Query parameter names that were removed. Empty when nothing was signed. */
  removedKeys: string[];
  /** True when at least one credential-bearing part was stripped. */
  redacted: boolean;
};

const SIGNED = new Set(SIGNED_QUERY_KEYS.map((k) => k.toLowerCase()));
const REDACTED_MARK = "…redacted";

/** True when this value carries (or may carry) a credential. */
export function isSignedProvenance(raw: string | null | undefined): boolean {
  const text = String(raw ?? "").trim();
  if (!text) return false;
  // userinfo is a credential in any scheme
  const schemeSplit = text.indexOf("://");
  if (schemeSplit >= 0) {
    const rest = text.slice(schemeSplit + 3);
    const at = rest.indexOf("@");
    const firstSep = rest.search(/[/?#]/);
    if (at >= 0 && (firstSep < 0 || at < firstSep)) return true;
  }
  const query = text.includes("?") ? text.slice(text.indexOf("?") + 1).split("#")[0] : "";
  return query
    .split("&")
    .map((pair) => pair.split("=")[0]?.trim().toLowerCase() ?? "")
    .some((key) => key.length > 0 && SIGNED.has(key));
}

/**
 * Host + path only, and never a query string when one of its keys is a
 * credential. Nothing else is stripped: hiding the path of a public asset makes
 * provenance unauditable, which is the opposite of what this screen is for.
 */
export function redactProvenance(raw: string | null | undefined): Redaction {
  const text = String(raw ?? "").trim();
  if (!text) return { display: "—", removedKeys: [], redacted: false };

  const schemeAt = text.indexOf("://");
  const scheme = schemeAt >= 0 ? text.slice(0, schemeAt + 3) : "";
  const body = schemeAt >= 0 ? text.slice(schemeAt + 3) : text;

  const queryAt = body.indexOf("?");
  const beforeQuery = queryAt >= 0 ? body.slice(0, queryAt) : body;
  const rawQuery = queryAt >= 0 ? body.slice(queryAt + 1).split("#")[0] : "";

  const removedKeys = rawQuery
    .split("&")
    .map((pair) => pair.split("=")[0] ?? "")
    .map((k) => k.trim())
    .filter((k) => k.length > 0 && SIGNED.has(k.toLowerCase()));

  // userinfo is dropped unconditionally: `https://user:pw@host/x` is a leak.
  const at = beforeQuery.indexOf("@");
  const firstSep = beforeQuery.search(/[/?#]/);
  const hadUserinfo = at >= 0 && (firstSep < 0 || at < firstSep);
  const cleanHostPath = hadUserinfo
    ? beforeQuery.slice(at + 1)
    : beforeQuery;

  if (removedKeys.length === 0 && !hadUserinfo) {
    // Nothing was credential-bearing: show the value the backend actually sent,
    // byte for byte. Rebuilding it here would silently truncate a plain query.
    return { display: text, removedKeys: [], redacted: false };
  }
  return {
    display: `${scheme}${cleanHostPath}${REDACTED_MARK}`,
    removedKeys: Array.from(new Set(removedKeys)),
    redacted: true,
  };
}

/* ==========================================================================
 * Kinds — a view over ASSET_TYPES / ASSET_ORIGINS
 * ======================================================================= */

export type AssetKind = "all" | "video" | "image" | "audio" | "voice" | "music" | "generated" | "source" | "avatars";

type KindSpec = {
  id: AssetKind;
  label: string;
  /** MediaAsset.type values this kind includes. */
  types: string[];
  hint: string;
};

const KINDS: KindSpec[] = [
  { id: "all", label: "All", types: [], hint: "Every registered media asset." },
  { id: "video", label: "Video", types: ["video"], hint: "ASSET_TYPES.video" },
  { id: "image", label: "Image", types: ["image", "thumbnail"], hint: "ASSET_TYPES.image + thumbnail" },
  { id: "audio", label: "Audio", types: ["audio"], hint: "ASSET_TYPES.audio" },
  { id: "voice", label: "Voice", types: ["voice"], hint: "ASSET_TYPES.voice" },
  { id: "music", label: "Music", types: ["audio"], hint: "Audio assets + the workspace music policy" },
  { id: "generated", label: "Generated", types: ["generated_image", "generated_video"], hint: "origin = generated | render" },
  { id: "source", label: "Source", types: [], hint: "origin = upload | stock | import | record | proxy" },
  { id: "avatars", label: "Avatars", types: ["avatar"], hint: "GET /avatars profiles + ASSET_TYPES.avatar" },
];

/** `origin` values this system produced, per ASSET_ORIGINS. */
const GENERATED_ORIGINS = new Set(["generated", "render"]);
/** `origin` values this system received. */
const SOURCE_ORIGINS = new Set(["upload", "stock", "import", "record", "proxy"]);

export function isGeneratedAsset(asset: MediaAsset): boolean {
  return GENERATED_ORIGINS.has(asset.origin.toLowerCase());
}
export function isSourceAsset(asset: MediaAsset): boolean {
  return SOURCE_ORIGINS.has(asset.origin.toLowerCase());
}

export function matchesKind(asset: MediaAsset, kind: AssetKind): boolean {
  const spec = KINDS.find((k) => k.id === kind);
  if (!spec || kind === "all") return true;
  if (kind === "generated") return isGeneratedAsset(asset) || spec.types.includes(asset.type);
  if (kind === "source") return isSourceAsset(asset);
  return spec.types.includes(asset.type);
}

/** The music view has no asset type of its own; audio carries it. */
function matchesMusic(asset: MediaAsset): boolean {
  return asset.type === "audio";
}

export function kindLabel(kind: AssetKind): string {
  return KINDS.find((k) => k.id === kind)?.label ?? humanize(kind);
}

/* ==========================================================================
 * Small formatters
 * ======================================================================= */

function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  const s = Math.max(0, seconds);
  const m = Math.floor(s / 60);
  return `${String(m).padStart(2, "0")}:${(s % 60).toFixed(1).padStart(4, "0")}`;
}

function dimensions(asset: MediaAsset): ReactNode {
  if (asset.width === null || asset.height === null) {
    return <span className="ym-muted">not reported</span>;
  }
  return `${asset.width}×${asset.height}`;
}

function bytes(size: number | null): ReactNode {
  if (size === null) return <span className="ym-muted">not reported</span>;
  const mb = size / (1024 * 1024);
  return mb >= 1 ? `${mb.toFixed(1)} MB` : `${Math.max(1, Math.round(size / 1024))} KB`;
}

function originTone(origin: string): Tone {
  const o = origin.toUpperCase();
  if (GENERATED_ORIGINS.has(o.toLowerCase())) return "info";
  if (o === "STOCK" || o === "IMPORT") return "neutral";
  return "warning";
}

function createdAt(iso: string): string {
  return iso ? iso.replace("Z", " UTC").replace("T", " ").slice(0, 16) : "—";
}

type View = "grid" | "list";

/* ==========================================================================
 * Component
 * ======================================================================= */

export function Assets() {
  const { workspaceId } = useSession();
  const enabled = Boolean(workspaceId);

  const [kind, setKind] = useState<AssetKind>("all");
  const [search, setSearch] = useState("");
  const [appliedSearch, setAppliedSearch] = useState("");
  const [originFilter, setOriginFilter] = useState("");
  const [providerFilter, setProviderFilter] = useState("");
  const [view, setView] = useState<View>("list");
  const [selected, setSelected] = useState<MediaAsset | null>(null);
  const [stockQuery, setStockQuery] = useState("");

  /* ---- reads ------------------------------------------------------------
   * `list_media` accepts only `type` and `limit`, so every other filter here
   * runs over the returned page. That is stated in the UI rather than hidden:
   * a client filter that looks server-side is how an operator misses a row. */
  const media = useWsQuery<MediaList>("/assets/media?limit=200", { enabled });
  const renders = useWsQuery<RenderList>("/assets", { enabled });
  const avatars = useWsQuery<AvatarList>("/avatars", { enabled });
  const music = useWsQuery<MusicPolicy>("/music/policy", { enabled });

  const [stockHits, setStockHits] = useState<StockHit[] | null>(null);
  const stock = useMutation<{ query: string }, StockHit[]>(async (args) => {
    const res = (await wsApi.post("/assets/broll/search", {
      query: args.query,
      per_page: 12,
    })) as { items?: StockHit[] };
    return Array.isArray(res?.items) ? res.items : [];
  }, {
    onSuccess: (result) => setStockHits(result),
    onError: () => setStockHits(null),
  });

  const rows = useMemo(() => media.data?.items ?? null, [media.data]);

  const origins = useMemo(() => {
    const set = new Set<string>();
    for (const a of rows ?? []) if (a.origin) set.add(a.origin);
    return [...set].sort();
  }, [rows]);

  const providers = useMemo(() => {
    const set = new Set<string>();
    for (const a of rows ?? []) if (a.provider) set.add(a.provider);
    return [...set].sort();
  }, [rows]);

  const visible = useMemo(() => {
    if (!rows) return null;
    const needle = appliedSearch.trim().toLowerCase();
    return rows.filter((asset) => {
      // `avatars` has its own endpoint and its own panel below; `music` is
      // audio plus the workspace music policy.
      if (kind === "avatars") return false;
      if (kind === "music" ? !matchesMusic(asset) : !matchesKind(asset, kind)) return false;
      if (originFilter && asset.origin !== originFilter) return false;
      if (providerFilter && asset.provider !== providerFilter) return false;
      if (!needle) return true;
      // Only fields the backend actually returned are searchable. Searching the
      // storage key would be searching a path nobody typed.
      return [asset.type, asset.origin, asset.provider, asset.mime_type, asset.checksum]
        .join(" ")
        .toLowerCase()
        .includes(needle);
    });
  }, [rows, kind, originFilter, providerFilter, appliedSearch]);

  const stats = useMemo(() => {
    if (!rows) return null;
    return {
      total: rows.length,
      generated: rows.filter(isGeneratedAsset).length,
      source: rows.filter(isSourceAsset).length,
      video: rows.filter((a) => a.type === "video").length,
      audio: rows.filter((a) => a.type === "audio").length,
      voice: rows.filter((a) => a.type === "voice").length,
      image: rows.filter((a) => a.type === "image" || a.type === "thumbnail").length,
      withDimensions: rows.filter((a) => a.width !== null && a.height !== null).length,
      withDuration: rows.filter((a) => a.duration_seconds !== null).length,
      providers: new Set(rows.map((a) => a.provider).filter(Boolean)).size,
    };
  }, [rows]);

  const filtersActive = Boolean(appliedSearch || originFilter || providerFilter || kind !== "all");

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    setAppliedSearch(search.trim());
  };

  const clearFilters = () => {
    setSearch("");
    setAppliedSearch("");
    setOriginFilter("");
    setProviderFilter("");
    setKind("all");
  };

  const runStockSearch = (e: FormEvent) => {
    e.preventDefault();
    const q = stockQuery.trim();
    if (!q) return;
    void stock.run({ query: q });
  };

  const columns: Column<MediaAsset>[] = [
    {
      key: "asset",
      header: "Asset",
      cell: (a) => (
        <span>
          <Badge tone="neutral">{humanize(a.type)}</Badge>{" "}
          <span style={MONO}>{a.id.slice(0, 8)}</span>
        </span>
      ),
    },
    {
      key: "origin",
      header: "Origin",
      cell: (a) => <Badge tone={originTone(a.origin)}>{humanize(a.origin)}</Badge>,
    },
    {
      key: "made",
      header: "Produced by",
      cell: (a) =>
        isGeneratedAsset(a) ? (
          <Badge tone="info" title="This workspace produced it">Generated</Badge>
        ) : (
          <Badge tone="neutral" title="This workspace received it">Source</Badge>
        ),
    },
    {
      key: "provider",
      header: "Provider",
      cell: (a) => (a.provider ? a.provider : <span className="ym-muted">not reported</span>),
    },
    {
      key: "dimensions",
      header: "Dimensions",
      align: "right",
      cell: dimensions,
      hideBelow: "md",
    },
    {
      key: "duration",
      header: "Duration",
      align: "right",
      cell: (a) => duration(a.duration_seconds),
      hideBelow: "sm",
    },
    {
      key: "provenance",
      header: "Provenance",
      cell: (a) => <AssetProvenance asset={a} />,
      hideBelow: "lg",
    },
    { key: "created", header: "Registered", cell: (a) => createdAt(a.created_at), hideBelow: "lg" },
  ];

  if (!workspaceId) {
    return (
      <>
        <PageHeader title="Assets" description="Every byte this workspace holds, with its provenance." />
        <Panel title="No workspace selected">
          <EmptyState
            title="Nothing to list"
            description="Assets are workspace-scoped. Select or create a workspace to load its library."
          />
        </Panel>
      </>
    );
  }

  const activeKind = KINDS.find((k) => k.id === kind) ?? KINDS[0];

  return (
    <>
      <PageHeader
        title="Assets"
        description="Registered media references, rendered videos and avatar profiles — each with the origin that says who made it."
        actions={<Button onClick={() => { media.reload(); renders.reload(); avatars.reload(); music.reload(); }}>Refresh</Button>}
      />

      {/* ---- kinds -------------------------------------------------------- */}
      <Panel title="Kinds" dense>
        <div className="ym-grid ym-grid--sm">
          {KINDS.map((k) => (
            <Button
              key={k.id}
              size="sm"
              variant={k.id === kind ? "primary" : "secondary"}
              aria-pressed={k.id === kind}
              onClick={() => setKind(k.id)}
              title={k.hint}
            >
              {k.label}
            </Button>
          ))}
        </div>
        <p className="ym-hint">{activeKind.hint}</p>
      </Panel>

      {/* ---- filters ------------------------------------------------------ */}
      <Panel title="Find an asset" dense>
        <form className="ym-grid ym-grid--md" onSubmit={onSubmit} style={{ gridTemplateColumns: "minmax(220px, 2fr) minmax(150px, 1fr) minmax(150px, 1fr) auto" }}>
          <Field
            label="Search"
            type="search"
            placeholder="type, origin, provider, mime, checksum"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            hint="Filtering runs over the loaded page: GET /assets/media accepts only type and limit."
          />
          <Select label="Origin" value={originFilter} onChange={(e) => setOriginFilter(e.target.value)}>
            <option value="">Any origin</option>
            {origins.map((o) => (
              <option key={o} value={o}>
                {humanize(o)}
              </option>
            ))}
          </Select>
          <Select label="Provider" value={providerFilter} onChange={(e) => setProviderFilter(e.target.value)}>
            <option value="">Any provider</option>
            {providers.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </Select>
          <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "flex-end" }}>
            <Button type="submit" variant="primary">
              Search
            </Button>
            <Button variant="ghost" onClick={clearFilters} disabled={!filtersActive}>
              Clear
            </Button>
          </div>
        </form>
      </Panel>

      {/* ---- counts ------------------------------------------------------- */}
      <Panel title="Library at a glance" dense>
        <Grid min={170} gap="sm">
          <StatTile
            label="Registered media"
            value={stats?.total}
            unavailable={stats === null}
            hint={media.data ? `${media.data.total} reported by the backend` : undefined}
            source="GET /assets/media"
          />
          <StatTile
            label="Generated here"
            value={stats?.generated}
            unavailable={stats === null}
            hint="origin = generated | render"
            source="GET /assets/media"
          />
          <StatTile
            label="Source media"
            value={stats?.source}
            unavailable={stats === null}
            hint="origin = upload | stock | import | record | proxy"
            source="GET /assets/media"
          />
          <StatTile
            label="Distinct providers"
            value={stats?.providers}
            unavailable={stats === null}
            source="GET /assets/media"
          />
          <StatTile
            label="With dimensions"
            value={stats?.withDimensions}
            unavailable={stats === null}
            hint="Width and height are both reported — an unset column is not a 0×0 clip"
            source="GET /assets/media"
          />
          <StatTile
            label="With duration"
            value={stats?.withDuration}
            unavailable={stats === null}
            hint="duration_seconds is nullable; audio and images usually are not"
            source="GET /assets/media"
          />
          <StatTile
            label="Rendered videos"
            value={Array.isArray(renders.data?.items) ? renders.data.items.length : undefined}
            unavailable={!Array.isArray(renders.data?.items)}
            source="GET /assets"
          />
          <StatTile
            label="Avatar profiles"
            value={typeof avatars.data?.total === "number" ? avatars.data.total : undefined}
            unavailable={typeof avatars.data?.total !== "number"}
            source="GET /avatars"
          />
        </Grid>
      </Panel>

      {/* ---- the library -------------------------------------------------- */}
      <Panel
        title={kind === "avatars" ? "Avatar profiles" : "Media assets"}
        subtitle={
          kind === "avatars"
            ? "Consent-gated profiles. A profile is not a render."
            : filtersActive
              ? "Filtered over the loaded page."
              : "Newest first."
        }
        dense
        actions={
          <div role="group" aria-label="View mode" style={{ display: "flex", gap: "var(--space-1)" }}>
            <Button size="sm" variant={view === "list" ? "primary" : "ghost"} aria-pressed={view === "list"} onClick={() => setView("list")}>
              List
            </Button>
            <Button size="sm" variant={view === "grid" ? "primary" : "ghost"} aria-pressed={view === "grid"} onClick={() => setView("grid")}>
              Grid
            </Button>
          </div>
        }
      >
        {kind === "avatars" ? (
          <QueryBoundary query={avatars} skeletonRows={5}>
            {(d) => (
              <DataTable
                rows={d.items ?? []}
                columns={[
                  { key: "name", header: "Profile", cell: (a) => a.name || a.id.slice(0, 8) },
                  { key: "consent", header: "Consent", cell: (a) => <Badge tone={a.consent_state === "authorized" ? "success" : "warning"}>{humanize(a.consent_state)}</Badge> },
                  { key: "status", header: "Status", cell: (a) => <Badge tone="neutral">{humanize(a.status)}</Badge> },
                  { key: "provider", header: "Provider", cell: (a) => a.provider || <span className="ym-muted">not reported</span> },
                  { key: "source", header: "Source ref", cell: (a) => a.source_asset_ref || <span className="ym-muted">none</span>, hideBelow: "md" },
                  { key: "created", header: "Created", cell: (a) => createdAt(a.created_at), hideBelow: "lg" },
                ]}
                rowKey={(a) => a.id}
                caption="Avatar profiles"
                empty="No avatar profile"
                emptyHint="Create one with POST /avatars; consent starts pending and rendering is refused until it is authorized."
              />
            )}
          </QueryBoundary>
        ) : (
          <QueryBoundary query={media} skeletonRows={8}>
            {() =>
              visible === null || visible.length === 0 ? (
                <EmptyState
                  title={filtersActive ? "No asset matches this view" : "No media asset registered"}
                  description={
                    filtersActive
                      ? "The backend returned rows, none of which match these filters. Clear them to see the whole page."
                      : "An asset appears here once something registers a MediaAsset reference — a render, an upload, a stock fetch or a voice preview."
                  }
                />
              ) : view === "grid" ? (
                <Grid min={200} gap="sm">
                  {visible.map((asset) => (
                    <AssetCard key={asset.id} asset={asset} onOpen={() => setSelected(asset)} />
                  ))}
                </Grid>
              ) : (
                <DataTable
                  rows={visible}
                  columns={columns}
                  rowKey={(a) => a.id}
                  caption="Media assets"
                  onRowClick={(a) => setSelected(a)}
                  maxHeight={640}
                  empty="No media asset"
                />
              )
            }
          </QueryBoundary>
        )}
      </Panel>

      {/* ---- rendered output ---------------------------------------------- */}
      <Panel title="Rendered videos and uploads" subtitle="What the render engine actually produced." dense>
        <QueryBoundary query={renders} skeletonRows={4}>
          {(d) => (
            <DataTable
              rows={d.items ?? []}
              columns={[
                { key: "title", header: "Title", cell: (r) => r.title || r.id },
                { key: "status", header: "Status", cell: (r) => <Badge tone={r.is_mock ? "mock" : "neutral"}>{humanize(r.status)}</Badge> },
                { key: "engine", header: "Engine", cell: (r) => r.engine || <span className="ym-muted">—</span> },
                { key: "aspect", header: "Aspect", cell: (r) => r.aspect_ratio || <span className="ym-muted">not reported</span> },
                { key: "resolution", header: "Resolution", cell: (r) => r.resolution || <span className="ym-muted">not reported</span>, hideBelow: "md" },
                { key: "size", header: "Size", align: "right", cell: (r) => bytes(r.size_bytes), hideBelow: "md" },
                { key: "created", header: "Created", cell: (r) => createdAt(r.created_at), hideBelow: "lg" },
              ]}
              rowKey={(r) => r.id}
              caption="Rendered videos"
              empty="No render yet"
              emptyHint={d.capabilities?.note}
              maxHeight={420}
            />
          )}
        </QueryBoundary>
      </Panel>

      {/* ---- music policy -------------------------------------------------- */}
      <Panel title="Music" subtitle="Generation is opt-in and a brand can refuse it." dense>
        <QueryBoundary query={music} skeletonRows={3}>
          {(d) => {
            /* Read defensively: a 200 that omitted a key is "not reported",
             * never a crash and never a zero. */
            const forbidden = Array.isArray(d.forbidden_genres) ? d.forbidden_genres : [];
            const provider = typeof d.provider_key === "string" ? d.provider_key : "";
            return (
              <Grid min={180} gap="sm">
                <StatTile
                  label="Generate music"
                  value={d.generate ? "Allowed" : "Not allowed"}
                  tone={d.generate ? "success" : "neutral"}
                  hint={d.reason ? `reason: ${d.reason}` : undefined}
                  source="GET /music/policy"
                />
                <StatTile
                  label="Provider"
                  value={provider || undefined}
                  unavailable={!provider}
                  hint={d.brand_disabled ? "Brand DNA refuses music generation" : undefined}
                  tone={d.brand_disabled ? "warning" : "neutral"}
                  source="GET /music/policy"
                />
                <StatTile
                  label="Forbidden genres"
                  value={forbidden.length}
                  unavailable={forbidden.length === 0}
                  hint={forbidden.length === 0 ? "The brand stated none" : forbidden.join(", ")}
                  source="GET /music/policy"
                />
              </Grid>
            );
          }}
        </QueryBoundary>
      </Panel>

      {/* ---- source media: where a signed URL actually shows up ------------- */}
      <Panel title="Source media" subtitle="Stock catalogue search. Provider URLs are redacted before render." dense>
        <form onSubmit={runStockSearch} style={{ display: "flex", gap: "var(--space-2)", alignItems: "flex-end", flexWrap: "wrap" }}>
          <Field
            label="Stock search"
            type="search"
            placeholder="e.g. city night traffic"
            value={stockQuery}
            onChange={(e) => setStockQuery(e.target.value)}
            style={{ flex: "1 1 240px" }}
            hint="POST /assets/broll/search queries the configured provider. Search is free; nothing is downloaded."
          />
          <Button type="submit" variant="primary" loading={stock.pending} disabled={!stockQuery.trim()}>
            Search
          </Button>
        </form>
        {stock.error ? (
          <p className="ym-error" role="alert">
            The stock provider refused the search: {stock.error}
          </p>
        ) : null}
        {stock.pending ? null : stockHits ? (
          stockHits.length === 0 ? (
            <EmptyState title="No stock match" description="The provider returned an empty catalogue for that query." />
          ) : (
            <DataTable
              rows={stockHits}
              columns={[
                { key: "id", header: "Clip", cell: (h) => <span style={MONO}>{h.video_id}</span> },
                { key: "author", header: "Creator", cell: (h) => h.author || <span className="ym-muted">not reported</span> },
                { key: "duration", header: "Duration", align: "right", cell: (h) => duration(h.duration) },
                { key: "preview", header: "Preview URL", cell: (h) => <Provenance value={h.preview} label="preview" /> },
                { key: "page", header: "Page URL", cell: (h) => <Provenance value={h.page_url} label="page" /> },
              ]}
              rowKey={(h) => h.video_id}
              caption="Stock search results"
              empty="No stock match"
            />
          )
        ) : (
          <EmptyState
            title="No search run yet"
            description="Stock results appear here once you search. The catalogue is not prefetched — each query costs the provider a request."
          />
        )}
      </Panel>

      <Modal
        open={selected !== null}
        onClose={() => setSelected(null)}
        title={selected ? `${humanize(selected.type)} · ${selected.id.slice(0, 8)}` : "Asset"}
        width={680}
      >
        {selected ? <AssetDetail asset={selected} /> : null}
      </Modal>
    </>
  );
}

/* ==========================================================================
 * Cells
 * ======================================================================= */

const MONO: React.CSSProperties = { fontFamily: "var(--font-mono)" };

/**
 * The provenance cell. It renders the REDACTED form and says so; the raw value
 * is never placed in the DOM, in an href, or in a title attribute.
 */
function Provenance({ value, label }: { value: string | null | undefined; label: string }) {
  const red = redactProvenance(value);
  return (
    <span data-provenance={red.redacted ? "redacted" : "clean"} data-provenance-label={label}>
      <span style={MONO}>{red.display}</span>{" "}
      {red.redacted ? (
        <Badge tone="warning" title={`Removed: ${red.removedKeys.join(", ") || "userinfo"}`}>
          credential removed
        </Badge>
      ) : null}
    </span>
  );
}

/** A media row's provenance is its storage key. */
function AssetProvenance({ asset }: { asset: MediaAsset }) {
  return <Provenance value={asset.storage_key} label="storage key" />;
}

function AssetCard({ asset, onOpen }: { asset: MediaAsset; onOpen: () => void }) {
  return (
    <Panel dense>
      <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
        <div style={{ display: "flex", gap: "var(--space-1)", flexWrap: "wrap" }}>
          <Badge tone="neutral">{humanize(asset.type)}</Badge>
          <Badge tone={originTone(asset.origin)}>{humanize(asset.origin)}</Badge>
          {isGeneratedAsset(asset) ? <Badge tone="info">Generated</Badge> : <Badge tone="neutral">Source</Badge>}
        </div>
        <div>
          <div style={MONO}>{asset.id.slice(0, 8)}</div>
          <div className="ym-muted">{dimensions(asset)}</div>
        </div>
        <Provenance value={asset.storage_key} label="storage key" />
        <Button size="sm" onClick={onOpen}>
          Preview
        </Button>
      </div>
    </Panel>
  );
}

function AssetDetail({ asset }: { asset: MediaAsset }) {
  /* The file URL is a BROWSER AUTH url for the media element: an <img>/<video>
   * cannot send an Authorization header, which is why lib/api.ts builds it this
   * way for every preview in the app. It is deliberately NOT used for
   * provenance: provenance renders through `redactProvenance` only. */
  const file = mediaFileUrl(asset.id);
  const isPlayable = asset.mime_type.startsWith("video/") || asset.mime_type.startsWith("audio/");
  const isPicture = asset.mime_type.startsWith("image/");

  return (
    <Grid min={280} gap="md">
      <Panel title="Preview" dense>
        {isPicture ? (
          <img src={file} alt={`${humanize(asset.type)} asset ${asset.id.slice(0, 8)}`} style={{ maxWidth: "100%" }} />
        ) : isPlayable ? (
          asset.mime_type.startsWith("video/") ? (
            <video src={file} controls style={{ maxWidth: "100%" }} />
          ) : (
            <audio src={file} controls style={{ width: "100%" }} />
          )
        ) : (
          <EmptyState
            title="No inline preview"
            description={`The backend reports mime_type "${asset.mime_type || "not reported"}", which this browser cannot render inline.`}
          />
        )}
      </Panel>
      <Panel title="Technical" dense>
        <dl style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "var(--space-1) var(--space-3)" }}>
          <dt className="ym-muted">Type</dt>
          <dd>{humanize(asset.type)}</dd>
          <dt className="ym-muted">Origin</dt>
          <dd>{humanize(asset.origin)}</dd>
          <dt className="ym-muted">Produced by</dt>
          <dd>{isGeneratedAsset(asset) ? "This workspace" : isSourceAsset(asset) ? "An external source" : "Origin not recognised"}</dd>
          <dt className="ym-muted">Provider</dt>
          <dd>{asset.provider || "not reported"}</dd>
          <dt className="ym-muted">MIME</dt>
          <dd>{asset.mime_type || "not reported"}</dd>
          <dt className="ym-muted">Dimensions</dt>
          <dd>{asset.width !== null && asset.height !== null ? `${asset.width}×${asset.height}` : "not reported"}</dd>
          <dt className="ym-muted">Duration</dt>
          <dd>{asset.duration_seconds !== null ? duration(asset.duration_seconds) : "not reported"}</dd>
          <dt className="ym-muted">Checksum</dt>
          <dd style={MONO}>{asset.checksum || "not reported"}</dd>
          <dt className="ym-muted">Registered</dt>
          <dd>{createdAt(asset.created_at)}</dd>
        </dl>
      </Panel>
      <Panel title="Provenance" subtitle="Credential-bearing parts are removed before render." dense>
        <Textarea
          label="Storage key (redacted)"
          readOnly
          rows={3}
          value={redactProvenance(asset.storage_key).display}
        />
        <p className="ym-hint">
          <code>lib/api.ts#mediaFileUrl</code> is used for the inline preview above only. It is never shown as
          text: the access token in its query string is a credential.
        </p>
      </Panel>
    </Grid>
  );
}

export default Assets;
