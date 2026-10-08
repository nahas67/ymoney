/* Automation — the Autopilot loop's missing control surface.
 *
 * Endpoint map (every control below names a real route; nothing is invented):
 *
 *   Loop controls  GET  /autopilot/status      (viewer)
 *                  POST /autopilot/start       (admin, {mode, cycles_target,
 *                             override_readiness}; 409 when blocked or busy)
 *                  POST /autopilot/stop        (admin, graceful: steps finish)
 *                  POST /autopilot/pause       (admin, RUNNING only)
 *                  POST /autopilot/resume      (admin, PAUSED only)
 *                  POST /autopilot/run-one-cycle (admin, one FIND→…→LEARN cycle)
 *   Decision       GET  /decision              (viewer, read-only NEXT BEST
 *                             ACTION with WHY: reasons / factors / evidence)
 *   Agent policy   GET  /agents/config         (viewer)
 *                  PUT  /agents/config/{key}   (admin, {enabled, model,
 *                             timeout_seconds, cost_limit_usd})
 *   Recent cycles  GET  /cycles                (viewer, read-only)
 *
 * DELIBERATELY NOT HERE:
 *
 *   - Circuit-breaker / auto-pause tuning. The engine auto-pauses on repeated
 *     failures (decision.record_auto_pause) and the thresholds live in the
 *     safety policy (PUT /safety) owned by Settings → Budgets & Safety. No
 *     route exposes them here, so this screen surfaces the loop STATE and the
 *     last error instead of inventing dials.
 *   - Readiness override is a single explicit checkbox on the start form, never
 *     a default: starting past a failed readiness gate spends real budget.
 *   - Optimistic UI is refused for every control. Each mutation is pessimistic
 *     (pending → server confirmation → status reload), so the loop state on
 *     screen is always the server's, never a guess.
 */

import { useMemo, useState } from "react";
import {
  Badge,
  Button,
  DataTable,
  DestructiveButton,
  EmptyState,
  Field,
  Grid,
  Money,
  PageHeader,
  Panel,
  QueryBoundary,
  Select,
  StatTile,
  Toggle,
  humanize,
  toneForStatus,
  type Column,
} from "../../design-system/primitives";
import { useMutation, useWsQuery } from "../../api/queries";
import { wsApi } from "../../lib/api";
import { useSession } from "../../state/session";

/* Component-scoped styles. Tokens only, no colour literals; the press
 * transition sits in the 80–120ms band (100ms) with a reduced-motion opt-out.
 * Dense tables scroll horizontally inside their panel (DataTable's own
 * .ym-table-scroll), so this file only wraps the control clusters. */

/* ==========================================================================
 * Response shapes (server-shaped, nothing invented)
 * ======================================================================= */

/* `autopilot.get_autopilot_status`. The idle shape is `{state: IDLE,
 * cycles_completed: 0}` — every other field is absent, never zeroed. */
export type AutopilotStatus = {
  state: string;
  mode?: string | null;
  cycles_completed: number;
  cycles_target?: number | null;
  scheduled_start_at?: string | null;
  scheduled_stop_at?: string | null;
  current_cycle?: {
    id: string;
    number: number;
    stage: string;
    status: string;
  } | null;
  queued_jobs?: number | null;
  config?: Record<string, unknown>;
  last_error?: string;
};

/* `decision.decide_next_best_action().why()`. */
export type DecisionPreview = {
  action: string;
  opportunity_id?: string | null;
  topic?: string | null;
  score?: number | null;
  confidence?: number | null;
  reasons: string[];
  factors: { name: string; value: number; contribution: number; detail: string }[];
  evidence: string[];
};

/* `agents_router.list_agent_configs`. `prompt_override` is never returned. */
export type AgentPolicyRow = {
  key: string;
  title: string;
  description: string;
  enabled: boolean;
  model: string;
  timeout_seconds: number;
  cost_limit_usd: number | null;
};

/* `cycles_router.list_cycles`. */
export type CycleRow = {
  id: string;
  number: number;
  stage: string;
  status: string;
  cost_usd: number | null;
  topic: string | null;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
};

/* ==========================================================================
 * Helpers
 * ======================================================================= */

/* Any admin-role capability. The backend derives capabilities from the role, so
 * holding ANY admin-gated one is the affordance signal for "this operator is an
 * admin". The backend stays authoritative: a wrong guess renders as a 403,
 * which QueryBoundary reports as a refusal, never as breakage. */
const ADMIN_CAPABILITIES = ["publish.approve", "publish.execute", "brand.manage", "providers.manage"];

function useAdminGate(): { allowed: boolean; reason: string | null } {
  const { capabilities, capabilitiesKnown } = useSession();
  const allowed = capabilitiesKnown && ADMIN_CAPABILITIES.some((c) => capabilities.includes(c));
  return {
    allowed,
    reason: allowed
      ? null
      : capabilitiesKnown
        ? "Requires an admin role (owner or admin). The server enforces this — the controls below are disabled."
        : "Checking admin permissions. Write controls remain disabled until the role is confirmed.",
  };
}

function when(value: string | null | undefined) {
  return value ? (
    value.replace("Z", " UTC").replace("T", " ")
  ) : (
    <span className="ym-muted">—</span>
  );
}

/* Pause/stop gates, derived from the server's state — never asserted by hand.
 * The backend re-checks (409 when not running / not paused / no active run),
 * so a stale screen degrades to a reported refusal, not a silent no-op. */
function gatesFor(state: string | undefined) {
  const s = (state ?? "").toUpperCase();
  const active = s === "STARTING" || s === "RUNNING" || s === "PAUSED" || s === "STOPPING";
  return {
    active,
    canStart: s === "" || s === "IDLE" || s === "STOPPED" || s === "FAILED",
    canPause: s === "RUNNING",
    canResume: s === "PAUSED",
    canStop: active,
  };
}

/* ==========================================================================
 * Loop controls
 * ======================================================================= */

function LoopControls({
  status,
  admin,
  onDone,
}: {
  status: ReturnType<typeof useWsQuery<AutopilotStatus>>;
  admin: { allowed: boolean; reason: string | null };
  onDone: (message: string | null) => void;
}) {
  const [mode, setMode] = useState("CONTINUOUS");
  const [cyclesTarget, setCyclesTarget] = useState("");
  const [overrideReadiness, setOverrideReadiness] = useState(false);
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [confirmed, setConfirmed] = useState<string | null>(null);

  const gates = gatesFor(status.data?.state);
  const statusReady =
    !status.loading &&
    !status.refreshing &&
    !status.error &&
    typeof status.data?.state === "string" &&
    status.data.state.trim() !== "";
  const canMutate = admin.allowed && statusReady;

  const finish = (message: string) => {
    setConfirmed(message);
    onDone(message);
    status.reload();
  };
  const fail = (message: string) => {
    onDone(message);
  };

  const start = useMutation<{ mode: string; cycles_target: number; override_readiness: boolean }, unknown>(
    (body) => wsApi.post("/autopilot/start", body) as Promise<unknown>,
    { onSuccess: (r) => finish(`Start accepted — ${JSON.stringify(r)}. Status re-read from the server.`), onError: (e) => fail(e.message) },
  );
  const stop = useMutation<void, unknown>(
    () => wsApi.post("/autopilot/stop") as Promise<unknown>,
    { onSuccess: () => finish("Stop requested — running steps finish, then the loop stops. Status re-read from the server."), onError: (e) => fail(e.message) },
  );
  const pause = useMutation<void, unknown>(
    () => wsApi.post("/autopilot/pause") as Promise<unknown>,
    { onSuccess: () => finish("Pause requested — the loop halts before the next stage. Status re-read from the server."), onError: (e) => fail(e.message) },
  );
  const resume = useMutation<void, unknown>(
    () => wsApi.post("/autopilot/resume") as Promise<unknown>,
    { onSuccess: () => finish("Resume accepted. Status re-read from the server."), onError: (e) => fail(e.message) },
  );
  const runOne = useMutation<void, unknown>(
    () => wsApi.post("/autopilot/run-one-cycle") as Promise<unknown>,
    { onSuccess: () => finish("Single cycle queued — exactly one FIND→…→LEARN cycle. Status re-read from the server."), onError: (e) => fail(e.message) },
  );

  const pending = start.pending || stop.pending || pause.pending || resume.pending || runOne.pending;
  const mutationError = start.error ?? stop.error ?? pause.error ?? resume.error ?? runOne.error;

  const submitStart = () => {
    const raw = cyclesTarget.trim();
    let target = 0;
    if (raw !== "") {
      const n = Number(raw);
      if (!Number.isInteger(n) || n < 0) {
        setFieldError("Cycles target must be a whole number of 0 or more. Nothing was sent.");
        return;
      }
      target = n;
    }
    setFieldError(null);
    void start.run({ mode, cycles_target: target, override_readiness: overrideReadiness });
  };

  return (
    <>
      <div className="ymauto-startform">
        <Select label="Start mode" value={mode} onChange={(e) => setMode(e.target.value)} disabled={!canMutate || pending}>
          <option value="CONTINUOUS">CONTINUOUS</option>
          <option value="SINGLE_CYCLE">SINGLE_CYCLE</option>
        </Select>
        <Field
          label="Cycles target"
          type="number"
          min="0"
          step="1"
          value={cyclesTarget}
          onChange={(e) => setCyclesTarget(e.target.value)}
          hint="Blank means 0: run until stopped. Whole numbers only."
          disabled={!canMutate || pending}
        />
      </div>
      <div className="ymauto-startform">
        <Toggle
          checked={overrideReadiness}
          onChange={setOverrideReadiness}
          label="Override readiness gate (start even if readiness checks fail)"
          disabled={!canMutate || pending}
        />
      </div>
      {fieldError ? <p className="ym-error" role="alert">{fieldError}</p> : null}
      {mutationError ? <p className="ym-error" role="alert">{mutationError}</p> : null}
      {confirmed && !mutationError ? <p className="ym-hint" role="status">{confirmed}</p> : null}
      <div className="ymauto-controls" role="group" aria-label="Autopilot loop controls">
        <Button
          variant="primary"
          loading={start.pending}
          disabled={!canMutate || !gates.canStart || pending}
          onClick={submitStart}
          title={gates.canStart ? "POST /autopilot/start" : `Start is unavailable while the loop is ${status.data?.state ?? "loading"}.`}
        >
          Start loop
        </Button>
        <Button
          variant="secondary"
          loading={runOne.pending}
          disabled={!canMutate || !gates.canStart || pending}
          onClick={() => void runOne.run(undefined)}
          title={gates.canStart ? "POST /autopilot/run-one-cycle" : `Single cycle is unavailable while the loop is ${status.data?.state ?? "loading"}.`}
        >
          Run one cycle
        </Button>
        <Button
          variant="secondary"
          loading={pause.pending}
          disabled={!canMutate || !gates.canPause || pending}
          onClick={() => void pause.run(undefined)}
          title={gates.canPause ? "POST /autopilot/pause" : "Pause applies only while the loop is RUNNING."}
        >
          Pause
        </Button>
        <Button
          variant="secondary"
          loading={resume.pending}
          disabled={!canMutate || !gates.canResume || pending}
          onClick={() => void resume.run(undefined)}
          title={gates.canResume ? "POST /autopilot/resume" : "Resume applies only while the loop is PAUSED."}
        >
          Resume
        </Button>
        <DestructiveButton
          confirmLabel="Stop the loop. Running steps finish; no new cycle starts. This ends the current run."
          disabled={!canMutate || !gates.canStop || pending}
          onConfirm={() => void stop.run(undefined)}
        >
          Stop
        </DestructiveButton>
      </div>
      <p className="ymauto-permnote" role="note">
        {!admin.allowed
          ? admin.reason
          : !statusReady
            ? "Loop writes are disabled until GET /autopilot/status returns a current state."
            : "Write paths POST /autopilot/start|/stop|/pause|/resume|/run-one-cycle require an admin role. The server enforces this; every control above is pessimistic — pending, then confirmed from GET /autopilot/status."}
      </p>
    </>
  );
}

/* ==========================================================================
 * Agent policy (enable/disable + model/timeout/cost retune)
 * ======================================================================= */

function AgentPolicy({
  query,
  admin,
}: {
  query: ReturnType<typeof useWsQuery<{ items: AgentPolicyRow[] }>>;
  admin: { allowed: boolean; reason: string | null };
}) {
  const [pendingKey, setPendingKey] = useState<string | null>(null);
  const [toggleError, setToggleError] = useState<string | null>(null);
  const [selectedKey, setSelectedKey] = useState<string>("");
  const [model, setModel] = useState("");
  const [timeout, setTimeout] = useState("");
  const [cap, setCap] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [retuneNote, setRetuneNote] = useState<string | null>(null);

  const toggle = useMutation<string, unknown>(
    (agentKey: string) => {
      const row = query.data?.items.find((a) => a.key === agentKey);
      return wsApi.put(`/agents/config/${encodeURIComponent(agentKey)}`, {
        enabled: !(row?.enabled ?? true),
      }) as Promise<unknown>;
    },
    {
      onSuccess: () => {
        setPendingKey(null);
        setToggleError(null);
        query.reload();
      },
      onError: (e) => {
        setPendingKey(null);
        setToggleError(e.message);
      },
    },
  );

  const retune = useMutation<Record<string, unknown>, unknown>(
    (body) => wsApi.put(`/agents/config/${encodeURIComponent(selectedKey)}`, body) as Promise<unknown>,
    {
      onSuccess: () => {
        setRetuneNote(`Policy saved for ${selectedKey}. Values re-read from the server.`);
        setModel("");
        setTimeout("");
        setCap("");
        setFieldError(null);
        query.reload();
      },
      onError: (e) => setRetuneNote(null),
    },
  );

  const selected = useMemo(
    () => query.data?.items.find((a) => a.key === selectedKey) ?? null,
    [query.data, selectedKey],
  );

  const submitRetune = () => {
    if (!selectedKey) {
      setFieldError("Pick an agent first. Nothing was sent.");
      return;
    }
    const body: Record<string, unknown> = {};
    const m = model.trim();
    if (m !== "") {
      if (m.length > 120) {
        setFieldError("Model must be 120 characters or fewer. Nothing was sent.");
        return;
      }
      body.model = m;
    }
    const t = timeout.trim();
    if (t !== "") {
      const n = Number(t);
      if (!Number.isInteger(n) || n < 10 || n > 3600) {
        setFieldError("Timeout must be a whole number of seconds from 10 to 3600. Nothing was sent.");
        return;
      }
      body.timeout_seconds = n;
    }
    const c = cap.trim();
    if (c !== "") {
      const n = Number(c);
      if (!Number.isFinite(n) || n < 0) {
        setFieldError("Cost cap must be 0 or more in USD. Nothing was sent.");
        return;
      }
      body.cost_limit_usd = n;
    }
    if (Object.keys(body).length === 0) {
      setFieldError("Nothing to save: fill at least one field. Blank fields are left untouched, never zeroed.");
      return;
    }
    setFieldError(null);
    void retune.run(body);
  };

  const columns: Column<AgentPolicyRow>[] = [
    { key: "title", header: "Agent", cell: (a) => <strong>{a.title}</strong> },
    {
      key: "enabled",
      header: "Enabled",
      cell: (a) =>
        a.enabled ? (
          <Badge tone="success" dot>ENABLED</Badge>
        ) : (
          <Badge tone="neutral" dot>DISABLED</Badge>
        ),
    },
    {
      key: "model",
      header: "Model",
      cell: (a) =>
        a.model ? (
          a.model
        ) : (
          <span className="ym-muted" title="No override; the agent uses the engine default.">engine default</span>
        ),
      hideBelow: "md",
    },
    { key: "timeout", header: "Timeout", align: "right", cell: (a) => `${a.timeout_seconds}s`, hideBelow: "md" },
    {
      key: "cap",
      header: "Cost cap",
      align: "right",
      cell: (a) => <Money usd={a.cost_limit_usd} />,
      hideBelow: "lg",
    },
    {
      key: "toggle",
      header: "Control",
      cell: (a) => (
        <Toggle
          checked={a.enabled}
          disabled={(toggle.pending && pendingKey === a.key) || !admin.allowed}
          label={a.enabled ? "Disable" : "Enable"}
          onChange={() => {
            setPendingKey(a.key);
            void toggle.run(a.key);
          }}
        />
      ),
    },
  ];

  return (
    <>
      {toggleError ? <p className="ym-error" role="alert">{toggleError}</p> : null}
      <QueryBoundary query={query} skeletonRows={6}>
        {(d) => (
          <>
            <DataTable
              rows={d.items ?? []}
              columns={columns}
              rowKey={(a) => a.key}
              caption="Automation agent policy"
              maxHeight={520}
              empty="No agent registered"
              emptyHint="The registry declares no agent, so no stage of the pipeline can run."
            />
            <h3 className="ym-panel-title">Retune an agent</h3>
            <div className="ymauto-startform">
              <Select
                label="Agent"
                value={selectedKey}
                onChange={(e) => setSelectedKey(e.target.value)}
                disabled={!admin.allowed || retune.pending}
              >
                <option value="">Pick an agent…</option>
                {(d.items ?? []).map((a) => (
                  <option key={a.key} value={a.key}>
                    {a.title}
                  </option>
                ))}
              </Select>
              <Field
                label="Model"
                value={model}
                onChange={(e) => setModel(e.target.value)}
                hint={selected ? `Now: ${selected.model || "engine default"}. Blank leaves it untouched.` : "Blank leaves the stored model untouched."}
                disabled={!admin.allowed || retune.pending}
              />
              <Field
                label="Timeout (seconds)"
                type="number"
                min="10"
                max="3600"
                step="1"
                value={timeout}
                onChange={(e) => setTimeout(e.target.value)}
                hint={selected ? `Now: ${selected.timeout_seconds}s. Blank leaves it untouched.` : "10–3600. Blank leaves it untouched."}
                disabled={!admin.allowed || retune.pending}
              />
              <Field
                label="Cost cap (USD)"
                type="number"
                min="0"
                step="0.01"
                value={cap}
                onChange={(e) => setCap(e.target.value)}
                hint="Blank leaves the stored cap untouched — it never zeroes it. A missing cap means no per-agent cap, not free execution."
                disabled={!admin.allowed || retune.pending}
              />
            </div>
            {fieldError ? <p className="ym-error" role="alert">{fieldError}</p> : null}
            {retune.error ? <p className="ym-error" role="alert">{retune.error}</p> : null}
            {retuneNote && !retune.error ? <p className="ym-hint" role="status">{retuneNote}</p> : null}
            <div className="ymauto-controls">
              <Button variant="primary" loading={retune.pending} disabled={!admin.allowed || !selectedKey} onClick={submitRetune}>
                Save agent policy
              </Button>
            </div>
          </>
        )}
      </QueryBoundary>
      <p className="ymauto-permnote" role="note">
        {admin.allowed
          ? "Write path PUT /agents/config/{key} requires an admin role. The server enforces this. A null cost cap means no per-agent cap — it does not mean the agent costs nothing."
          : admin.reason}
      </p>
    </>
  );
}

/* ==========================================================================
 * Page
 * ======================================================================= */

export default function Automation() {
  const admin = useAdminGate();
  const status = useWsQuery<AutopilotStatus>("/autopilot/status");
  const decision = useWsQuery<DecisionPreview>("/decision");
  const agents = useWsQuery<{ items: AgentPolicyRow[] }>("/agents/config");
  const cycles = useWsQuery<{ items: CycleRow[] }>("/cycles");
  const [lastAction, setLastAction] = useState<string | null>(null);

  const gates = gatesFor(status.data?.state);
  const state = (status.data?.state ?? "").toUpperCase();

  const cycleColumns: Column<CycleRow>[] = [
    { key: "number", header: "Cycle", align: "right", cell: (c) => `#${c.number}` },
    {
      key: "stage",
      header: "Stage",
      cell: (c) => <Badge tone={toneForStatus(c.stage)}>{humanize(c.stage)}</Badge>,
      hideBelow: "md",
    },
    {
      key: "status",
      header: "Status",
      cell: (c) => <Badge tone={toneForStatus(c.status)} dot>{humanize(c.status)}</Badge>,
    },
    {
      key: "topic",
      header: "Topic",
      cell: (c) => c.topic ?? <span className="ym-muted">—</span>,
      hideBelow: "lg",
    },
    { key: "cost", header: "Cost", align: "right", cell: (c) => <Money usd={c.cost_usd} />, hideBelow: "md" },
    { key: "started", header: "Started", cell: (c) => when(c.started_at), hideBelow: "lg" },
    {
      key: "error",
      header: "Error",
      cell: (c) =>
        c.error ? (
          <span className="ymauto-errortext" title={c.error}>{c.error}</span>
        ) : (
          <span className="ym-muted">—</span>
        ),
      hideBelow: "lg",
    },
  ];

  return (
    // NOT a <main>: the shell already provides the single page landmark
    // (`AppShell` `<main id="ym-main">`). A second <main> fails the route-matrix
    // render gate (exactly one landmark) and is an a11y violation — nested
    // landmarks split the page for assistive technology.
    <div aria-label="Automation">
      <PageHeader
        title="Automation"
        description="The autonomous loop — its live state, its next decision, and the policy that steers it. Every figure below is read from the server; nothing is sampled or estimated."
      />

      {/* Loop state — the single honest status line, announced to assistive tech. */}
      <div role="status" aria-live="polite" className="ymauto-savestate" aria-label="Loop state">
        {status.loading
          ? "Reading loop state…"
          : status.error
            ? `Loop state UNAVAILABLE — ${status.error}`
            : `Loop is ${humanize(state) || "unknown"}${status.data?.mode ? ` · mode ${status.data.mode}` : ""} · ${status.data?.cycles_completed ?? 0} cycles completed`}
      </div>

      <Panel
        title="Loop"
        subtitle="GET /autopilot/status — state, mode, progress, and the last reported error."
        actions={
          status.data ? (
            <Badge tone={toneForStatus(state)} dot title={`Loop state: ${state}`}>
              {humanize(state) || "UNKNOWN"}
            </Badge>
          ) : undefined
        }
      >
        <QueryBoundary query={status} skeletonRows={4}>
          {(d) => (
            <>
              {d.state === "IDLE" && (d.cycles_completed ?? 0) === 0 ? (
                <EmptyState
                  title="Loop is idle"
                  description="No run has completed for this workspace. Start the loop or queue a single cycle to produce the first FIND→…→LEARN pass."
                />
              ) : null}
              <Grid min={190} gap="sm">
                <StatTile label="State" value={humanize(d.state)} source="GET /autopilot/status" />
                <StatTile
                  label="Mode"
                  value={d.mode || "NOT SET"}
                  tone={d.mode ? "neutral" : "unknown"}
                  hint="CONTINUOUS runs until stopped; SINGLE_CYCLE runs once."
                  source="GET /autopilot/status"
                />
                <StatTile
                  label="Cycles completed"
                  value={`${d.cycles_completed ?? 0}${d.cycles_target ? ` of ${d.cycles_target}` : ""}`}
                  source="GET /autopilot/status"
                />
                <StatTile
                  label="Queued jobs"
                  value={d.queued_jobs ?? "UNAVAILABLE"}
                  unavailable={d.queued_jobs === null || d.queued_jobs === undefined}
                  source="GET /autopilot/status"
                />
              </Grid>
              {d.current_cycle ? (
                <p className="ym-hint" role="status">
                  Live now: cycle #{d.current_cycle.number} — stage {humanize(d.current_cycle.stage)} ({humanize(d.current_cycle.status)}).
                </p>
              ) : (
                <p className="ym-hint">No cycle is running right now. That is an idle loop, not a missing read.</p>
              )}
              {d.last_error ? (
                <p className="ym-error" role="alert">
                  Last error: {d.last_error}
                </p>
              ) : null}
              {(d.scheduled_start_at || d.scheduled_stop_at) && (
                <p className="ym-hint">
                  Window: {d.scheduled_start_at ? when(d.scheduled_start_at) : "—"} →{" "}
                  {d.scheduled_stop_at ? when(d.scheduled_stop_at) : "—"}
                </p>
              )}
              <h3 className="ym-panel-title">Pause / stop gates</h3>
              <p className="ym-hint" role="note">
                Pause applies {gates.canPause ? "now (RUNNING)" : "only while RUNNING"} · Resume applies{" "}
                {gates.canResume ? "now (PAUSED)" : "only while PAUSED"} · Stop applies{" "}
                {gates.canStop ? "now (a run is active)" : "only while a run is active"} · Start applies{" "}
                {gates.canStart ? "now (loop is idle)" : `never while ${humanize(state)}`}. The server re-checks
                every gate; a stale screen degrades to a reported 409, not a silent no-op.
              </p>
            </>
          )}
        </QueryBoundary>
      </Panel>

      <Panel
        title="Loop controls"
        subtitle="POST /autopilot/start|/stop|/pause|/resume|/run-one-cycle — pessimistic: pending, then confirmed from the server."
      >
        <div className="ymauto-savestate" role="status" aria-label="Automation save state">
          {lastAction ?? "No loop action taken yet."}
        </div>
        <LoopControls status={status} admin={admin} onDone={setLastAction} />
      </Panel>

      <Panel
        title="Next best action"
        subtitle="GET /decision — the Decision Engine's live preview: PRODUCE, WAIT, SKIP, RESEARCH_MORE, or HUMAN_REVIEW, with its WHY."
      >
        <QueryBoundary query={decision} skeletonRows={3}>
          {(d) => (
            <>
              <Grid min={190} gap="sm">
                <StatTile
                  label="Action"
                  value={<Badge tone={d.action === "PRODUCE" ? "success" : d.action === "HUMAN_REVIEW" ? "warning" : "neutral"} dot>{humanize(d.action)}</Badge>}
                  source="GET /decision"
                />
                <StatTile
                  label="Topic"
                  value={d.topic || "UNAVAILABLE"}
                  unavailable={!d.topic}
                  source="GET /decision"
                />
                <StatTile
                  label="Score"
                  value={d.score ?? "UNAVAILABLE"}
                  unavailable={d.score === null || d.score === undefined}
                  source="GET /decision"
                />
                <StatTile
                  label="Confidence"
                  value={d.confidence ?? "UNAVAILABLE"}
                  unavailable={d.confidence === null || d.confidence === undefined}
                  source="GET /decision"
                />
              </Grid>
              {(d.reasons ?? []).length > 0 ? (
                <ul className="ymauto-reasons">
                  {d.reasons.map((r, i) => (
                    <li key={i} className="ym-notif-detail">{r}</li>
                  ))}
                </ul>
              ) : (
                <p className="ym-hint">No reasons reported. That is an empty preview, not approval.</p>
              )}
              {(d.factors ?? []).length > 0 ? (
                <DataTable
                  rows={d.factors}
                  columns={[
                    { key: "name", header: "Factor", cell: (f) => humanize(f.name) },
                    { key: "value", header: "Value", align: "right", cell: (f) => String(f.value ?? "—") },
                    { key: "contribution", header: "Contribution", align: "right", cell: (f) => String(f.contribution ?? "—") },
                    { key: "detail", header: "Detail", cell: (f) => f.detail || <span className="ym-muted">—</span>, hideBelow: "md" },
                  ]}
                  rowKey={(_, i) => String(i)}
                  caption="Decision factors"
                  empty="No decision factors reported"
                />
              ) : null}
              {(d.evidence ?? []).length > 0 ? (
                <p className="ym-hint">Evidence: {d.evidence.join(" · ")}</p>
              ) : null}
              <p className="ym-hint" role="note">
                Read-only. Planning autonomy is not publishing autonomy: no planning action here can publish, and a
                HUMAN_REVIEW action means a human must approve before production advances.
              </p>
            </>
          )}
        </QueryBoundary>
      </Panel>

      <Panel title="Agent policy" subtitle="GET /agents/config · PUT /agents/config/{key} — which agents may run, and on what budget.">
        <AgentPolicy query={agents} admin={admin} />
      </Panel>

      <Panel title="Recent cycles" subtitle="GET /cycles — the loop's executed FIND→…→LEARN passes, newest first.">
        <QueryBoundary query={cycles} skeletonRows={4}>
          {(d) => (
            <DataTable
              rows={d.items ?? []}
              columns={cycleColumns}
              rowKey={(c) => c.id}
              caption="Recent autopilot cycles"
              empty="No cycles yet"
              emptyHint="The loop has executed nothing for this workspace. An empty history is not a failed read."
            />
          )}
        </QueryBoundary>
      </Panel>
    </div>
  );
}
