/* CalendarView — day / week / month over the canonical ScheduleEntry store.
 *
 * There is exactly ONE schedule in this system and this screen reads it. The
 * planner's `engine/planning/calendar.py` writes `ScheduleEntry` through the
 * same store the Scheduler dispatches from, so this list is not a copy of the
 * plan and a plan item with no entry is genuinely unscheduled.
 *
 *   GET    /calendar                  PENDING | DISPATCHING | QUEUED | FAILED
 *   POST   /calendar                  place an entry (admin)
 *   PATCH  /calendar/{entry_id}       reschedule (admin)
 *   DELETE /calendar/{entry_id}       cancel (admin)
 *   GET    /calendar/best-times       hour-of-day by average views
 *   GET    /calendar/response-windows ranked windows + WHY each was chosen
 *   GET    /planner/calendar?days=    capacity vs committed
 *   GET    /campaigns                 campaign names for the lineage column
 *
 * THE RESCHEDULE PATH, END TO END
 *
 *   1. The control only renders for entries the backend will actually move.
 *      `PATCH /calendar/{entry_id}` raises 409 unless `status` is PENDING or
 *      FAILED, so those are the only rows that get a button; every other row
 *      states the refusal instead of offering a control that cannot work.
 *   2. The operator picks a datetime. The value is converted to a TIMEZONE-AWARE
 *      UTC ISO string, because `_validate_run_at` rejects a naive value outright
 *      — a naive local-time string is a 422, not a silent local-time read.
 *   3. A time in the past is blocked client-side, for the same reason: the
 *      server refuses it.
 *   4. `useMutation` → `wsApi.patch`. A failure renders the ApiError detail
 *      verbatim. There is NO generic retry, because a reschedule is not a paid
 *      action but it IS a state change, and "Retry" on a 409 would invite an
 *      operator to keep hammering a refusal.
 *   5. On success the LIST is refetched (`reload()`), so the screen shows the
 *      server's row, not a locally-optimistic guess.
 *
 * BEST TIMES ARE NEVER INVENTED. `/calendar/best-times` returns
 * `measured: false` and platform seed hours when fewer than three
 * metric-backed posts exist; that flag is rendered, because "18:00" and
 * "18:00 because 40 posts proved it" are different facts.
 */

import { useMemo, useState, type ReactNode } from "react";
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
  humanize,
  type Column,
} from "../../design-system/primitives";
import { useMutation, useWsQuery } from "../../api/queries";
import { wsApi } from "../../lib/api";
import { useSession } from "../../state/session";
import type { PlannerCalendar } from "./Planner";

/* ==========================================================================
 * Response shapes
 *
 * api/v1/content.py `calendar_router.list_schedule` — the canonical store.
 * ======================================================================= */

export type ScheduleEntryRow = {
  id: string;
  platform: string;
  /** naive-UTC ISO from the store, emitted with a trailing "Z". */
  run_at: string;
  content_item_id: string | null;
  campaign_id: string | null;
  /** PENDING | DISPATCHING | QUEUED | FAILED */
  status: string;
};

export type ScheduleList = { items: ScheduleEntryRow[] };

/* api/v1/content.py `reschedule` */
export type RescheduleResult = { id: string; run_at: string };

/* api/v1/content.py `add_schedule` */
export type CreateScheduleResult = { id: string };

/* api/v1/content.py `cancel_schedule` */
export type CancelScheduleResult = { cancelled: boolean };

/* api/v1/content.py `best_times` */
export type BestTimeItem = { hour: number; avg_views: number; posts: number };
export type BestTimes = {
  items: BestTimeItem[];
  /** false ⇒ the hours are platform seeds, not measured performance. */
  measured: boolean;
};

/* engine/agents/scheduler.py `recommend_response_windows` */
export type ResponseWindowItem = { hour: number; reason: string; sources: string[] };
export type ResponseWindows = {
  items: ResponseWindowItem[];
  measured: boolean;
  activity: boolean;
  audience_activity: { available: boolean; hours: number[] };
  caps: Record<string, number | null>;
  avoid_hours: number[];
  /** recommendation_only unless settings.schedule_automation is true. */
  activity_policy: string;
  notes: string[];
};

/* api/v1/content.py `list_campaigns` */
export type CampaignRef = {
  id: string;
  name: string;
  status: string;
  platforms: string[];
};

/* ==========================================================================
 * Time helpers
 *
 * `run_at` is stored NAIVE and read as UTC (the planner writes it that way and
 * the column has no timezone). So the key for grouping is the UTC date, not the
 * operator's local date — otherwise every late-evening entry silently moves a
 * day and the month grid lies about what is scheduled when.
 * ======================================================================= */

const MS_DAY = 86_400_000;

/** UTC YYYY-MM-DD for a naive-UTC ISO string. */
export function utcDayKey(iso: string): string {
  return iso.slice(0, 10);
}

function parseUtc(iso: string): Date {
  const normalized = iso.endsWith("Z") ? iso : `${iso}Z`;
  const d = new Date(normalized);
  return Number.isNaN(d.getTime()) ? new Date(0) : d;
}

function addDaysKey(key: string, days: number): string {
  const base = new Date(`${key}T00:00:00Z`);
  return new Date(base.getTime() + days * MS_DAY).toISOString().slice(0, 10);
}

function dayOfWeek(key: string): number {
  return new Date(`${key}T00:00:00Z`).getUTCDay();
}

function monthStartKey(key: string): string {
  return `${key.slice(0, 7)}-01`;
}

function monthLabel(key: string): string {
  const d = new Date(`${key}T00:00:00Z`);
  return d.toLocaleString("en-GB", { month: "long", year: "numeric", timeZone: "UTC" });
}

function dayLabel(key: string): string {
  const d = new Date(`${key}T00:00:00Z`);
  return d.toLocaleString("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });
}

function hhmm(iso: string): string {
  return iso.slice(11, 16);
}

function hourLabel(hour: number): string {
  return `${String(hour).padStart(2, "0")}:00 UTC`;
}

/** `datetime-local` wants `YYYY-MM-DDTHH:mm` with NO zone suffix. */
function toLocalInput(iso: string): string {
  return iso.slice(0, 16);
}

/**
 * Local wall-clock input → the tz-aware UTC ISO the backend demands.
 * Returns null when the browser produced an unparseable value.
 */
export function localInputToUtcIso(value: string): string | null {
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return null;
  return d.toISOString();
}

/** The only statuses `PATCH /calendar/{id}` and `DELETE /calendar/{id}` accept. */
export const MUTABLE_ENTRY_STATUSES = ["PENDING", "FAILED"] as const;

export function entryIsMutable(status: string | null | undefined): boolean {
  return (MUTABLE_ENTRY_STATUSES as readonly string[]).includes((status ?? "").toUpperCase());
}

function mutableRefusal(status: string): string {
  return `Cannot reschedule a ${status.toUpperCase()} entry — the backend only moves PENDING or FAILED.`;
}

/* ==========================================================================
 * Reschedule / cancel
 * ======================================================================= */

function RescheduleDialog({
  entry,
  onClose,
  onDone,
}: {
  entry: ScheduleEntryRow | null;
  onClose: () => void;
  onDone: () => void;
}) {
  const [value, setValue] = useState(entry ? toLocalInput(entry.run_at) : "");

  const parsed = value ? localInputToUtcIso(value) : null;
  const inPast = parsed !== null && new Date(parsed).getTime() <= Date.now();
  const invalid = value.length > 0 && parsed === null;

  const reschedule = useMutation<string, RescheduleResult>(
    (entryId) => wsApi.patch(`/calendar/${entryId}`, { run_at: parsed }) as Promise<RescheduleResult>,
    { onSuccess: onDone },
  );

  const cancelEntry = useMutation<string, CancelScheduleResult>((entryId) =>
    wsApi.del(`/calendar/${entryId}`) as Promise<CancelScheduleResult>,
    { onSuccess: onDone },
  );

  if (!entry) return null;

  return (
    <>
      <Modal
        open
        onClose={onClose}
        title="Reschedule this entry"
        footer={
          <>
            <Button variant="ghost" onClick={onClose}>
              Keep current time
            </Button>
            <DestructiveButton
              confirmLabel={`Cancel entry ${entry.id.slice(0, 8)}? It becomes CANCELLED and is hidden from the calendar.`}
              onConfirm={() => void cancelEntry.run(entry.id)}
              disabled={cancelEntry.pending}
            >
              Cancel entry
            </DestructiveButton>
            <Button
              variant="primary"
              loading={reschedule.pending}
              disabled={!parsed || inPast}
              onClick={() => void reschedule.run(entry.id)}
            >
              PATCH reschedule
            </Button>
          </>
        }
      >
        <p className="ym-hint">
          <code>PATCH /calendar/&#123;entry_id&#125;</code> · currently{" "}
          <strong>{entry.run_at.replace("Z", " UTC")}</strong> ·{" "}
          {humanize(entry.platform)}
        </p>
        <Field
          label="New run time"
          type="datetime-local"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          error={inPast ? "run_at cannot be in the past" : undefined}
          hint="Sent as a timezone-aware UTC ISO string. The backend rejects a naive local-time value."
        />
        {invalid ? <p className="ym-error">That value could not be read as a date and time.</p> : null}
        {entry.status.toUpperCase() === "FAILED" ? (
          <div className="ym-notif-item ym-notif-warning">
            <div className="ym-notif-title">This re-arms a FAILED entry</div>
            <div className="ym-notif-detail">
              Rescheduling a FAILED entry sets it back to PENDING. That is the
              operator path for a publish that failed — the only one the backend
              offers. It is not a paid resubmit, and nothing here can re-bill.
            </div>
          </div>
        ) : null}
        {reschedule.error ? <p className="ym-error">{reschedule.error}</p> : null}
        {cancelEntry.error ? <p className="ym-error">{cancelEntry.error}</p> : null}
      </Modal>
    </>
  );
}

function EntryRowActions({
  entry,
  onReschedule,
}: {
  entry: ScheduleEntryRow;
  onReschedule: (entry: ScheduleEntryRow) => void;
}) {
  if (!entryIsMutable(entry.status)) {
    return (
      <span className="ym-muted" title={mutableRefusal(entry.status)}>
        {mutableRefusal(entry.status)}
      </span>
    );
  }
  return (
    <Button size="sm" onClick={() => onReschedule(entry)}>
      Reschedule
    </Button>
  );
}

/* ==========================================================================
 * Views: day / week / month
 *
 * All three are client-side bucketing of the rows the server returned. No view
 * invents a slot, and an empty cell is empty — not "0 posts".
 * ======================================================================= */

type Scale = "day" | "week" | "month";

/** The UTC day keys a scale covers, anchored on `anchor`. */
function bucketCells(scale: Scale, anchor: string): string[] {
  if (scale === "day") return [anchor];
  if (scale === "week") {
    // Anchor to the Sunday of the anchor's week, in UTC.
    const start = addDaysKey(anchor, -dayOfWeek(anchor));
    return Array.from({ length: 7 }, (_, i) => addDaysKey(start, i));
  }
  const first = monthStartKey(anchor);
  const start = addDaysKey(first, -dayOfWeek(first));
  return Array.from({ length: 42 }, (_, i) => addDaysKey(start, i));
}

function groupByDay(items: ScheduleEntryRow[]): Map<string, ScheduleEntryRow[]> {
  const byDay = new Map<string, ScheduleEntryRow[]>();
  for (const item of items) {
    const key = utcDayKey(item.run_at);
    const list = byDay.get(key);
    if (list) list.push(item);
    else byDay.set(key, [item]);
  }
  return byDay;
}

function DayGrid({ cells, byDay, entriesFor }: {
  cells: string[];
  byDay: Map<string, ScheduleEntryRow[]>;
  entriesFor: (row: ScheduleEntryRow) => ReactNode;
}) {
  return (
    <div className="ym-grid ym-grid--sm" style={{ gridTemplateColumns: "repeat(7, minmax(0, 1fr))" }}>
      {cells.map((key) => {
        const rows = byDay.get(key) ?? [];
        const inMonth = key.slice(0, 7) === monthStartKey(key).slice(0, 7);
        return (
          <div
            key={key}
            style={{
              border: "1px solid var(--border-subtle)",
              borderRadius: "var(--radius-sm, 4px)",
              padding: "var(--space-2)",
              minHeight: 96,
              opacity: inMonth ? 1 : 0.55,
            }}
          >
            <div className="ym-inventory">{key.slice(5)}</div>
            {rows.length === 0 ? (
              <div className="ym-muted" style={{ fontSize: "var(--text-xs)" }}>
                —
              </div>
            ) : (
              rows.map((row) => (
                <div key={row.id} style={{ marginTop: "var(--space-1)" }}>
                  <div style={{ fontSize: "var(--text-xs)" }}>{hhmm(row.run_at)}</div>
                  {entriesFor(row)}
                </div>
              ))
            )}
          </div>
        );
      })}
    </div>
  );
}

function CalendarGrid({
  scale,
  anchor,
  onAnchor,
  items,
  campaignsById,
  onReschedule,
}: {
  scale: Scale;
  anchor: string;
  onAnchor: (next: string) => void;
  items: ScheduleEntryRow[];
  campaignsById: Map<string, CampaignRef>;
  onReschedule: (entry: ScheduleEntryRow) => void;
}) {
  const byDay = useMemo(() => groupByDay(items), [items]);
  const cells = useMemo(() => bucketCells(scale, anchor), [scale, anchor]);
  const step = scale === "day" ? 1 : scale === "week" ? 7 : 30;

  const chip = (row: ScheduleEntryRow) => (
    <div>
      <Badge tone={row.status.toUpperCase() === "FAILED" ? "danger" : "info"} title={row.status}>
        {humanize(row.platform)}
      </Badge>{" "}
      <span className="ym-inventory">{row.id.slice(0, 6)}</span>
    </div>
  );

  return (
    <>
      <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "flex-end", flexWrap: "wrap" }}>
        <Field
          label="Anchor date (UTC)"
          type="date"
          value={anchor}
          onChange={(e) => onAnchor(e.target.value || anchor)}
        />
        <Button variant="ghost" onClick={() => onAnchor(addDaysKey(anchor, -step))}>
          Previous
        </Button>
        <Button variant="ghost" onClick={() => onAnchor(addDaysKey(anchor, step))}>
          Next
        </Button>
        <Button variant="ghost" onClick={() => onAnchor(new Date().toISOString().slice(0, 10))}>
          Today
        </Button>
      </div>
      <p className="ym-hint">
        {scale === "day"
          ? dayLabel(anchor)
          : scale === "week"
            ? `Week of ${dayLabel(cells[0])} → ${dayLabel(cells[cells.length - 1])}`
            : monthLabel(anchor)}{" "}
        · times are UTC, the timezone the ScheduleEntry store is read in.
      </p>
      <DayGrid
        cells={cells}
        byDay={byDay}
        entriesFor={(row) => (
          <div>
            {chip(row)}
            <div className="ym-inventory">
              {row.campaign_id
                ? campaignsById.get(row.campaign_id)?.name ?? row.campaign_id.slice(0, 8)
                : "no campaign"}
            </div>
            <EntryRowActions entry={row} onReschedule={onReschedule} />
          </div>
        )}
      />
    </>
  );
}

/* ==========================================================================
 * Screen
 * ======================================================================= */

export function CalendarView() {
  const { workspaceId, workspace } = useSession();
  const [scale, setScale] = useState<Scale>("week");
  const [anchor, setAnchor] = useState(() => new Date().toISOString().slice(0, 10));
  const [rescheduling, setRescheduling] = useState<ScheduleEntryRow | null>(null);
  const [platformFilter, setPlatformFilter] = useState("");

  const schedule = useWsQuery<ScheduleList>("/calendar");
  const bestTimes = useWsQuery<BestTimes>("/calendar/best-times");
  const windows = useWsQuery<ResponseWindows>(
    `/calendar/response-windows${platformFilter ? `?platform=${encodeURIComponent(platformFilter)}` : ""}`,
  );
  const capacity = useWsQuery<PlannerCalendar>("/planner/calendar?days=30");
  const campaigns = useWsQuery<{ items: CampaignRef[] }>("/campaigns");

  const campaignsById = useMemo(() => {
    const map = new Map<string, CampaignRef>();
    for (const c of campaigns.data?.items ?? []) map.set(c.id, c);
    return map;
  }, [campaigns.data]);

  const rows = useMemo(() => {
    const all = schedule.data?.items ?? [];
    return platformFilter ? all.filter((e) => e.platform === platformFilter) : all;
  }, [schedule.data, platformFilter]);

  const platforms = useMemo(() => {
    const set = new Set<string>();
    for (const r of schedule.data?.items ?? []) set.add(r.platform);
    return [...set].sort();
  }, [schedule.data]);

  /* Entries falling inside the day/week/month grid. A real count of real rows —
     never a computed "expected" figure. */
  const inWindow = useMemo(() => {
    const cells = new Set(bucketCells(scale, anchor));
    return rows.filter((r) => cells.has(utcDayKey(r.run_at))).length;
  }, [rows, scale, anchor]);

  const blocked = useMemo(
    () => rows.filter((r) => !entryIsMutable(r.status)).length,
    [rows],
  );

  const columns: Column<ScheduleEntryRow>[] = [
    { key: "run_at", header: "Run at (UTC)", cell: (r) => r.run_at.replace("Z", " UTC").replace("T", " ") },
    { key: "platform", header: "Platform", cell: (r) => humanize(r.platform) },
    { key: "status", header: "State", cell: (r) => <StatusBadge status={r.status} /> },
    {
      key: "campaign",
      header: "Campaign",
      cell: (r) => {
        if (!r.campaign_id) return <span className="ym-muted">none</span>;
        const c = campaignsById.get(r.campaign_id);
        return c ? (
          <>
            {c.name} <StatusBadge status={c.status} />
          </>
        ) : (
          <span className="ym-muted" title="Not in GET /campaigns">
            {r.campaign_id.slice(0, 8)}
          </span>
        );
      },
    },
    {
      key: "content",
      header: "Content item",
      cell: (r) =>
        r.content_item_id ? (
          r.content_item_id.slice(0, 8)
        ) : (
          <span className="ym-muted" title="No ContentItem has been produced for this entry yet">
            none produced
          </span>
        ),
      hideBelow: "md",
    },
    {
      key: "reschedule",
      header: "Reschedule",
      cell: (r) => <EntryRowActions entry={r} onReschedule={setRescheduling} />,
    },
  ];

  if (!workspaceId) {
    return (
      <>
        <PageHeader title="Editorial Calendar" description="Day, week and month over the canonical schedule." />
        <Panel title="No workspace selected">
          <EmptyState
            title="Nothing scheduled"
            description="The schedule is workspace-scoped. Select a workspace to load its entries."
          />
        </Panel>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Editorial Calendar"
        description={
          workspace?.name
            ? `${workspace.name} — one canonical ScheduleEntry store, read directly.`
            : "One canonical ScheduleEntry store, read directly."
        }
        actions={<Button onClick={schedule.reload}>Refresh</Button>}
      />

      <Tabs
        tabs={[
          { id: "day", label: "Day" },
          { id: "week", label: "Week" },
          { id: "month", label: "Month" },
        ]}
        active={scale}
        onChange={(id) => setScale(id as Scale)}
      />

      <Panel title="At a glance" dense>
        <Grid min={170} gap="sm">
          <StatTile
            label="Entries the backend returned"
            value={schedule.data === null ? undefined : (schedule.data.items ?? []).length}
            unavailable={schedule.data === null}
            hint="PENDING, DISPATCHING, QUEUED and FAILED only"
            source="GET /calendar"
          />
          <StatTile
            label="In this view"
            value={schedule.data === null ? undefined : inWindow}
            unavailable={schedule.data === null}
            source="client-side bucketing of GET /calendar"
          />
          <StatTile
            label="Cannot be rescheduled"
            value={schedule.data === null ? undefined : blocked}
            unavailable={schedule.data === null}
            tone={blocked > 0 ? "neutral" : "success"}
            hint="PENDING and FAILED are the only statuses PATCH accepts"
            source="PATCH /calendar/{entry_id}"
          />
          <StatTile
            label="Measured best-hour data"
            value={bestTimes.data === null ? undefined : bestTimes.data.measured ? "yes" : "no — seed hours"}
            unavailable={bestTimes.data === null}
            tone={bestTimes.data?.measured ? "success" : "warning"}
            hint="Fewer than 3 metric-backed posts means no measured optimum is claimed"
            source="GET /calendar/best-times"
          />
          <StatTile
            label="Scheduling automation"
            value={windows.data === null ? undefined : windows.data.activity_policy === "action_allowed" ? "enabled" : "recommendation only"}
            unavailable={windows.data === null}
            tone={windows.data?.activity_policy === "action_allowed" ? "info" : "neutral"}
            hint="Only the explicit schedule_automation opt-in allows acting on a window"
            source="GET /calendar/response-windows"
          />
          <StatTile
            label="Capacity room (30d)"
            value={
              capacity.data === null || capacity.data.remaining?.shorts === null || capacity.data.remaining?.shorts === undefined
                ? undefined
                : capacity.data.remaining.shorts
            }
            unavailable={
              capacity.data === null ||
              capacity.data.remaining?.shorts === null ||
              capacity.data.remaining?.shorts === undefined
            }
            hint="Unbounded means nothing was declared — not zero"
            source="GET /planner/calendar?days=30"
          />
        </Grid>
      </Panel>

      <Panel title="Schedule" subtitle={`${humanize(scale)} view`} dense>
        <div className="ym-grid ym-grid--md" style={{ gridTemplateColumns: "minmax(160px, 240px) auto", alignItems: "end" }}>
          <Select label="Platform" value={platformFilter} onChange={(e) => setPlatformFilter(e.target.value)}>
            <option value="">All platforms</option>
            {platforms.map((p) => (
              <option key={p} value={p}>
                {humanize(p)}
              </option>
            ))}
          </Select>
          <Button variant="ghost" onClick={() => setPlatformFilter("")} disabled={!platformFilter}>
            Clear
          </Button>
        </div>
        <QueryBoundary query={schedule} skeletonRows={6}>
          {(d) =>
            (d.items ?? []).length === 0 ? (
              <EmptyState
                title="No schedule entry exists"
                description="GET /calendar returns PENDING, DISPATCHING, QUEUED and FAILED entries only. DONE entries disappear once the post exists and CANCELLED is terminal, so an empty list means nothing is pending, in flight or failed."
              />
            ) : rows.length === 0 ? (
              <EmptyState
                title="Nothing on the calendar for this filter"
                description="The backend returned entries, but none match this platform. Clear the filter to see them."
              />
            ) : (
              <>
                <CalendarGrid
                  scale={scale}
                  anchor={anchor}
                  onAnchor={setAnchor}
                  items={rows}
                  campaignsById={campaignsById}
                  onReschedule={setRescheduling}
                />
                <DataTable
                  rows={rows}
                  columns={columns}
                  rowKey={(r) => r.id}
                  caption="Schedule entries"
                  maxHeight={420}
                  empty="No entry in this window"
                  emptyHint="The entries returned by the backend fall outside the selected view. Move the anchor date."
                  onRowClick={setRescheduling}
                />
              </>
            )
          }
        </QueryBoundary>
      </Panel>

      <Panel title="Best publish hours" subtitle="Ranked by average views from measured history — or honestly labelled as a seed." dense>
        <QueryBoundary query={bestTimes} skeletonRows={3}>
          {(d) =>
            d.measured ? (
              <DataTable
                rows={d.items ?? []}
                rowKey={(t) => String(t.hour)}
                caption="Measured best hours"
                empty="No measured hour"
                emptyHint="The response reported measured data but returned no hours."
                columns={[
                  { key: "hour", header: "Hour (UTC)", cell: (t) => hourLabel(t.hour) },
                  { key: "views", header: "Avg views", align: "right", cell: (t) => t.avg_views },
                  { key: "posts", header: "Posts measured", align: "right", cell: (t) => t.posts },
                ]}
              />
            ) : (
              <>
                <div className="ym-notif-item ym-notif-unknown">
                  <div className="ym-notif-title">No measured best time exists</div>
                  <div className="ym-notif-detail">
                    Fewer than three metric-backed published posts, so these are
                    the platform's seed hours. They are listed so the scheduler
                    has somewhere to start, NOT because this workspace has proved
                    them.
                  </div>
                </div>
                <DataTable
                  rows={d.items ?? []}
                  rowKey={(t) => String(t.hour)}
                  caption="Seed hours"
                  empty="No seed hour returned"
                  columns={[
                    { key: "hour", header: "Hour (UTC)", cell: (t) => hourLabel(t.hour) },
                    {
                      key: "views",
                      header: "Avg views",
                      align: "right",
                      cell: (t) => (
                        <span className="ym-muted" title="No measured history behind this hour">
                          UNAVAILABLE
                        </span>
                      ),
                    },
                    { key: "posts", header: "Posts measured", align: "right", cell: (t) => (t.posts > 0 ? t.posts : <span className="ym-muted">0</span>) },
                  ]}
                />
              </>
            )
          }
        </QueryBoundary>
      </Panel>

      <Panel title="Response windows" subtitle="Ranked hours with the reason each one was chosen." dense>
        <QueryBoundary query={windows} skeletonRows={4}>
          {(d) => (
            <>
              <Grid min={170} gap="sm">
                <StatTile
                  label="Measured"
                  value={d.measured ? "yes" : "no — heuristic seed"}
                  tone={d.measured ? "success" : "warning"}
                  source="GET /calendar/response-windows"
                />
                <StatTile
                  label="Audience activity"
                  value={d.activity ? "available" : "unavailable"}
                  tone={d.activity ? "success" : "unknown"}
                  hint="Needs enough interactions to bucket by hour of day."
                  source="audience_activity"
                />
                <StatTile
                  label="Busy hours"
                  value={(d.avoid_hours ?? []).length
              ? (d.avoid_hours ?? []).map(hourLabel).join(", ")
              : "none recorded"}
                  hint="Hours already holding active campaign schedule entries."
                  source="avoid_hours"
                />
                <StatTile
                  label="Policy"
                  value={humanize(d.activity_policy)}
                  tone={d.activity_policy === "action_allowed" ? "info" : "neutral"}
                  source="activity_policy"
                />
              </Grid>
              <DataTable
                rows={d.items ?? []}
                rowKey={(w) => String(w.hour)}
                caption="Ranked response windows"
                maxHeight={360}
                empty="No window returned"
                emptyHint="The scheduler produced no candidate hours for this workspace."
                columns={[
                  { key: "hour", header: "Hour (UTC)", cell: (w) => hourLabel(w.hour) },
                  { key: "reason", header: "Why", cell: (w) => <span className="ym-notif-detail">{w.reason}</span> },
                  {
                    key: "sources",
                    header: "Sources",
                    cell: (w) => (
                      <span>
                        {w.sources.map((s) => (
                          <Badge key={s} tone={s === "measured_performance" ? "success" : "info"}>
                            {humanize(s)}
                          </Badge>
                        ))}
                      </span>
                    ),
                  },
                ]}
              />
              {(d.notes ?? []).length ? (
                <ul>
                  {(d.notes ?? []).map((n, i) => (
                    <li key={i} className="ym-notif-detail">
                      {n}
                    </li>
                  ))}
                </ul>
              ) : null}
            </>
          )}
        </QueryBoundary>
      </Panel>

      <RescheduleDialog
        entry={rescheduling}
        onClose={() => setRescheduling(null)}
        onDone={schedule.reload}
      />
    </>
  );
}

export default CalendarView;