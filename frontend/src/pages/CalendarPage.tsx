import { useMemo, useState } from "react";
import { wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Badge, Card, PageHeader, Section, statusTone } from "../components/ui";
import { platformLabel } from "../lib/format";

function monthGrid(year: number, month: number): (Date | null)[][] {
  const first = new Date(year, month, 1);
  const startDay = first.getDay();
  const days = new Date(year, month + 1, 0).getDate();
  const cells: (Date | null)[] = [...Array(startDay).fill(null)];
  for (let d = 1; d <= days; d++) cells.push(new Date(year, month, d));
  while (cells.length % 7) cells.push(null);
  const weeks: (Date | null)[][] = [];
  for (let i = 0; i < cells.length; i += 7) weeks.push(cells.slice(i, i + 7));
  return weeks;
}

export default function CalendarPage() {
  const now = new Date();
  const [ym, setYm] = useState({ y: now.getFullYear(), m: now.getMonth() });
  const sched = useFetch(() => wsApi.get("/calendar"), []);
  const best = useFetch(() => wsApi.get("/calendar/best-times"), []);
  const [cancelId, setCancelId] = useState("");
  const [planning, setPlanning] = useState(false);

  const items: any[] = (sched.data as any)?.items ?? [];
  const byDay = useMemo(() => {
    const map: Record<string, any[]> = {};
    for (const e of items) {
      const d = new Date(e.run_at);
      const key = `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
      (map[key] ??= []).push(e);
    }
    return map;
  }, [items]);

  async function autoFill() {
    setPlanning(true);
    try {
      const r = await wsApi.post("/calendar/plan", { days: 7 });
      alert(r.summary ?? `Scheduled ${(r.created ?? []).length} publish(es)`);
      sched.reload();
    } catch (e: any) {
      alert(e.message);
    } finally {
      setPlanning(false);
    }
  }

  async function cancel(id: string) {
    setCancelId(id);
    try {
      await wsApi.del(`/calendar/${id}`);
      sched.reload();
    } catch (e: any) {
      alert(e.message);
    } finally {
      setCancelId("");
    }
  }

  const bestItems: any[] = (best.data as any)?.items ?? [];
  const measured = (best.data as any)?.measured;

  return (
    <div className="space-y-4">
      <PageHeader title="Calendar" subtitle="Scheduled publishes. The sweep dispatches due entries with platform-native scheduling."
        actions={
          <>
            <button className="btn-primary !text-xs" disabled={planning} onClick={autoFill}>
              {planning ? "Planning…" : "✨ Auto-fill week"}
            </button>
            <button className="btn-outline !text-xs" onClick={() => setYm({ y: ym.m === 0 ? ym.y - 1 : ym.y, m: (ym.m + 11) % 12 })}>←</button>
            <b className="text-[14px]">{new Date(ym.y, ym.m).toLocaleString(undefined, { month: "long", year: "numeric" })}</b>
            <button className="btn-outline !text-xs" onClick={() => setYm({ y: ym.m === 11 ? ym.y + 1 : ym.y, m: (ym.m + 1) % 12 })}>→</button>
          </>
        } />

      <div className="grid lg:grid-cols-4 gap-4">
        <Card className="lg:col-span-3" pad={false}>
          <div className="grid grid-cols-7 text-center text-[11px] font-semibold uppercase tracking-wider py-2" style={{ color: "var(--text-faint)", borderBottom: "var(--seam)" }}>
            {["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].map((d) => <div key={d}>{d}</div>)}
          </div>
          {monthGrid(ym.y, ym.m).map((week, wi) => (
            <div key={wi} className="grid grid-cols-7" style={{ borderBottom: wi < monthGrid(ym.y, ym.m).length - 1 ? "var(--seam)" : "none" }}>
              {week.map((day, di) => {
                const key = day ? `${day.getFullYear()}-${day.getMonth()}-${day.getDate()}` : "";
                const entries = key ? byDay[key] ?? [] : [];
                const today = day && day.toDateString() === new Date().toDateString();
                return (
                  <div key={di} className="min-h-[86px] p-1.5 text-[11.5px]" style={{ borderRight: di < 6 ? "var(--seam)" : "none", background: today ? "var(--accent-dim)" : undefined }}>
                    {day && <div className="font-semibold mb-1" style={{ color: today ? "var(--accent)" : undefined }}>{day.getDate()}</div>}
                    {entries.map((e: any) => (
                      <div key={e.id} className="rounded px-1.5 py-0.5 mb-1 truncate" style={{ background: "var(--bg-subtle)" }} title={`${e.platform} · ${e.status}`}>
                        <span style={{ color: "var(--info)" }}>{platformLabel(e.platform)}</span> · {e.status}
                      </div>
                    ))}
                  </div>
                );
              })}
            </div>
          ))}
        </Card>

        <div className="space-y-4">
          <Card>
            <b className="text-[13.5px]">Best hours to publish</b>
            {!measured && <div className="text-[12px] mt-1" style={{ color: "var(--text-faint)" }}>Generic defaults — needs 3+ measured posts.</div>}
            <div className="mt-2 space-y-1">
              {bestItems.map((b: any) => (
                <div key={b.hour} className="flex justify-between text-[12.5px] font-mono">
                  <span>{String(b.hour).padStart(2, "0")}:00</span>
                  <span style={{ color: "var(--text-muted)" }}>{b.avg_views} avg · {b.posts} posts</span>
                </div>
              ))}
            </div>
          </Card>
          <Card>
            <b className="text-[13.5px]">Queue ({items.length})</b>
            <Section data={items} loading={sched.loading} error={sched.error} onRetry={sched.reload} empty="Nothing scheduled" emptyHint="Schedule videos from any Studio item.">
              {(list) => (
                <div className="mt-2 space-y-2">
                  {list.map((e: any) => (
                    <div key={e.id} className="flex items-center gap-2 text-[12.5px]">
                      <Badge tone={statusTone(e.status)}>{e.status}</Badge>
                      <span>{platformLabel(e.platform)}</span>
                      <span className="font-mono" style={{ color: "var(--text-muted)" }}>{new Date(e.run_at).toLocaleString()}</span>
                      <button className="btn-ghost !text-[11px] !py-0.5 ml-auto" disabled={cancelId === e.id}
                        onClick={() => cancel(e.id)}>Cancel</button>
                    </div>
                  ))}
                </div>
              )}
            </Section>
          </Card>
        </div>
      </div>
    </div>
  );
}
