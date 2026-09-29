import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { wsApi } from "../../lib/api";
import { useFetch } from "../../hooks/hooks";
import { Badge, Card, Empty, ErrorBox, Loading } from "../ui";

/* Wire shapes: GET /timelines/{id}/diff (api/v1/timelines.py) +
 * engine/timeline_diff.diff_timeline_docs / diff_brand_snapshots. */
type VersionRow = { id: string; version: number; is_tip?: boolean; created_at?: string };

type ClipRef = {
  track_id: string;
  track_kind: string;
  clip_id: string;
  name: string;
  start: number;
  duration: number;
};

type TrackRef = { track_id: string; kind: string; name: string; clip_count: number };

type ModifiedClip = {
  track_id: string;
  track_kind: string;
  clip_id: string;
  changes: Record<string, unknown>;
};

type DiffSummary = {
  added: number;
  removed: number;
  modified: number;
  tracks_added: number;
  tracks_removed: number;
  metadata_changed: number;
  changed: boolean;
};

type DiffResponse = {
  from: { version: number; manifest_hash: string | null };
  to: { version: number; manifest_hash: string | null };
  diff: {
    added_clips: ClipRef[];
    removed_clips: ClipRef[];
    modified_clips: ModifiedClip[];
    added_tracks: TrackRef[];
    removed_tracks: TrackRef[];
    metadata_changes: Record<string, { before: unknown; after: unknown }>;
    summary: DiffSummary;
  };
  brand_diff: {
    available: boolean;
    reason?: string;
    selection?: string;
    diff?: {
      changed?: Record<string, unknown>;
      added?: Record<string, unknown>;
      removed?: Record<string, unknown>;
    };
  };
};

type Props = { timelineId: string; versions: VersionRow[] };

function short(s?: string | null, n = 8): string {
  if (!s) return "—";
  return s.length > n ? `${s.slice(0, n)}…` : s;
}

function val(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

/** Renders one clip's change set honestly: direct before/after pairs, plus
 *  nested groups (timing.start, trim.end, …) flattened one level deep. */
function ChangeLines({ changes }: { changes: Record<string, unknown> }) {
  const lines: { key: string; before: unknown; after: unknown }[] = [];
  for (const [group, value] of Object.entries(changes ?? {})) {
    if (value && typeof value === "object" && !Array.isArray(value)) {
      const obj = value as Record<string, unknown>;
      if ("before" in obj || "after" in obj) {
        lines.push({ key: group, before: obj.before, after: obj.after });
        continue;
      }
      for (const [sub, subVal] of Object.entries(obj)) {
        if (subVal && typeof subVal === "object" && !Array.isArray(subVal)) {
          const s = subVal as Record<string, unknown>;
          if ("before" in s || "after" in s) {
            lines.push({ key: `${group}.${sub}`, before: s.before, after: s.after });
            continue;
          }
        }
        lines.push({ key: `${group}.${sub}`, before: undefined, after: subVal });
      }
      continue;
    }
    lines.push({ key: group, before: undefined, after: value });
  }
  if (!lines.length) return null;
  return (
    <div className="mt-1 space-y-0.5">
      {lines.map((l, i) => (
        <div key={`${l.key}-${i}`} className="font-mono text-[11px]" style={{ color: "var(--text-muted)" }}>
          {l.key}: <span style={{ color: "var(--danger)", textDecoration: "line-through" }}>{val(l.before)}</span>{" "}
          <span style={{ color: "var(--accent)" }}>{val(l.after)}</span>
        </div>
      ))}
    </div>
  );
}

/**
 * Version comparison: pick two versions of this timeline and render the
 * semantic diff from lane D's GET /timelines/{id}/diff route. Nothing is
 * synthesised — added/removed/changed segments come straight from the
 * response, and an identical pair reports "no differences".
 */
export default function VersionCompare({ timelineId, versions }: Props) {
  const sorted = [...versions].sort((a, b) => b.version - a.version);
  const [fromV, setFromV] = useState<number | null>(null);
  const [toV, setToV] = useState<number | null>(null);

  // default: newest pair (tip vs previous), only once versions have loaded
  useEffect(() => {
    if (!sorted.length) return;
    setToV((cur) => (cur == null ? sorted[0].version : cur));
    setFromV((cur) => (cur == null ? (sorted[1]?.version ?? sorted[0].version) : cur));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [versions.length]);

  const ready = fromV != null && toV != null;
  const same = ready && fromV === toV;

  const q = useFetch<DiffResponse | null>(
    () =>
      ready && !same
        ? (wsApi.get(
            `/timelines/${timelineId}/diff?from_version=${fromV}&to_version=${toV}`
          ) as Promise<DiffResponse>)
        : Promise.resolve(null),
    [timelineId, fromV, toV, same]
  );

  const d = q.data;

  return (
    <Card>
      <div className="flex items-center gap-2 flex-wrap">
        <b className="text-[13px]">Compare versions</b>
        <select
          className="select !w-auto !text-[12px]"
          value={fromV ?? ""}
          onChange={(e) => setFromV(Number(e.target.value))}
          aria-label="From version"
        >
          <option value="" disabled>From…</option>
          {sorted.map((v) => (
            <option key={v.id} value={v.version}>v{v.version}{v.is_tip ? " (tip)" : ""}</option>
          ))}
        </select>
        <span style={{ color: "var(--text-faint)" }}>→</span>
        <select
          className="select !w-auto !text-[12px]"
          value={toV ?? ""}
          onChange={(e) => setToV(Number(e.target.value))}
          aria-label="To version"
        >
          <option value="" disabled>To…</option>
          {sorted.map((v) => (
            <option key={v.id} value={v.version}>v{v.version}{v.is_tip ? " (tip)" : ""}</option>
          ))}
        </select>
        <button
          className="btn-ghost !text-xs"
          onClick={() => { setFromV(toV); setToV(fromV); }}
          title="Swap sides"
        >
          ⇄ Swap
        </button>
        <button className="btn-ghost !text-xs ml-auto" onClick={q.reload}>↻</button>
      </div>

      <div className="mt-2">
        {!versions.length ? (
          <Empty title="No saved versions yet" hint="Save a version from the Inspector to compare history." />
        ) : same ? (
          <Empty title="Pick two different versions" hint="A version compared with itself has no differences to show." />
        ) : q.loading && !d ? (
          <Loading rows={3} />
        ) : q.error ? (
          <ErrorBox error={q.error} onRetry={q.reload} />
        ) : !d ? (
          <Empty title="Nothing to compare" hint="Choose a from/to pair above." />
        ) : (
          <DiffBody d={d} />
        )}
      </div>
    </Card>
  );
}

function DiffBody({ d }: { d: DiffResponse }) {
  const s = d.diff.summary;
  const brand = d.brand_diff;

  if (!s.changed) {
    return (
      <div className="space-y-2">
        <Empty
          title={`No differences between v${d.from.version} and v${d.to.version}`}
          hint={`Identical manifests (hash ${short(d.from.manifest_hash, 12)}).`}
        />
        <BrandDiff brand={brand} />
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2 flex-wrap text-[11.5px]" style={{ color: "var(--text-faint)" }}>
        <span className="font-mono">v{d.from.version} ({short(d.from.manifest_hash, 10)})</span>
        <span>→</span>
        <span className="font-mono">v{d.to.version} ({short(d.to.manifest_hash, 10)})</span>
      </div>

      <div className="flex items-center gap-2 flex-wrap">
        <Badge tone="success">+{s.added} added</Badge>
        <Badge tone="error">−{s.removed} removed</Badge>
        <Badge tone="warning">~{s.modified} modified</Badge>
        {s.tracks_added > 0 && <Badge tone="success">+{s.tracks_added} tracks</Badge>}
        {s.tracks_removed > 0 && <Badge tone="error">−{s.tracks_removed} tracks</Badge>}
        {s.metadata_changed > 0 && <Badge tone="info">{s.metadata_changed} metadata</Badge>}
      </div>

      {d.diff.added_clips.length > 0 && (
        <Section title="Added clips" tone="success">
          {d.diff.added_clips.map((c) => (
            <div key={`${c.track_id}-${c.clip_id}`} className="text-[12.5px]">
              <Badge tone="success">+</Badge>{" "}
              <span className="font-medium">{c.name || short(c.clip_id)}</span>{" "}
              <span className="font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>
                {c.track_kind} @ {c.start.toFixed(1)}s · {c.duration.toFixed(1)}s
              </span>
            </div>
          ))}
        </Section>
      )}

      {d.diff.removed_clips.length > 0 && (
        <Section title="Removed clips" tone="error">
          {d.diff.removed_clips.map((c) => (
            <div key={`${c.track_id}-${c.clip_id}`} className="text-[12.5px]">
              <Badge tone="error">−</Badge>{" "}
              <span className="font-medium">{c.name || short(c.clip_id)}</span>{" "}
              <span className="font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>
                {c.track_kind} @ {c.start.toFixed(1)}s · {c.duration.toFixed(1)}s
              </span>
            </div>
          ))}
        </Section>
      )}

      {d.diff.modified_clips.length > 0 && (
        <Section title="Changed clips" tone="warning">
          {d.diff.modified_clips.map((m) => (
            <div
              key={`${m.track_id}-${m.clip_id}`}
              className="rounded-lg px-2 py-1.5"
              style={{ background: "var(--bg-inset)" }}
            >
              <div className="text-[12.5px]">
                <Badge tone="warning">~</Badge>{" "}
                <span className="font-mono">{short(m.clip_id)}</span>{" "}
                <span className="font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>{m.track_kind}</span>
              </div>
              <ChangeLines changes={m.changes} />
            </div>
          ))}
        </Section>
      )}

      {(d.diff.added_tracks.length > 0 || d.diff.removed_tracks.length > 0) && (
        <Section title="Tracks" tone="info">
          {d.diff.added_tracks.map((t) => (
            <div key={`a-${t.track_id}`} className="text-[12.5px]">
              <Badge tone="success">+</Badge> {t.kind} “{t.name}” ({t.clip_count} clips)
            </div>
          ))}
          {d.diff.removed_tracks.map((t) => (
            <div key={`r-${t.track_id}`} className="text-[12.5px]">
              <Badge tone="error">−</Badge> {t.kind} “{t.name}” ({t.clip_count} clips)
            </div>
          ))}
        </Section>
      )}

      {Object.keys(d.diff.metadata_changes ?? {}).length > 0 && (
        <Section title="Document metadata" tone="info">
          {Object.entries(d.diff.metadata_changes).map(([k, v]) => (
            <div key={k} className="font-mono text-[11.5px]" style={{ color: "var(--text-muted)" }}>
              {k}: <span style={{ color: "var(--danger)", textDecoration: "line-through" }}>{val(v.before)}</span>{" "}
              <span style={{ color: "var(--accent)" }}>{val(v.after)}</span>
            </div>
          ))}
        </Section>
      )}

      <BrandDiff brand={brand} />
    </div>
  );
}

function BrandDiff({ brand }: { brand: DiffResponse["brand_diff"] }) {
  if (!brand) return null;
  if (!brand.available) {
    return (
      <div className="text-[11.5px]" style={{ color: "var(--text-faint)" }}>
        Brand diff unavailable: {brand.reason ?? "no comparable snapshot"} — nothing is invented.
      </div>
    );
  }
  const changed = Object.keys(brand.diff?.changed ?? {});
  const added = Object.keys(brand.diff?.added ?? {});
  const removed = Object.keys(brand.diff?.removed ?? {});
  return (
    <Section title="Brand DNA" tone="info">
      <div className="flex items-center gap-2 flex-wrap text-[12.5px]">
        <Badge tone={changed.length ? "warning" : "muted"}>{changed.length} changed</Badge>
        <Badge tone={added.length ? "success" : "muted"}>{added.length} added</Badge>
        <Badge tone={removed.length ? "error" : "muted"}>{removed.length} removed</Badge>
        {brand.selection && (
          <span className="text-[11px]" style={{ color: "var(--text-faint)" }}>{brand.selection}</span>
        )}
      </div>
      {changed.length > 0 && (
        <div className="mt-1 font-mono text-[11.5px]" style={{ color: "var(--text-muted)" }}>
          {changed.slice(0, 8).join(", ")}{changed.length > 8 ? " …" : ""}
        </div>
      )}
    </Section>
  );
}

function Section({
  title,
  tone,
  children,
}: {
  title: string;
  tone: string;
  children: ReactNode;
}) {
  return (
    <div>
      <div className="panel-label mb-1.5">
        <Badge tone={tone}>{title}</Badge>
      </div>
      <div className="space-y-1">{children}</div>
    </div>
  );
}
