import { useMemo, useState } from "react";
import { wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Badge, Card, Empty, ErrorBox, Loading, PageHeader, SearchInput, toast } from "../components/ui";
import { fmtAgo, fmtDate } from "../lib/format";

/* ---- Wire shape: api/v1/activity.py + services/activity.py::event_dto -----
 * The ledger HOISTS actor / target / version / project_id out of data_json to
 * the top level and keeps the original payload under `data`. Filters: kind,
 * project_id, target_type, target_id, since, limit (capped at MAX_LIMIT=200). */

type LedgerTarget = { type?: string | null; id?: string | null } | null;

type LedgerRow = {
  id: string;
  kind: string;
  message: string;
  level: string;
  source?: string | null;
  actor?: string | null;
  target?: LedgerTarget;
  version?: string | null;
  project_id?: string | null;
  request_id?: string | null;
  created_at: string;
  data?: {
    actor?: string | null;
    target?: { type?: string | null; id?: string | null } | null;
    version?: string | null;
    request_id?: string | null;
  } | null;
};

/** services/activity.py: MAX_LIMIT = 200 — anything larger is a 422. */
const LIMIT_STEPS = [25, 50, 100, 200];

/** Work 11 kinds (contracts §9) — the filter never depends on this list being
 *  complete: anything the ledger actually returns shows up in the dropdown
 *  too, so an unlisted kind is filterable rather than invisible. */
const WORK11_KINDS = [
  "PROJECT_CREATED",
  "TIMELINE_EDITED",
  "VERSION_CREATED",
  "COMMENT_ADDED",
  "REVIEW_REQUESTED",
  "CHANGES_REQUESTED",
  "APPROVED",
  "EXPORT_CREATED",
  "PUBLISHED",
  "ARCHIVE_CREATED",
  "RETENTION_SWEEP",
  "REVIEW_ASSIGNED",
  "REVISION_REQUESTED",
  "EXPORT_COMPLETED",
  "EXPORT_FAILED",
  "PROJECT_TRANSFERRED",
  "RETENTION_POLICY_UPDATED",
];

const levelColor = (l: string) =>
  l === "error" ? "var(--danger)" : l === "warning" ? "var(--warn)" : l === "success" ? "var(--accent)" : "var(--text-faint)";

function short(s?: string | null, n = 10): string {
  if (!s) return "—";
  return s.length > n ? `${s.slice(0, n)}…` : s;
}

/* event_dto hoists these to the top level; the nested `data` copy is the
 * fallback for rows emitted before the hoist existed. */
const actorOf = (r: LedgerRow) => r.actor ?? r.data?.actor ?? null;
const versionOf = (r: LedgerRow) => r.version ?? r.data?.version ?? null;
const requestIdOf = (r: LedgerRow) => r.request_id || r.data?.request_id || "";
const targetOf = (r: LedgerRow) => r.target ?? r.data?.target ?? null;

export default function Activity() {
  const [kind, setKind] = useState("");
  const [targetType, setTargetType] = useState("");
  const [q, setQ] = useState("");
  const [limit, setLimit] = useState(50);

  const params = new URLSearchParams();
  if (kind) params.set("kind", kind);
  if (targetType) params.set("target_type", targetType);
  params.set("limit", String(limit));
  const path = `/activity?${params.toString()}`;

  const list = useFetch<{ items?: LedgerRow[] }>(() => wsApi.get(path), [kind, targetType, limit]);

  const rows = useMemo(() => {
    const raw = list.data?.items ?? [];
    const needle = q.trim().toLowerCase();
    return raw
      .filter((r) => {
        if (!needle) return true;
        return (
          (r.message ?? "").toLowerCase().includes(needle) ||
          (r.kind ?? "").toLowerCase().includes(needle) ||
          (actorOf(r) ?? "").toLowerCase().includes(needle) ||
          (versionOf(r) ?? "").toLowerCase().includes(needle) ||
          (requestIdOf(r) ?? "").toLowerCase().includes(needle) ||
          `${targetOf(r)?.type ?? ""}:${targetOf(r)?.id ?? ""}`.toLowerCase().includes(needle)
        );
      })
      // newest-first regardless of the order the route hands them over
      .slice()
      .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
  }, [list.data, q]);

  const kindOptions = useMemo(() => {
    const seen = (list.data?.items ?? []).map((r) => r.kind);
    return Array.from(new Set([...WORK11_KINDS, ...seen])).filter(Boolean).sort();
  }, [list.data]);

  const targetTypes = useMemo(() => {
    const seen = (list.data?.items ?? [])
      .map((r) => targetOf(r)?.type)
      .filter((t): t is string => !!t);
    return Array.from(new Set(seen)).sort();
  }, [list.data]);

  function refresh() {
    try {
      list.reload();
    } catch (e: any) {
      toast(e?.message ?? "refresh failed", "error", "Ledger");
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Activity"
        subtitle="Append-only workspace ledger — every entry was written once and is never edited or deleted."
        actions={
          <button className="btn-outline !text-xs" onClick={refresh}>
            Refresh
          </button>
        }
      />

      <div
        className="rounded-xl px-4 py-2.5 flex items-center gap-2.5 flex-wrap text-[12.5px]"
        style={{ background: "var(--bg-inset)", border: "var(--seam)" }}
      >
        <Badge tone="muted">read-only</Badge>
        <span style={{ color: "var(--text-muted)" }}>
          No edit, retry or delete affordance exists on this ledger — by design, the API exposes no mutator.
        </span>
      </div>

      <div className="flex flex-wrap gap-3 items-center">
        <select className="select !w-[200px] !text-[12.5px]" value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Event kind">
          <option value="">All kinds</option>
          {kindOptions.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
        <select
          className="select !w-[180px] !text-[12.5px]"
          value={targetType}
          onChange={(e) => setTargetType(e.target.value)}
          aria-label="Target type"
        >
          <option value="">All targets</option>
          {targetTypes.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
        <select
          className="select !w-[130px] !text-[12.5px]"
          value={String(limit)}
          onChange={(e) => setLimit(Number(e.target.value))}
          aria-label="Rows"
        >
          {LIMIT_STEPS.map((n) => (
            <option key={n} value={String(n)}>
              newest {n}
            </option>
          ))}
        </select>
        <div className="w-[240px] ml-auto">
          <SearchInput value={q} onChange={setQ} placeholder="Actor, target, version, request id…" />
        </div>
      </div>

      {list.loading && !list.data ? (
        <Loading rows={4} />
      ) : list.error && !list.data ? (
        <ErrorBox error={list.error} onRetry={list.reload} />
      ) : !rows.length ? (
        <Card>
          <Empty
            title={kind || targetType || q ? "No entries match this filter" : "Ledger is empty"}
            hint={
              kind || targetType || q
                ? "Widen the kind or target filter — the ledger only keeps what your workspace has emitted."
                : "Entries appear as agents work: timeline edits, review decisions, exports, publishes. Nothing is back-filled retroactively."
            }
            action={
              <button className="btn-outline !text-xs" onClick={list.reload}>
                Refresh
              </button>
            }
          />
        </Card>
      ) : (
        <>
          <div className="text-[11.5px]" style={{ color: "var(--text-faint)" }}>
            showing {rows.length} of the newest {limit} — raise the limit control to pull more history
          </div>
          <Card pad={false}>
            <div>
              {rows.map((r) => (
                <Row key={r.id} row={r} />
              ))}
            </div>
          </Card>
        </>
      )}
    </div>
  );
}

function Row({ row }: { row: LedgerRow }) {
  const actor = actorOf(row);
  const target = targetOf(row);
  const requestId = requestIdOf(row);
  return (
    <div className="px-4 py-3 flex gap-3 items-start" style={{ borderBottom: "var(--seam)" }}>
      <span className="mt-1.5 shrink-0" style={{ color: levelColor(row.level) }} title={row.level}>
        ●
      </span>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <Badge tone={row.level === "error" ? "error" : row.level === "warning" ? "warning" : "muted"}>{row.kind}</Badge>
          <span className="text-[13px] break-words">{row.message}</span>
        </div>
        <div className="font-mono text-[11px] mt-1 flex flex-wrap gap-x-3" style={{ color: "var(--text-faint)" }}>
          <span title={fmtDate(row.created_at)}>{fmtAgo(row.created_at)}</span>
          {actor && <span title={actor}>actor {short(actor)}</span>}
          {target && (target.type || target.id) && (
            <span title={`${target.type ?? ""}:${target.id ?? ""}`}>
              target {target.type ?? "?"}:{short(target.id, 10)}
            </span>
          )}
          {versionOf(row) && <span>version {versionOf(row)}</span>}
          {row.project_id && <span title={row.project_id}>project {short(row.project_id, 8)}</span>}
          {row.source && <span>via {row.source}</span>}
          {requestId && <span title={requestId}>req {short(requestId, 12)}</span>}
        </div>
      </div>
    </div>
  );
}
