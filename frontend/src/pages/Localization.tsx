import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { wsApi } from "../lib/api";
import { useFetch, useInterval } from "../hooks/hooks";
import {
  Accordion, Badge, Card, ConfirmButton, ErrorBox, Field, Loading,
  PageHeader, Progress, Section, Tabs, toast,
} from "../components/ui";
import { fmtAgo } from "../lib/format";

/* Localization (Work 07): run translations, glossary, QC reports.
   Mirrors Performance.tsx — every panel degrades to an explicit
   empty/error state when a route 404s. No hardcoded mocks. */

/* mirrors backend app.providers.dubbing.LANG_LOCALES */
const LANGS: { code: string; label: string }[] = [
  { code: "es", label: "Spanish" }, { code: "fr", label: "French" },
  { code: "de", label: "German" }, { code: "pt", label: "Portuguese" },
  { code: "hi", label: "Hindi" }, { code: "ar", label: "Arabic" },
  { code: "id", label: "Indonesian" }, { code: "tr", label: "Turkish" },
  { code: "ru", label: "Russian" }, { code: "ja", label: "Japanese" },
  { code: "ko", label: "Korean" }, { code: "zh", label: "Chinese" },
  { code: "it", label: "Italian" }, { code: "nl", label: "Dutch" },
  { code: "pl", label: "Polish" }, { code: "uk", label: "Ukrainian" },
  { code: "en", label: "English" },
];

const GLOSSARY_KINDS = ["brand", "product", "terminology", "pronunciation"] as const;

function is404(err: string | null): boolean {
  return !!err && /404|not found/i.test(err);
}

/* localization run statuses come from the pipeline (PENDING/RUNNING/READY/FAILED/CANCELLED) */
function runTone(status?: string): "success" | "warning" | "error" | "info" | "muted" {
  const s = (status || "").toUpperCase();
  if (s === "READY" || s === "COMPLETED") return "success";
  if (s === "FAILED" || s === "CANCELLED") return "error";
  if (s === "RUNNING") return "warning";
  if (s === "PENDING") return "info";
  return "muted";
}

function qcTone(status?: string): "success" | "warning" | "error" | "muted" {
  const s = (status || "").toUpperCase();
  if (s === "PASS") return "success";
  if (s === "PASS_WITH_WARNINGS" || s === "REVIEW_REQUIRED") return "warning";
  if (s === "FAIL") return "error";
  return "muted";
}

/* data-derived progress: terminal states 100, pending 5, running scales with stages */
function runProgress(run: any): number {
  const s = String(run?.status || "").toUpperCase();
  if (s === "READY" || s === "FAILED" || s === "CANCELLED") return 100;
  if (s === "PENDING") return 5;
  const stages = Array.isArray(run?.stages) ? run.stages.length : 0;
  return Math.min(95, 15 + stages * 15);
}

const ACTIVE_JOB_STATUSES = ["QUEUED", "WAITING", "RUNNING", "RETRYING"];

export default function Localization() {
  const [tab, setTab] = useState<"runs" | "glossary">("runs");
  const runs = useFetch(() => wsApi.get("/localization"), []);
  const items: any[] = (runs.data as any)?.items ?? [];
  const [sel, setSel] = useState<string | null>(null);

  const anyActive = items.some((r) =>
    ["PENDING", "RUNNING"].includes(String(r.status || "").toUpperCase()));
  useInterval(() => runs.reload(), anyActive ? 4000 : null);

  return (
    <div className="space-y-4">
      <PageHeader
        title="Localization"
        subtitle="Translate content into target languages with glossary + QC — open finished runs in the editor."
        actions={
          <button className="btn-ghost !text-xs !py-1" onClick={runs.reload}>Refresh</button>
        }
      />
      <Tabs tabs={[
        { key: "runs", label: "Runs", count: items.length },
        { key: "glossary", label: "Glossary" },
      ]} active={tab} onChange={setTab} />

      {tab === "runs" && (
        <RunsTab runs={runs} items={items} sel={sel} setSel={setSel} />
      )}
      {tab === "glossary" && <GlossaryTab />}
    </div>
  );
}

/* ---- Runs: new run form + list + detail/QC ---- */

function RunsTab({
  runs, items, sel, setSel,
}: {
  runs: { data: any; loading: boolean; error: string | null; reload: () => void };
  items: any[];
  sel: string | null;
  setSel: (id: string | null) => void;
}) {
  const nav = useNavigate();
  const jobs = useFetch(() => wsApi.get("/jobs?limit=200"), []);
  const jobItems: any[] = (jobs.data as any)?.items ?? [];
  const [busy, setBusy] = useState<string | null>(null);

  /* the run row carries no job_id — match the enqueued localization.run job
     by payload.localized_ids (data-driven, no hardcoded mapping) */
  function activeJob(runId: string): any | null {
    return jobItems.find((j) =>
      j.type === "localization.run" &&
      j.payload &&
      Array.isArray(j.payload.localized_ids) &&
      j.payload.localized_ids.includes(runId) &&
      ACTIVE_JOB_STATUSES.includes(String(j.status || "").toUpperCase())) ?? null;
  }

  async function cancelRun(run: any, job: any) {
    setBusy(run.id);
    try {
      await wsApi.post(`/jobs/${job.id}/cancel`, {});
      toast("Cancellation requested", "success");
      runs.reload();
      jobs.reload();
    } catch (e: any) {
      toast(e?.message ?? "cancel failed", "error", "Cancel failed");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-4">
      <NewRunForm onQueued={() => { runs.reload(); jobs.reload(); }} />
      <div className="grid lg:grid-cols-[340px_1fr] gap-4">
        <Card pad={false} className="overflow-hidden">
          <div className="px-4 py-3 text-[13px] font-semibold" style={{ borderBottom: "var(--seam)" }}>
            Runs <span className="font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>{items.length}</span>
          </div>
          {runs.loading && !runs.data && <div className="p-3"><Loading rows={3} /></div>}
          {runs.error && !runs.data && (
            <div className="p-3"><ErrorBox error={runs.error} onRetry={runs.reload} /></div>
          )}
          {runs.data && !items.length && (
            <div className="p-4 text-[13px]" style={{ color: "var(--text-muted)" }}>
              No localization runs yet — pick source content above and queue one.
            </div>
          )}
          {items.map((r: any) => (
            <button key={r.id} onClick={() => setSel(r.id)} className="w-full text-left px-3 py-2.5"
              style={{ borderBottom: "var(--seam)", background: sel === r.id ? "var(--seam)" : undefined }}>
              <div className="flex items-center gap-2 flex-wrap">
                <Badge tone={runTone(r.status)}>{r.status || "UNKNOWN"}</Badge>
                <b className="text-[13px]">{(r.language || "?").toUpperCase()}{r.locale ? ` · ${r.locale}` : ""}</b>
              </div>
              <div className="flex items-center gap-2 mt-1 flex-wrap">
                {r.qc?.status && <Badge tone={qcTone(r.qc.status)}>QC {r.qc.status}</Badge>}
                <span className="font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>
                  {fmtAgo(r.created_at)}
                </span>
              </div>
              <div className="mt-1.5"><Progress value={runProgress(r)} /></div>
            </button>
          ))}
        </Card>

        {sel ? (
          <RunDetail
            key={sel}
            runId={sel}
            onOpenEditor={(tid) => nav(`/editor/${tid}`)}
            onCancel={(run) => {
              const job = activeJob(run.id);
              if (job) cancelRun(run, job);
            }}
            cancelJob={activeJob(sel)}
            busy={busy === sel}
          />
        ) : (
          <Card>
            <div className="text-[13px]" style={{ color: "var(--text-muted)" }}>
              Select a run to inspect stages, QC checks and open the localized timeline in the editor.
            </div>
          </Card>
        )}
      </div>
    </div>
  );
}

function NewRunForm({ onQueued }: { onQueued: () => void }) {
  const content = useFetch(() => wsApi.get("/content?limit=100"), []);
  const contentItems: any[] = (content.data as any)?.items ?? [];
  const [sourceId, setSourceId] = useState("");
  const [langs, setLangs] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  function toggle(code: string) {
    setLangs((l) => l.includes(code) ? l.filter((x) => x !== code) : [...l, code]);
  }

  async function run() {
    if (!sourceId) { toast("Pick source content first", "warning"); return; }
    if (!langs.length) { toast("Select at least one target language", "warning"); return; }
    setBusy(true);
    try {
      const r: any = await wsApi.post("/localization/run", {
        source_content_id: sourceId,
        target_languages: langs,
      });
      const n = (r?.items ?? []).length;
      toast(`${n} localization run(s) queued${r?.job_id ? "" : " (no job id returned)"}`,
        r?.queued ? "success" : "warning");
      setLangs([]);
      onQueued();
    } catch (e: any) {
      toast(e?.message ?? "run failed", "error", "Localization failed to queue");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <div className="flex items-center gap-2 flex-wrap">
        <b className="text-[14px]">New localization run</b>
        <Badge tone="info">{langs.length} language(s) selected</Badge>
      </div>
      <div className="grid md:grid-cols-[1fr_auto] gap-3 items-end mt-3">
        <Field label="Source content"
          hint={content.error ? `Content list unavailable: ${content.error}` : "Existing content item to translate."}>
          <select className="select !text-xs" value={sourceId} onChange={(e) => setSourceId(e.target.value)}>
            <option value="">— select content —</option>
            {contentItems.map((c: any) => (
              <option key={c.id} value={c.id}>{c.topic || c.id.slice(0, 8)} · {c.status}</option>
            ))}
          </select>
        </Field>
        <button className="btn-primary !text-xs mb-3" disabled={busy || !sourceId || !langs.length} onClick={run}>
          {busy ? "Queueing…" : "Run localization"}
        </button>
      </div>
      <Field label="Target languages" hint="Pipeline languages mirror the dubbing provider table.">
        <div className="flex gap-1.5 flex-wrap">
          {LANGS.map((l) => (
            <button key={l.code}
              className={`tab ${langs.includes(l.code) ? "active" : ""}`}
              onClick={() => toggle(l.code)}
              type="button">
              {l.label} <span className="font-mono text-[10.5px] opacity-70">{l.code}</span>
            </button>
          ))}
        </div>
      </Field>
      {!contentItems.length && !content.loading && !content.error && (
        <div className="text-[12.5px]" style={{ color: "var(--text-faint)" }}>
          No content available yet — produce something in the Studio first.
        </div>
      )}
    </Card>
  );
}

function RunDetail({
  runId, onOpenEditor, onCancel, cancelJob, busy,
}: {
  runId: string;
  onOpenEditor: (timelineId: string) => void;
  onCancel: (run: any) => void;
  cancelJob: any | null;
  busy: boolean;
}) {
  const detail = useFetch(() => wsApi.get(`/localization/${runId}`), [runId]);
  const qc = useFetch(() => wsApi.get(`/localization/${runId}/qc`), [runId]);
  const run: any = detail.data;

  if (detail.loading && !run) return <Card><Loading rows={4} /></Card>;
  if (detail.error && !run) {
    return <ErrorBox error={detail.error} onRetry={detail.reload} />;
  }
  if (!run) return null;

  const report: any = qc.data;
  const checks: any[] = Array.isArray(report?.checks) ? report.checks : [];
  const flagged = checks.filter((c) => String(c?.status || "").toLowerCase() !== "pass");
  const counts: any = report?.counts ?? run.qc?.counts ?? {};

  return (
    <div className="space-y-3">
      <Card>
        <div className="flex items-center gap-2 flex-wrap">
          <b className="text-[14px]">Localization · {(run.language || "?").toUpperCase()}</b>
          <Badge tone={runTone(run.status)}>{run.status}</Badge>
          {run.qc?.status && <Badge tone={qcTone(run.qc.status)}>QC {run.qc.status}</Badge>}
          {run.locale && <Badge tone="muted">{run.locale}</Badge>}
          <Badge tone="muted">v{run.translation_version ?? 1}</Badge>
          <span className="ml-auto font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>
            {fmtAgo(run.updated_at || run.created_at)}
          </span>
        </div>
        <div className="mt-2"><Progress value={runProgress(run)} /></div>
        <div className="flex gap-2 mt-3 flex-wrap">
          {run.timeline_id && (
            <button className="btn-primary !text-xs" onClick={() => onOpenEditor(run.timeline_id)}>
              🎞 Open in Editor
            </button>
          )}
          {cancelJob && (
            <button className="btn-outline !text-xs" disabled={busy} onClick={() => onCancel(run)}>
              {busy ? "Cancelling…" : "Cancel run"}
            </button>
          )}
          <button className="btn-ghost !text-xs" onClick={() => { detail.reload(); qc.reload(); }}>Refresh</button>
        </div>
        {run.error && (
          <div className="text-[12.5px] mt-2 font-mono" style={{ color: "var(--danger)" }}>{run.error}</div>
        )}
        {run.timeline_id && (
          <div className="text-[12px] mt-2 font-mono" style={{ color: "var(--text-faint)" }}>
            timeline: {run.timeline_id}
          </div>
        )}
        {!run.timeline_id && String(run.status).toUpperCase() === "READY" && (
          <div className="text-[12.5px] mt-2" style={{ color: "var(--warn)" }}>
            Run finished but no timeline id was recorded — nothing to open in the editor.
          </div>
        )}
      </Card>

      <Card>
        <div className="flex items-center gap-2 flex-wrap">
          <b className="text-[14px]">QC report</b>
          {report?.status && <Badge tone={qcTone(report.status)}>{report.status}</Badge>}
          {!report && qc.error && <Badge tone="muted">unavailable</Badge>}
        </div>
        {qc.loading && !qc.data && <div className="mt-2"><Loading rows={2} /></div>}
        {qc.error && !qc.data && (
          <div className="text-[12.5px] mt-2" style={{ color: "var(--text-muted)" }}>
            {is404(qc.error)
              ? "No QC report yet — the report is written when the localization pipeline finishes."
              : `QC report unavailable: ${qc.error}`}
            {!is404(qc.error) && (
              <button className="btn-outline !text-xs ml-3" onClick={qc.reload}>Retry</button>
            )}
          </div>
        )}
        {report && (
          <>
            <div className="flex gap-2 flex-wrap mt-2">
              {Object.entries(counts).map(([k, v]) => (
                <Badge key={k} tone="muted">{k}: {String(v)}</Badge>
              ))}
            </div>
            <div className="mt-3 space-y-1.5">
              {!checks.length && (
                <div className="text-[12.5px]" style={{ color: "var(--text-faint)" }}>
                  Report carries no check rows.
                </div>
              )}
              {flagged.map((c, i) => (
                <div key={i} className="flex gap-2 items-start text-[12.5px] flex-wrap">
                  <Badge tone={String(c.status).toLowerCase() === "fail" ? "error"
                    : String(c.status).toLowerCase() === "review" ? "warning" : "info"}>
                    {c.status}
                  </Badge>
                  <code className="text-[12px]">{c.name}</code>
                  <span style={{ color: "var(--text-muted)" }}>{c.detail}</span>
                </div>
              ))}
              {checks.length > 0 && flagged.length === 0 && (
                <div className="text-[12.5px]" style={{ color: "var(--accent)" }}>
                  All {checks.length} checks passed.
                </div>
              )}
            </div>
          </>
        )}
      </Card>

      <Accordion title="Stages, warnings & lineage">
        <div className="space-y-2 text-[12.5px]">
          <div className="flex gap-1.5 flex-wrap">
            {(run.stages ?? []).length === 0 && (
              <span style={{ color: "var(--text-faint)" }}>No stages recorded yet.</span>
            )}
            {(run.stages ?? []).map((s: any, i: number) => (
              <span key={i} className="font-mono text-[10.5px] px-1.5 py-0.5 rounded"
                style={{ background: "var(--accent-dim)", color: "var(--accent)" }}>
                {typeof s === "string" ? s : JSON.stringify(s)}
              </span>
            ))}
          </div>
          {(run.warnings ?? []).map((w: any, i: number) => (
            <div key={`w${i}`} style={{ color: "var(--warn)" }}>⚠ {String(w)}</div>
          ))}
          {(run.repairs ?? []).length > 0 && (
            <div style={{ color: "var(--info)" }}>
              repairs: {JSON.stringify(run.repairs).slice(0, 300)}
            </div>
          )}
          {run.costs && Object.keys(run.costs).length > 0 && (
            <div className="font-mono" style={{ color: "var(--text-muted)" }}>
              costs: {JSON.stringify(run.costs)}
            </div>
          )}
          <pre className="text-[11.5px] font-mono whitespace-pre-wrap max-h-[220px] overflow-y-auto p-2.5 rounded-lg"
            style={{ background: "var(--bg-panel)" }}>
            {JSON.stringify(run.lineage ?? {}, null, 2)}
          </pre>
        </div>
      </Accordion>
    </div>
  );
}

/* ---- Glossary: list / add / delete ---- */

function GlossaryTab() {
  const list = useFetch(() => wsApi.get("/localization/glossary"), []);
  const items: any[] = (list.data as any)?.items ?? [];
  const [term, setTerm] = useState("");
  const [replacement, setReplacement] = useState("");
  const [kind, setKind] = useState<string>("terminology");
  const [targetLanguages, setTargetLanguages] = useState("");
  const [caseSensitive, setCaseSensitive] = useState(false);
  const [busy, setBusy] = useState(false);

  async function add() {
    if (!term.trim()) { toast("Term is required", "warning"); return; }
    setBusy(true);
    try {
      await wsApi.post("/localization/glossary", {
        term: term.trim(),
        replacement: replacement.trim(),
        kind,
        case_sensitive: caseSensitive,
        target_languages: targetLanguages.split(",").map((s) => s.trim().toLowerCase()).filter(Boolean),
      });
      toast("Glossary term added", "success");
      setTerm(""); setReplacement(""); setTargetLanguages(""); setCaseSensitive(false);
      list.reload();
    } catch (e: any) {
      toast(e?.message ?? "add failed", "error", "Add term failed");
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string) {
    try {
      await wsApi.del(`/localization/glossary/${id}`);
      toast("Term deleted", "success");
      list.reload();
    } catch (e: any) {
      toast(e?.message ?? "delete failed", "error", "Delete failed");
    }
  }

  return (
    <div className="space-y-4">
      <Card>
        <b className="text-[14px]">Add glossary term</b>
        <div className="grid md:grid-cols-2 gap-x-4 mt-2">
          <Field label="Term" hint="Exact text to protect or replace when translating.">
            <input className="input !text-xs" value={term} placeholder="YMONEY"
              onChange={(e) => setTerm(e.target.value)} />
          </Field>
          <Field label="Replacement" hint="Leave empty to keep the term as-is.">
            <input className="input !text-xs" value={replacement} placeholder="translated form…"
              onChange={(e) => setReplacement(e.target.value)} />
          </Field>
          <Field label="Kind">
            <select className="select !text-xs" value={kind} onChange={(e) => setKind(e.target.value)}>
              {GLOSSARY_KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
            </select>
          </Field>
          <Field label="Target languages" hint="Comma-separated codes (es, fr, …). Empty = all.">
            <input className="input font-mono !text-xs" value={targetLanguages} placeholder="es, fr"
              onChange={(e) => setTargetLanguages(e.target.value)} />
          </Field>
          <label className="flex items-center gap-2 text-[12.5px] mb-3" style={{ color: "var(--text-muted)" }}>
            <input type="checkbox" checked={caseSensitive} onChange={(e) => setCaseSensitive(e.target.checked)} />
            Case sensitive
          </label>
        </div>
        <button className="btn-primary !text-xs" disabled={busy || !term.trim()} onClick={add}>
          {busy ? "Adding…" : "Add term"}
        </button>
      </Card>

      <Section data={items} loading={list.loading} error={list.error} onRetry={list.reload}
        empty="No glossary terms yet"
        emptyHint="Add brand, product and terminology entries so translations never mangle the words that matter.">
        {(rows) => (
          <Card>
            <div className="flex items-center gap-2 flex-wrap">
              <b className="text-[14px]">Workspace glossary</b>
              <Badge tone="info">{rows.length} terms</Badge>
              <span className="ml-auto">
                <button className="btn-ghost !text-xs !py-1" onClick={list.reload}>Refresh</button>
              </span>
            </div>
            <div className="mt-3 space-y-2">
              {rows.map((t: any) => (
                <div key={t.id} className="flex items-center gap-2 flex-wrap text-[12.5px] p-2 rounded-lg"
                  style={{ border: "var(--seam)", background: "var(--bg-inset)" }}>
                  <Badge tone="muted">{t.kind}</Badge>
                  <b className="font-mono">{t.term}</b>
                  {t.replacement && (
                    <>
                      <span style={{ color: "var(--text-faint)" }}>→</span>
                      <span className="font-mono">{t.replacement}</span>
                    </>
                  )}
                  {(t.target_languages ?? []).map((l: string) => (
                    <Badge key={l} tone="info">{l}</Badge>
                  ))}
                  {t.case_sensitive && <Badge tone="warning">case-sensitive</Badge>}
                  <span className="ml-auto font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>
                    {fmtAgo(t.created_at)}
                  </span>
                  <ConfirmButton onConfirm={() => remove(t.id)} className="btn-outline !text-xs !py-0.5">
                    Delete
                  </ConfirmButton>
                </div>
              ))}
            </div>
          </Card>
        )}
      </Section>
    </div>
  );
}
