import { useState } from "react";
import { wsApi, api } from "../lib/api";
import { useFetch, useInterval, useActivityFeed } from "../hooks/hooks";
import {
  Badge, Card, FeedList, Modal, PageHeader, Progress, Stat, WhyPanel, statusTone, toast,
} from "../components/ui";
import { fmtUSD } from "../lib/format";

export default function CommandCenter() {
  const status = useFetch(() => wsApi.get("/autopilot/status"), []);
  const doctor = useFetch(() => api("GET", "/system/doctor"), []);
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
  const doc: any = doctor.data;
  const blocked: string[] = doc?.doctor?.blocking_failed ?? doc?.blocking_failures ?? [];
  const checks: any[] = doc?.checks ?? [];

  async function act(kind: string, path: string, body?: any) {
    setBusy(kind);
    try {
      const r = await wsApi.post(path, body ?? {});
      status.reload();
      doctor.reload();
      if (kind === "start") toast("Autopilot started", "success");
      if (kind === "stop") toast("Autopilot stopping — running steps finish", "warning");
      return r;
    } catch (e: any) {
      toast(e.message, "error", "Action failed");
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
  const spendPct = cost?.daily_budget_usd ? Math.min(100, ((cost?.spent_last_24h_usd ?? 0) / cost.daily_budget_usd) * 100) : 0;

  return (
    <div className="space-y-5">
      <PageHeader
        title="Command Center"
        subtitle="START once — FIND → SCORE → PRODUCE → PUBLISH → LEARN runs itself inside budget and quality guardrails."
      />

      {/* Hero control deck */}
      <div className="card overflow-hidden" style={{ padding: 0 }}>
        <div className="px-6 pt-6 pb-5 flex flex-wrap items-center gap-5">
          <span className="grid place-items-center w-14 h-14 rounded-2xl text-[26px]"
            style={{
              background: running ? "linear-gradient(135deg, var(--accent-bright), var(--accent-deep))" : "var(--bg-subtle)",
              boxShadow: running ? "0 0 28px -4px var(--accent-glow)" : undefined,
              border: running ? undefined : "var(--seam)",
            }}>
            {running ? "◉" : "○"}
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2.5 flex-wrap">
              <span className="text-[20px] font-bold tracking-tight">{st?.state ?? "…"}</span>
              <Badge tone={running ? "success" : "muted"}>
                {st?.cycles_completed ? `${st.cycles_completed} cycles` : "idle"}
              </Badge>
              <Badge tone={doc?.status === "ready" ? "success" : "warning"}>
                {doc ? `Doctor: ${doc.status}` : "probing…"}
              </Badge>
            </div>
            <div className="mt-2.5 max-w-[420px]">
              <div className="flex justify-between text-[11.5px] mb-1.5" style={{ color: "var(--text-muted)" }}>
                <span>24h spend {fmtUSD(cost?.spent_last_24h_usd)}</span>
                <span>${(cost?.daily_budget_usd ?? 0).toFixed(2)} budget</span>
              </div>
              <Progress value={spendPct} />
            </div>
          </div>
          <div className="flex gap-2 flex-wrap">
            {!running ? (
              <button className="btn-primary !px-7 !py-3 !text-[15px]" disabled={busy === "start"} onClick={start}>
                {busy === "start" ? "…" : "▶ START"}
              </button>
            ) : (
              <>
                <button className="btn-outline" disabled={busy === "pause"} onClick={() => act("pause", "/autopilot/pause")}>⏸ Pause</button>
                <button className="btn-outline" disabled={busy === "once"} onClick={() => act("once", "/autopilot/run-one-cycle")}>1 cycle</button>
                <button className="btn-danger" disabled={busy === "stop"} onClick={() => act("stop", "/autopilot/stop")}>■ Stop</button>
              </>
            )}
          </div>
        </div>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <Stat label="Published posts" value={ana?.posts_published ?? 0} hint={`${ana?.totals?.views ?? 0} total views`} />
        <Stat label="Total spend" value={fmtUSD(ana?.cost_total_usd)} hint={ana?.mock_analytics ? "simulated analytics" : "measured spend"} />
        <Stat label="Remaining today" value={fmtUSD(cost?.remaining_usd)} hint={cost?.within_budget === false ? "budget exhausted" : "within budget"} />
        <Stat label="Blocking checks" value={blocked.length ? blocked.length : "0"}
          hint={blocked.length ? blocked.join(", ") : "all dependencies verified"} />
      </div>

      {/* Production gate */}
      <Card>
        <div className="flex items-center justify-between mb-2">
          <b className="text-[14px]">Production gate</b>
          <button className="btn-ghost !text-xs" onClick={() => doctor.reload()}>Re-check</button>
        </div>
        {(["Built-in", "Configured"] as const).map((group) => {
          const tier = group === "Built-in" ? 0 : 1;
          const rows = checks.filter((c: any) => (c.tier ?? 1) === tier);
          if (!rows.length) return null;
          return (
            <div key={group} className="mb-1">
              <div className="panel-label mt-2 mb-1">{group}</div>
              <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-x-6">
                {rows.map((c: any) => (
                  <div key={c.id} className="py-1.5 text-[13px]" style={{ borderBottom: "var(--seam)" }}>
                    <div className="flex justify-between gap-3">
                      <span className="capitalize" style={{ color: "var(--text-muted)" }}>{c.id.replace(/_/g, " ")}</span>
                      <span className="text-right truncate" title={c.detail}
                        style={{ color: c.status === "passed" ? "var(--accent)" : c.blocking ? "var(--danger)" : "var(--warn)" }}>
                        {c.status === "passed" ? "●" : "●"} {c.detail?.slice(0, 60)}
                      </span>
                    </div>
                    {c.status !== "passed" && c.remediation && (
                      <div className="font-mono text-[11.5px] mt-0.5 truncate" title={c.remediation} style={{ color: "var(--text-faint)" }}>
                        ↳ {c.remediation}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          );
        })}
        {!checks.length && <span className="text-[13px]" style={{ color: "var(--text-faint)" }}>Probing…</span>}
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
