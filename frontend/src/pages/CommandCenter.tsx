import { useState } from "react";
import { wsApi, api } from "../lib/api";
import { useFetch, useInterval, useActivityFeed } from "../hooks/hooks";
import { Badge, Card, FeedList, Modal, PageHeader, Stat, WhyPanel, statusTone } from "../components/ui";
import { fmtUSD } from "../lib/format";

export default function CommandCenter() {
  const status = useFetch(() => wsApi.get("/autopilot/status"), []);
  const readiness = useFetch(() => api("GET", "/system/readiness"), []);
  const costs = useFetch(() => wsApi.get("/costs"), []);
  const analytics = useFetch(() => wsApi.get("/analytics/overview"), []);
  const patterns = useFetch(() => wsApi.get("/analytics/patterns"), []);
  const cycles = useFetch(() => wsApi.get("/cycles?limit=5"), []);
  const [gateOpen, setGateOpen] = useState(false);
  const [override, setOverride] = useState(false);
  const [busy, setBusy] = useState("");
  const feed = useActivityFeed(() => {
    status.reload();
    costs.reload();
  });

  useInterval(() => {
    status.reload();
    costs.reload();
  }, 10000);

  const st: any = status.data;
  const running = st?.state === "RUNNING" || st?.state === "STARTING";
  const blocked: string[] = (readiness.data as any)?.blocking_failures ?? [];
  const checks: any[] = (readiness.data as any)?.checks ?? [];

  async function act(kind: string, path: string, body?: any) {
    setBusy(kind);
    try {
      await wsApi.post(path, body ?? {});
      status.reload();
      readiness.reload();
    } catch (e: any) {
      alert(e.message);
    } finally {
      setBusy("");
    }
  }

  function start() {
    if (blocked.length && !override) {
      setGateOpen(true);
      return;
    }
    act("start", "/autopilot/start", { override_readiness: override });
  }

  const cost: any = costs.data;
  const ana: any = analytics.data;
  const topPatterns: any[] = ((patterns.data as any)?.items ?? []).filter((p: any) => p.active).slice(0, 3);

  return (
    <div className="space-y-5">
      <PageHeader
        title="Command Center"
        subtitle="START once — FIND → SCORE → PRODUCE → PUBLISH → LEARN runs itself inside budget and quality guardrails."
        actions={
          <>
            {!running ? (
              <button className="btn-primary !px-6 !py-2.5 !text-[15px]" disabled={busy === "start"} onClick={start}>
                {busy === "start" ? "…" : "▶ START"}
              </button>
            ) : (
              <>
                <button className="btn-outline" disabled={busy === "pause"} onClick={() => act("pause", "/autopilot/pause")}>⏸ Pause</button>
                <button className="btn-outline" disabled={busy === "once"} onClick={() => act("once", "/autopilot/run-one-cycle")}>1 cycle</button>
                <button className="btn-danger" disabled={busy === "stop"} onClick={() => act("stop", "/autopilot/stop")}>■ Stop</button>
              </>
            )}
          </>
        }
      />

      {/* Status strip */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <Stat label="Autopilot" value={<span style={{ color: running ? "var(--accent)" : undefined }}>{st?.state ?? "…"}</span>}
          hint={st ? `${st.cycles_completed ?? 0} cycles completed` : undefined} />
        <Stat label="Spent (24h)" value={fmtUSD(cost?.spent_last_24h_usd)}
          hint={`$${(cost?.daily_budget_usd ?? 0).toFixed(2)} daily budget · $${(cost?.remaining_usd ?? 0).toFixed(2)} left`} />
        <Stat label="Published posts" value={ana?.posts_published ?? 0} hint={`${ana?.totals?.views ?? 0} total views`} />
        <Stat label="Readiness" value={(readiness.data as any)?.status === "ready" ? "Ready" : "Blocked"}
          hint={blocked.length ? `blocked: ${blocked.join(", ")}` : "all dependencies verified"} />
      </div>

      {/* Readiness gate */}
      <Card>
        <div className="flex items-center justify-between mb-2">
          <b className="text-[14px]">Production gate</b>
          <button className="btn-ghost !text-xs" onClick={() => readiness.reload()}>Re-check</button>
        </div>
        <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-x-6">
          {checks.map((c: any) => (
            <div key={c.id} className="flex justify-between gap-3 py-1.5 text-[13px]" style={{ borderBottom: "var(--seam)" }}>
              <span className="capitalize" style={{ color: "var(--text-muted)" }}>{c.id.replace(/_/g, " ")}</span>
              <span className="text-right truncate" title={c.detail} style={{ color: c.status === "passed" ? "var(--accent)" : c.blocking ? "var(--danger)" : "var(--warn)" }}>
                {c.status === "passed" ? "●" : "●"} {c.detail?.slice(0, 60)}
              </span>
            </div>
          ))}
          {!checks.length && <span className="text-[13px]" style={{ color: "var(--text-faint)" }}>Probing…</span>}
        </div>
      </Card>

      <div className="grid lg:grid-cols-5 gap-4">
        {/* Current cycle + WHY */}
        <Card className="lg:col-span-3">
          <b className="text-[14px]">Latest decision</b>
          <div className="mt-3 space-y-3">
            {((cycles.data as any)?.items ?? []).slice(0, 1).map((c: any) => (
              <div key={c.id}>
                <div className="flex items-center gap-2 mb-2 flex-wrap">
                  <Badge tone={statusTone(c.status)}>{c.status}</Badge>
                  <span className="text-[13px] font-medium">Cycle #{c.number} · {c.stage}</span>
                  {c.topic && <span className="text-[12.5px] truncate" style={{ color: "var(--text-muted)" }}>{c.topic}</span>}
                </div>
                <CycleWhy cycleId={c.id} />
              </div>
            ))}
            {!((cycles.data as any)?.items ?? []).length && (
              <div className="text-[13px]" style={{ color: "var(--text-muted)" }}>No cycles yet. Press START to run FIND → … → LEARN.</div>
            )}
          </div>
          {topPatterns.length > 0 && (
            <div className="mt-4 pt-3" style={{ borderTop: "var(--seam)" }}>
              <div className="panel-label mb-2">Learning highlights</div>
              {topPatterns.map((p: any) => (
                <div key={p.pattern_key} className="flex justify-between text-[12.5px] py-1">
                  <span>{p.description}</span>
                  <Badge tone="success">+{Number(p.improvement_pct).toFixed(0)}% · n={p.sample_size}</Badge>
                </div>
              ))}
            </div>
          )}
        </Card>

        {/* Live feed */}
        <Card className="lg:col-span-2">
          <b className="text-[14px]">Live activity</b>
          <div className="mt-2"><FeedList items={feed} limit={18} /></div>
        </Card>
      </div>

      {/* Gate modal */}
      <Modal open={gateOpen} onClose={() => setGateOpen(false)} title="Readiness blocked">
        <p className="text-[13.5px] mb-3" style={{ color: "var(--text-muted)" }}>
          These blocking checks fail: <b>{blocked.join(", ")}</b>. Starting anyway is audited and may burn budget on mock renders.
        </p>
        <label className="flex items-center gap-2 text-[13.5px] mb-4">
          <input type="checkbox" checked={override} onChange={(e) => setOverride(e.target.checked)} />
          I understand — override the gate for this start
        </label>
        <div className="flex gap-2 justify-end">
          <button className="btn-outline" onClick={() => setGateOpen(false)}>Cancel</button>
          <button className="btn-danger" disabled={!override || busy === "start"} onClick={() => { setGateOpen(false); start(); }}>
            Start with override
          </button>
        </div>
      </Modal>
    </div>
  );
}

function CycleWhy({ cycleId }: { cycleId: string }) {
  const d = useFetch(() => wsApi.get(`/cycles/${cycleId}`), [cycleId]);
  const summary: any = (d.data as any);
  const why = summary?.decision?.why ?? summary?.decision;
  if (d.loading) return <div className="text-[12.5px]" style={{ color: "var(--text-faint)" }}>Loading decision…</div>;
  if (!why) return <div className="text-[12.5px]" style={{ color: "var(--text-faint)" }}>No decision recorded for this cycle.</div>;
  return <WhyPanel why={why} />;
}
