import { useCallback, useEffect, useMemo, useState } from "react";
import { wsApi } from "../lib/api";
import { Badge, Card, EmptyState, Field, Modal, PageHeader, Skeleton, useToast } from "../components/ui";

const PLATFORMS = ["youtube", "tiktok", "facebook", "instagram"];
const PLATFORM_COLORS: Record<string, string> = {
  youtube: "#ef4444", tiktok: "#22d3ee", facebook: "#3b82f6", instagram: "#e1306c",
};

type Entry = { id: string; platform: string; run_at: string; content_item_id: string | null; status?: string };

const STATUS_COLOR: Record<string, string> = {
  DISPATCHING: "#f59e0b", QUEUED: "#3b82f6", FAILED: "#ef4444",
};

export default function CalendarPage() {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [cursor, setCursor] = useState(() => {
    const d = new Date();
    return new Date(d.getFullYear(), d.getMonth(), 1);
  });
  const [selected, setSelected] = useState<Entry | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const { push } = useToast();

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const r = await wsApi.get("/calendar");
      setEntries(r.items ?? []);
    } catch (e: any) { setError(e.message); } finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function reschedule(entry: Entry, date: Date) {
    const st = entry.status ?? "PENDING";
    if (st !== "PENDING" && st !== "FAILED") {
      push("error", `${st.toLowerCase()} entries cannot be rescheduled`);
      return;
    }
    await wsApi.patch(`/calendar/${entry.id}`, { run_at: date.toISOString() });
    push("success", st === "FAILED" ? "Retry scheduled" : "Rescheduled");
    load();
  }

  async function cancel(entry: Entry) {
    try {
      await wsApi.del(`/calendar/${entry.id}`);
      push("info", "Schedule cancelled");
      setSelected(null);
      load();
    } catch (e: any) { push("error", e.message); }
  }

  const grid = useMemo(() => buildMonthGrid(cursor), [cursor]);
  const byDay = useMemo(() => {
    const map: Record<string, Entry[]> = {};
    for (const e of entries) {
      const key = new Date(e.run_at).toDateString();
      (map[key] ??= []).push(e);
    }
    return map;
  }, [entries]);

  return (
    <div className="space-y-5">
      <PageHeader
        title="Calendar"
        subtitle="Scheduled publications. Drag entries between days to reschedule."
        actions={
          <>
            <div className="flex items-center gap-1">
              <button className="btn-outline !px-2.5" onClick={() => setCursor(new Date(cursor.getFullYear(), cursor.getMonth() - 1, 1))} aria-label="Previous month">‹</button>
              <span className="text-sm font-medium w-32 text-center">{cursor.toLocaleString(undefined, { month: "long", year: "numeric" })}</span>
              <button className="btn-outline !px-2.5" onClick={() => setCursor(new Date(cursor.getFullYear(), cursor.getMonth() + 1, 1))} aria-label="Next month">›</button>
            </div>
            <button className="btn-primary" onClick={() => setShowCreate(true)}>+ Schedule post</button>
          </>
        }
      />

      <div className="flex gap-3 flex-wrap">
        {PLATFORMS.map((p) => (
          <span key={p} className="flex items-center gap-1.5 text-[11px]" style={{ color: "var(--text-muted)" }}>
            <span className="w-2 h-2 rounded-full inline-block" style={{ background: PLATFORM_COLORS[p] }} />
            {p}
          </span>
        ))}
      </div>

      {loading ? <Skeleton rows={6} height={80} /> : error ? (
        <Card><p className="text-sm text-red-500">{error}</p></Card>
      ) : (
        <Card pad={false} className="overflow-hidden">
          <div className="grid grid-cols-7 border-b" style={{ borderColor: "var(--border)" }}>
            {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d) => (
              <div key={d} className="px-2 py-2 text-[10px] font-semibold uppercase tracking-wider text-center" style={{ color: "var(--text-muted)" }}>{d}</div>
            ))}
          </div>
          <div className="grid grid-cols-7">
            {grid.map((day, i) => (
              <div
                key={i}
                className={`min-h-[92px] border-b border-r p-1.5 ${day.isCurrentMonth ? "" : "opacity-40"} ${day.isToday ? "bg-emerald-500/5" : ""}`}
                style={{ borderColor: "var(--border)" }}
                onDragOver={(e) => day.date && e.preventDefault()}
                onDrop={(e) => {
                  const id = e.dataTransfer.getData("text/entry-id");
                  const entry = entries.find((x) => x.id === id);
                  if (entry && day.date) reschedule(entry, day.date);
                }}
              >
                {day.date && (
                  <>
                    <p className={`text-[11px] mb-1 ${day.isToday ? "font-bold text-emerald-500" : ""}`} style={{ color: day.isToday ? undefined : "var(--text-muted)" }}>
                      {day.date.getDate()}
                    </p>
                    <div className="space-y-1">
                      {(byDay[day.date.toDateString()] ?? []).map((e) => (
                        <button
                          key={e.id}
                          draggable
                          onDragStart={(ev) => ev.dataTransfer.setData("text/entry-id", e.id)}
                          onClick={() => setSelected(e)}
                          className="w-full text-left text-[10px] px-1.5 py-0.5 rounded truncate text-white"
                          style={{ background: PLATFORM_COLORS[e.platform] ?? "#71717a" }}
                          title={`${e.platform} — click for options, drag to reschedule`}
                        >
                          {new Date(e.run_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} {e.platform}
                          {e.status && e.status !== "PENDING" && (
                            <span className="ml-1 font-semibold" style={{ color: STATUS_COLOR[e.status] ?? "#fff" }}>{e.status.toLowerCase()}</span>
                          )}
                        </button>
                      ))}
                    </div>
                  </>
                )}
              </div>
            ))}
          </div>
        </Card>
      )}

      {!loading && entries.length === 0 && !error && (
        <EmptyState icon="▤" title="Nothing scheduled" hint="The autopilot schedules posts after QC approval; or create one with the Composer." />
      )}

      <Modal open={!!selected} onClose={() => setSelected(null)}>
        {selected && (
          <div className="space-y-4">
            <h3 className="font-semibold">Scheduled post</h3>
            <p className="text-sm capitalize">
              {selected.platform}
              {selected.status && selected.status !== "PENDING" ? ` · ${selected.status.toLowerCase()}` : ""}
            </p>
            <p className="text-[13px]" style={{ color: "var(--text-muted)" }}>{new Date(selected.run_at).toLocaleString()}</p>
            {selected.status === "FAILED" && <p className="text-[12px] text-red-500">Publishing failed after all retries. Drag this entry to a new day to retry.</p>}
            <div className="flex gap-2 pt-2">
              <button className="btn-danger" onClick={() => cancel(selected)}>Cancel schedule</button>
              <button className="btn-outline ml-auto" onClick={() => setSelected(null)}>Close</button>
            </div>
          </div>
        )}
      </Modal>

      <CreateModal open={showCreate} onClose={() => { setShowCreate(false); load(); }} />
    </div>
  );
}

function CreateModal({ open, onClose }: any) {
  const [platform, setPlatform] = useState("youtube");
  const [runAt, setRunAt] = useState(() => new Date(Date.now() + 3600_000).toISOString().slice(0, 16));
  const [contentId, setContentId] = useState("");
  const [contentItems, setContentItems] = useState<any[]>([]);
  const { push } = useToast();

  useEffect(() => {
    if (open) wsApi.get("/content?limit=50").then((r) => setContentItems(r.items ?? [])).catch(() => {});
  }, [open]);

  async function create() {
    try {
      await wsApi.post("/calendar", {
        platform,
        run_at: new Date(runAt).toISOString(),
        content_item_id: contentId,
      });
      push("success", "Post scheduled");
      onClose();
    } catch (e: any) { push("error", e.message); }
  }

  return (
    <Modal open={open} onClose={onClose}>
      <h3 className="font-semibold mb-4">Schedule a post</h3>
      <div className="space-y-4">
        <Field label="Platform">
          <select className="select" value={platform} onChange={(e) => setPlatform(e.target.value)}>
            {PLATFORMS.map((p) => <option key={p}>{p}</option>)}
          </select>
        </Field>
        <Field label="Date & time">
          <input className="input" type="datetime-local" value={runAt} onChange={(e) => setRunAt(e.target.value)} />
        </Field>
        <Field label="Content">
          <select className="select" value={contentId} onChange={(e) => setContentId(e.target.value)} required>
            <option value="" disabled>— select content —</option>
            {contentItems.map((c) => (
              <option key={c.id} value={c.id}>{c.topic.slice(0, 70)} ({c.status.toLowerCase()})</option>
            ))}
          </select>
        </Field>
        <div className="flex justify-end gap-2 pt-1">
          <button className="btn-outline" onClick={onClose}>Cancel</button>
          <button className="btn-primary" onClick={create} disabled={!runAt || !contentId}>Schedule</button>
        </div>
      </div>
    </Modal>
  );
}

function buildMonthGrid(cursor: Date) {
  const first = new Date(cursor.getFullYear(), cursor.getMonth(), 1);
  const startOffset = (first.getDay() + 6) % 7; // Monday-first
  const today = new Date();
  const cells: { date: Date | null; isCurrentMonth: boolean; isToday: boolean }[] = [];
  for (let i = 0; i < startOffset; i++) cells.push({ date: null, isCurrentMonth: false, isToday: false });
  const d = new Date(first);
  while (d.getMonth() === cursor.getMonth()) {
    cells.push({
      date: new Date(d),
      isCurrentMonth: true,
      isToday: d.toDateString() === today.toDateString(),
    });
    d.setDate(d.getDate() + 1);
  }
  while (cells.length % 7 !== 0) cells.push({ date: null, isCurrentMonth: false, isToday: false });
  return cells;
}
