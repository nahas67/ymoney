import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { wsApi } from "../lib/api";
import { useFetch, useInterval } from "../hooks/hooks";
import {
  Accordion, Badge, Card, Empty, ErrorBox, Field, Loading,
  PageHeader, Progress, Section, Tabs, toast,
} from "../components/ui";
import { fmtAgo } from "../lib/format";

/* UGC Studio (Work 07): presets, projects + QC + render, custom avatars
   with consent gating, and lip-sync jobs + dubbing plans.
   Data-driven only — a 404/erroring route renders an explicit state. */

function is404(err: string | null): boolean {
  return !!err && /404|not found/i.test(err);
}

function panelError(err: string | null, fallback: string): string {
  if (!err) return "";
  return is404(err) ? `${fallback} — API unavailable on this backend (404).` : `${fallback} ${err}`;
}

function statusToneLocal(status?: string): "success" | "warning" | "error" | "info" | "muted" {
  const s = (status || "").toUpperCase();
  if (["READY", "COMPLETED", "RENDERED", "SUCCEEDED", "PASS", "AUTHORIZED", "AVAILABLE"].includes(s)) return "success";
  if (["FAILED", "CANCELLED", "FAIL", "ERROR", "REVOKED", "UNAVAILABLE"].includes(s)) return "error";
  if (["RUNNING", "QUEUED", "RENDERING", "RETRYING", "REVIEW_REQUIRED", "PASS_WITH_WARNINGS", "PENDING", "DEGRADED"].includes(s)) return "warning";
  if (["DRAFT", "PRODUCTION"].includes(s)) return "info";
  return "muted";
}

function qcTone(status?: string): "success" | "warning" | "error" | "muted" {
  const s = (status || "").toUpperCase();
  if (s === "PASS") return "success";
  if (s === "PASS_WITH_WARNINGS" || s === "REVIEW_REQUIRED") return "warning";
  if (s === "FAIL") return "error";
  return "muted";
}

/* UGC QC checks are a dict: {name: {status, detail}} */
function QCChecks({ checks }: { checks: any }) {
  const entries = Object.entries(checks ?? {});
  if (!entries.length) {
    return <div className="text-[12.5px]" style={{ color: "var(--text-faint)" }}>No QC checks recorded.</div>;
  }
  const flagged = entries.filter(([, v]: any) => String(v?.status ?? "").toLowerCase() !== "pass");
  const shown = flagged.length ? flagged : entries;
  return (
    <div className="space-y-1.5">
      {flagged.length === 0 && (
        <div className="text-[12.5px]" style={{ color: "var(--accent)" }}>All {entries.length} checks passed.</div>
      )}
      {shown.map(([name, v]: any) => (
        <div key={name} className="flex gap-2 items-start text-[12.5px] flex-wrap">
          <Badge tone={String(v?.status).toLowerCase() === "fail" ? "error"
            : String(v?.status).toLowerCase() === "warning" ? "warning" : "success"}>
            {v?.status}
          </Badge>
          <code className="text-[12px]">{name}</code>
          {v?.detail && <span style={{ color: "var(--text-muted)" }}>{v.detail}</span>}
        </div>
      ))}
    </div>
  );
}

export default function Ugc() {
  const [tab, setTab] = useState<"projects" | "presets" | "avatars" | "lipsync">("projects");

  return (
    <div className="space-y-4">
      <PageHeader title="UGC Studio"
        subtitle="UGC-style video projects, the nine presets, consent-gated avatars, and lip-sync jobs." />
      <Tabs tabs={[
        { key: "projects", label: "Projects" },
        { key: "presets", label: "Presets" },
        { key: "avatars", label: "Avatars" },
        { key: "lipsync", label: "Lip-sync & Dubbing" },
      ]} active={tab} onChange={setTab} />
      {tab === "projects" && <ProjectsPanel />}
      {tab === "presets" && <PresetsPanel />}
      {tab === "avatars" && <AvatarsPanel />}
      {tab === "lipsync" && <LipSyncPanel />}
    </div>
  );
}

/* ---- Presets ---- */

function PresetsPanel() {
  const presets = useFetch(() => wsApi.get("/ugc/presets"), []);
  const items: any[] = (presets.data as any)?.items ?? [];

  return (
    <div className="space-y-3">
      {presets.error && !presets.data && (
        <ErrorBox error={panelError(presets.error, "Presets failed to load.")} onRetry={presets.reload} />
      )}
      {presets.loading && !presets.data && <Loading rows={3} />}
      {presets.data && !items.length && (
        <Card><Empty title="No presets registered" hint="The backend returned an empty preset list." /></Card>
      )}
      {items.length > 0 && (
        <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-3">
          {items.map((p: any) => (
            <Card key={p.key}>
              <div className="flex items-center gap-2 flex-wrap">
                <b className="text-[13.5px] font-mono">{p.key}</b>
                {p.duration_seconds != null && <Badge tone="muted">{p.duration_seconds}s</Badge>}
              </div>
              {p.hook_type && <div className="text-[12px] mt-1" style={{ color: "var(--text-muted)" }}>hook: {p.hook_type}</div>}
              {p.format && <div className="text-[12.5px] mt-1">{p.format}</div>}
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}

/* ---- Projects: create + list + detail ---- */

function ProjectsPanel() {
  const nav = useNavigate();
  const list = useFetch(() => wsApi.get("/ugc/projects"), []);
  const presets = useFetch(() => wsApi.get("/ugc/presets"), []);
  const presetItems: any[] = (presets.data as any)?.items ?? [];
  const items: any[] = (list.data as any)?.items ?? [];
  const [sel, setSel] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);

  const anyActive = items.some((p) => ["RUNNING", "PRODUCTION"].includes(String(p.status || "").toUpperCase()));
  useInterval(() => list.reload(), anyActive ? 4000 : null);

  async function onCreate(projectId: string) {
    setShowForm(false);
    setSel(projectId);
    list.reload();
  }

  return (
    <div className="space-y-4">
      <Card>
        <div className="flex items-center gap-2 flex-wrap">
          <b className="text-[14px]">New UGC project</b>
          <Badge tone="info">{items.length} total</Badge>
          <span className="ml-auto flex gap-2">
            <button className="btn-ghost !text-xs !py-1" onClick={list.reload}>Refresh</button>
            <button className="btn-primary !text-xs !py-1" onClick={() => setShowForm((v) => !v)}>
              {showForm ? "Close" : "+ New project"}
            </button>
          </span>
        </div>
        {showForm && (
          <ProjectCreateForm
            presets={presetItems}
            presetsError={presets.error}
            onCreated={onCreate}
          />
        )}
      </Card>

      <div className="grid lg:grid-cols-[340px_1fr] gap-4">
        <Card pad={false} className="overflow-hidden">
          <div className="px-4 py-3 text-[13px] font-semibold" style={{ borderBottom: "var(--seam)" }}>
            Projects <span className="font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>{items.length}</span>
          </div>
          {list.loading && !list.data && <div className="p-3"><Loading rows={3} /></div>}
          {list.error && !list.data && <div className="p-3"><ErrorBox error={list.error} onRetry={list.reload} /></div>}
          {list.data && !items.length && (
            <div className="p-4 text-[13px]" style={{ color: "var(--text-muted)" }}>
              No UGC projects yet — create one above.
            </div>
          )}
          {items.map((p: any) => (
            <button key={p.id} onClick={() => setSel(p.id)} className="w-full text-left px-3 py-2.5"
              style={{ borderBottom: "var(--seam)", background: sel === p.id ? "var(--seam)" : undefined }}>
              <div className="flex items-center gap-2 flex-wrap">
                <b className="text-[13px] font-mono">{p.preset}</b>
                <Badge tone={statusToneLocal(p.status)}>{p.status}</Badge>
              </div>
              <div className="flex items-center gap-2 mt-1 flex-wrap">
                {p.qc?.status && <Badge tone={qcTone(p.qc.status)}>QC {p.qc.status}</Badge>}
                {p.timeline_id && <Badge tone="info">timeline</Badge>}
                <span className="font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>{fmtAgo(p.created_at)}</span>
              </div>
            </button>
          ))}
        </Card>

        {sel ? (
          <ProjectDetail key={sel} projectId={sel}
            onChanged={() => list.reload()}
            onOpenEditor={(tid) => nav(`/editor/${tid}`)} />
        ) : (
          <Card>
            <div className="text-[13px]" style={{ color: "var(--text-muted)" }}>
              Select a project to inspect its QC report, render it, or open its timeline in the editor.
            </div>
          </Card>
        )}
      </div>
    </div>
  );
}

function ProjectCreateForm({
  presets, presetsError, onCreated,
}: {
  presets: any[];
  presetsError: string | null;
  onCreated: (projectId: string) => void;
}) {
  const [preset, setPreset] = useState("");
  const [topic, setTopic] = useState("");
  const [audience, setAudience] = useState("");
  const [tone, setTone] = useState("");
  const [assets, setAssets] = useState("");
  const [variants, setVariants] = useState("");
  const [run, setRun] = useState(true);
  const [busy, setBusy] = useState(false);

  async function submit() {
    if (!preset) { toast("Pick a preset", "warning"); return; }
    if (!topic.trim()) { toast("A topic/product name is required", "warning"); return; }
    setBusy(true);
    try {
      const brief: any = {
        audience: audience.trim(),
        tone: tone.trim(),
        product_assets: assets.split(",").map((s) => s.trim()).filter(Boolean),
        variants: variants.split(",").map((s) => s.trim()).filter(Boolean),
      };
      if (topic.trim()) brief.topic = topic.trim();
      const r: any = await wsApi.post("/ugc/projects", { preset, brief, run });
      const proj = r?.project;
      if (r?.ran) {
        const qcStatus = r?.qc?.status ?? proj?.qc?.status ?? "UNKNOWN";
        toast(`Project generated — QC ${qcStatus}`, qcTone(qcStatus) === "error" ? "warning" : "success");
      } else {
        toast("Draft saved (not run)", "info");
      }
      if (proj?.id) onCreated(proj.id);
      else onCreated("");
    } catch (e: any) {
      toast(e?.message ?? "create failed", "error", "Project creation failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid md:grid-cols-2 gap-x-4 mt-3">
      <Field label="Preset" hint={presetsError ? `Preset list unavailable: ${presetsError}` : "One of the registered presets."}>
        <select className="select !text-xs" value={preset} onChange={(e) => setPreset(e.target.value)}>
          <option value="">— select preset —</option>
          {presets.map((p: any) => <option key={p.key} value={p.key}>{p.key}</option>)}
        </select>
      </Field>
      <Field label="Topic / product name" hint="Nothing product-specific is ever invented for you.">
        <input className="input !text-xs" value={topic} placeholder="e.g. YMoney expense cards"
          onChange={(e) => setTopic(e.target.value)} />
      </Field>
      <Field label="Audience">
        <input className="input !text-xs" value={audience} placeholder="e.g. freelance designers"
          onChange={(e) => setAudience(e.target.value)} />
      </Field>
      <Field label="Tone">
        <input className="input !text-xs" value={tone} placeholder="e.g. casual, upbeat"
          onChange={(e) => setTone(e.target.value)} />
      </Field>
      <Field label="Product asset refs" hint="Comma-separated workspace asset refs.">
        <input className="input font-mono !text-xs" value={assets} placeholder="asset id…, uploads/clip.mp4"
          onChange={(e) => setAssets(e.target.value)} />
      </Field>
      <Field label="Variants" hint="Comma-separated angle/variant labels.">
        <input className="input !text-xs" value={variants} placeholder="price-led, feature-led"
          onChange={(e) => setVariants(e.target.value)} />
      </Field>
      <label className="flex items-center gap-2 text-[12.5px] mb-3" style={{ color: "var(--text-muted)" }}>
        <input type="checkbox" checked={run} onChange={(e) => setRun(e.target.checked)} />
        Run the pipeline now (uncheck to save a DRAFT)
      </label>
      <div className="flex items-end mb-3">
        <button className="btn-primary !text-xs" disabled={busy || !preset || !topic.trim()} onClick={submit}>
          {busy ? "Creating…" : "Create project"}
        </button>
      </div>
      {!presets.length && !presetsError && (
        <div className="text-[12.5px] md:col-span-2" style={{ color: "var(--text-faint)" }}>
          No presets loaded yet.
        </div>
      )}
    </div>
  );
}

function ProjectDetail({
  projectId, onChanged, onOpenEditor,
}: {
  projectId: string;
  onChanged: () => void;
  onOpenEditor: (timelineId: string) => void;
}) {
  const detail = useFetch(() => wsApi.get(`/ugc/projects/${projectId}`), [projectId]);
  const [busy, setBusy] = useState("");
  const p: any = detail.data;

  const active = p && ["RUNNING", "PRODUCTION"].includes(String(p.status || "").toUpperCase());
  useInterval(() => detail.reload(), active ? 3000 : null);

  async function render() {
    setBusy("render");
    try {
      const r: any = await wsApi.post(`/ugc/projects/${projectId}/render`, {});
      const asset = r?.render?.asset_id ?? r?.project?.render_asset_ref ?? "";
      toast(`Rendered${asset ? ` — ${asset}` : ""}`, "success");
      detail.reload();
      onChanged();
    } catch (e: any) {
      // 409 = QC gate refused; 422 = pipeline error — surface verbatim
      toast(e?.message ?? "render failed", "error",
        e?.status === 409 ? "Render refused (QC gate)" : "Render failed");
      detail.reload();
    } finally {
      setBusy("");
    }
  }

  if (detail.loading && !p) return <Card><Loading rows={4} /></Card>;
  if (detail.error && !p) return <ErrorBox error={panelError(detail.error, "Project failed to load.")} onRetry={detail.reload} />;
  if (!p) return null;

  const qc: any = p.qc ?? {};
  const assetsInfo: any = p.product_assets ?? {};

  return (
    <div className="space-y-3">
      <Card>
        <div className="flex items-center gap-2 flex-wrap">
          <b className="text-[14px] font-mono">{p.preset}</b>
          <Badge tone={statusToneLocal(p.status)}>{p.status}</Badge>
          {qc.status && <Badge tone={qcTone(qc.status)}>QC {qc.status}</Badge>}
          <span className="ml-auto font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>
            {fmtAgo(p.updated_at || p.created_at)}
          </span>
        </div>
        <div className="flex gap-2 mt-3 flex-wrap">
          {p.timeline_id && (
            <button className="btn-primary !text-xs" onClick={() => onOpenEditor(p.timeline_id)}>
              🎞 Open in Editor
            </button>
          )}
          <button className="btn-outline !text-xs" disabled={!!busy} onClick={render}>
            {busy === "render" ? "Rendering…" : "Render"}
          </button>
          <button className="btn-ghost !text-xs" onClick={() => { detail.reload(); onChanged(); }}>Refresh</button>
        </div>
        {!p.timeline_id && (
          <div className="text-[12.5px] mt-2" style={{ color: "var(--text-faint)" }}>
            No timeline yet — generate the project to produce one.
          </div>
        )}
        {p.render_asset_ref && (
          <div className="text-[12px] mt-2 font-mono" style={{ color: "var(--text-muted)" }}>
            rendered: {p.render_asset_ref}
          </div>
        )}
      </Card>

      <Card>
        <b className="text-[14px]">QC report</b>
        <div className="mt-2"><QCChecks checks={qc.checks} /></div>
        {qc.regeneration && (
          <div className="text-[12.5px] mt-2" style={{ color: "var(--warn)" }}>
            regeneration: {JSON.stringify(qc.regeneration)}
          </div>
        )}
      </Card>

      <Card>
        <b className="text-[14px]">Brief & assets</b>
        <div className="mt-2 space-y-1.5 text-[12.5px]">
          {Object.entries(p.brief ?? {}).length === 0 && (
            <div style={{ color: "var(--text-faint)" }}>Empty brief.</div>
          )}
          {Object.entries(p.brief ?? {}).map(([k, v]) => (
            <div key={k} className="flex gap-2 flex-wrap">
              <code className="text-[12px] min-w-[120px]" style={{ color: "var(--text-muted)" }}>{k}</code>
              <span className="font-mono break-words">{typeof v === "string" ? v : JSON.stringify(v)}</span>
            </div>
          ))}
          {(assetsInfo.resolved?.length > 0 || assetsInfo.unresolved?.length > 0) && (
            <div className="flex gap-2 flex-wrap pt-1">
              <Badge tone="success">resolved: {(assetsInfo.resolved ?? []).length}</Badge>
              {(assetsInfo.unresolved ?? []).length > 0 && (
                <Badge tone="warning">unresolved: {(assetsInfo.unresolved ?? []).map((u: any) => String(u)).join(", ")}</Badge>
              )}
            </div>
          )}
        </div>
        <Accordion title="Lineage">
          <pre className="text-[11.5px] font-mono whitespace-pre-wrap max-h-[260px] overflow-y-auto"
            style={{ color: "var(--text-muted)" }}>
            {JSON.stringify(p.lineage ?? {}, null, 2)}
          </pre>
        </Accordion>
      </Card>
    </div>
  );
}

/* ---- Avatars: health, list, create, authorize, render ---- */

function AvatarsPanel() {
  const nav = useNavigate();
  const health = useFetch(() => wsApi.get("/avatars/health"), []);
  const list = useFetch(() => wsApi.get("/avatars"), []);
  const items: any[] = (list.data as any)?.items ?? [];
  const h: any = health.data;
  const [showCreate, setShowCreate] = useState(false);
  const [authId, setAuthId] = useState<string | null>(null);
  const [renderId, setRenderId] = useState<string | null>(null);
  const [busy, setBusy] = useState("");
  const [renderError, setRenderError] = useState<string | null>(null);
  const [renderResult, setRenderResult] = useState<any>(null);

  async function createAvatar(fields: any) {
    setBusy("create");
    try {
      await wsApi.post("/avatars", fields);
      toast("Avatar created — consent starts pending", "success");
      setShowCreate(false);
      list.reload();
    } catch (e: any) {
      toast(e?.message ?? "create failed", "error", "Avatar creation failed");
    } finally {
      setBusy("");
    }
  }

  async function authorize(id: string, fields: any) {
    setBusy(`auth:${id}`);
    try {
      await wsApi.post(`/avatars/${id}/authorize`, fields);
      toast("Avatar authorized for rendering", "success");
      setAuthId(null);
      list.reload();
    } catch (e: any) {
      toast(e?.message ?? "authorize failed", "error", "Authorization failed");
    } finally {
      setBusy("");
    }
  }

  async function render(profileId: string, audioRef: string) {
    setBusy(`render:${profileId}`);
    setRenderError(null);
    setRenderResult(null);
    try {
      const r: any = await wsApi.post("/avatars/render", { profile_id: profileId, audio_ref: audioRef, timeline: true });
      setRenderResult(r);
      toast("Avatar render completed", "success");
      list.reload();
    } catch (e: any) {
      // 403 = consent gate rejected before any work; 503 = backend not configured
      setRenderError(e?.message ?? "render failed");
      toast(e?.message ?? "render failed", "error", "Avatar render rejected");
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="space-y-4">
      <Card>
        <div className="flex items-center gap-2 flex-wrap">
          <b className="text-[14px]">Provider health</b>
          {h && <Badge tone={h.ready ? "success" : "error"}>{h.ready ? "READY" : "NOT READY"}</Badge>}
          {h?.provider && <Badge tone="muted">{h.provider}</Badge>}
          <span className="ml-auto">
            <button className="btn-ghost !text-xs !py-1" onClick={health.reload}>Refresh</button>
          </span>
        </div>
        {health.loading && !health.data && <div className="mt-2"><Loading rows={1} /></div>}
        {health.error && !health.data && (
          <div className="text-[12.5px] mt-2" style={{ color: "var(--danger)" }}>
            {panelError(health.error, "Avatar health unavailable.")}
            <button className="btn-outline !text-xs ml-3" onClick={health.reload}>Retry</button>
          </div>
        )}
        {h && (
          <div className="mt-2 space-y-1.5 text-[12.5px]">
            {h.detail && <div style={{ color: "var(--text-muted)" }}>{h.detail}</div>}
            <div className="flex gap-1.5 flex-wrap">
              <Badge tone={h.consent_required ? "warning" : "muted"}>
                consent {h.consent_required ? "required" : "optional"}
              </Badge>
              {(h.consent_states ?? []).map((s: string) => <Badge key={s} tone="muted">{s}</Badge>)}
            </div>
            {h.capabilities && Object.keys(h.capabilities).length > 0 && (
              <div className="font-mono text-[11.5px]" style={{ color: "var(--text-faint)" }}>
                {Object.entries(h.capabilities).map(([k, v]) => `${k}:${String(v)}`).join(" · ")}
              </div>
            )}
          </div>
        )}
      </Card>

      <Card>
        <div className="flex items-center gap-2 flex-wrap">
          <b className="text-[14px]">Avatar profiles</b>
          <Badge tone="info">{items.length} total</Badge>
          <span className="ml-auto">
            <button className="btn-primary !text-xs !py-1" onClick={() => setShowCreate((v) => !v)}>
              {showCreate ? "Close" : "+ New avatar"}
            </button>
          </span>
        </div>
        {showCreate && <AvatarCreateForm busy={busy === "create"} onCreate={createAvatar} />}
      </Card>

      {renderError && (
        <Card style={{ borderColor: "var(--danger)" }}>
          <div className="text-[13px] font-medium" style={{ color: "var(--danger)" }}>Render rejected</div>
          <div className="text-[12.5px] mt-1 font-mono break-words" style={{ color: "var(--text-muted)" }}>{renderError}</div>
        </Card>
      )}
      {renderResult && (
        <Card>
          <div className="flex items-center gap-2 flex-wrap">
            <b className="text-[14px]">Last render</b>
            <Badge tone="success">{renderResult.status ?? "done"}</Badge>
            {renderResult.consent_state && <Badge tone="muted">consent: {renderResult.consent_state}</Badge>}
            {renderResult.is_mock && <Badge tone="warning">mock backend</Badge>}
          </div>
          <div className="flex gap-2 mt-2 flex-wrap text-[12px] font-mono" style={{ color: "var(--text-muted)" }}>
            {renderResult.asset_id && <span>asset: {renderResult.asset_id}</span>}
            {renderResult.timeline_id && (
              <button className="btn-outline !text-xs !py-0.5"
                onClick={() => nav(`/editor/${renderResult.timeline_id}`)}>
                🎞 Open in Editor
              </button>
            )}
          </div>
        </Card>
      )}

      <Section data={items} loading={list.loading} error={list.error} onRetry={list.reload}
        empty="No avatar profiles yet"
        emptyHint="Create a portrait profile — consent starts pending and rendering stays blocked until you authorize it.">
        {(rows) => (
          <div className="space-y-3">
            {rows.map((a: any) => {
              const authorized = String(a.consent_state || "").toLowerCase() === "authorized";
              return (
                <Card key={a.id}>
                  <div className="flex items-center gap-2 flex-wrap">
                    <b className="text-[13.5px]">{a.name || a.id.slice(0, 8)}</b>
                    <Badge tone={authorized ? "success" : a.consent_state === "revoked" ? "error" : "warning"}>
                      consent: {a.consent_state || "unknown"}
                    </Badge>
                    {a.status && <Badge tone={statusToneLocal(a.status)}>{a.status}</Badge>}
                    {a.provider && <Badge tone="muted">{a.provider}</Badge>}
                    <span className="ml-auto font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>
                      {fmtAgo(a.created_at)}
                    </span>
                  </div>
                  <div className="font-mono text-[12px] mt-1" style={{ color: "var(--text-muted)" }}>
                    source: {a.source_asset_ref}
                    {a.consent?.source ? ` · consent source: ${a.consent.source}` : ""}
                  </div>
                  <div className="flex gap-2 mt-2 flex-wrap">
                    {!authorized && (
                      <button className="btn-primary !text-xs !py-0.5"
                        onClick={() => setAuthId(authId === a.id ? null : a.id)}>
                        {authId === a.id ? "Close" : "Authorize…"}
                      </button>
                    )}
                    <button className="btn-outline !text-xs !py-0.5"
                      onClick={() => setRenderId(renderId === a.id ? null : a.id)}>
                      {renderId === a.id ? "Close" : "Render…"}
                    </button>
                  </div>
                  {authId === a.id && (
                    <AuthorizeForm busy={busy === `auth:${a.id}`}
                      onSubmit={(f) => authorize(a.id, f)} />
                  )}
                  {renderId === a.id && (
                    <RenderForm busy={busy === `render:${a.id}`}
                      onSubmit={(audioRef) => render(a.id, audioRef)}
                      unauthorized={!authorized} />
                  )}
                </Card>
              );
            })}
          </div>
        )}
      </Section>
    </div>
  );
}

function AvatarCreateForm({ busy, onCreate }: { busy: boolean; onCreate: (f: any) => void }) {
  const [name, setName] = useState("");
  const [source, setSource] = useState("");
  const [voice, setVoice] = useState("");

  return (
    <div className="grid md:grid-cols-3 gap-x-4 mt-3">
      <Field label="Name">
        <input className="input !text-xs" value={name} placeholder="Brand spokesperson"
          onChange={(e) => setName(e.target.value)} />
      </Field>
      <Field label="Source asset ref" hint="Required — the portrait asset.">
        <input className="input font-mono !text-xs" value={source} placeholder="asset id / path…"
          onChange={(e) => setSource(e.target.value)} />
      </Field>
      <Field label="Voice ref" hint="Optional.">
        <input className="input font-mono !text-xs" value={voice} placeholder="voice id…"
          onChange={(e) => setVoice(e.target.value)} />
      </Field>
      <div className="md:col-span-3">
        <button className="btn-primary !text-xs" disabled={busy || !source.trim()}
          onClick={() => onCreate({ name: name.trim(), source_asset_ref: source.trim(), voice_ref: voice.trim() })}>
          {busy ? "Creating…" : "Create avatar (consent pending)"}
        </button>
      </div>
    </div>
  );
}

function AuthorizeForm({ busy, onSubmit }: { busy: boolean; onSubmit: (f: any) => void }) {
  const [source, setSource] = useState("");
  const [grantedBy, setGrantedBy] = useState("");
  const [statement, setStatement] = useState("");

  return (
    <div className="grid md:grid-cols-2 gap-x-4 mt-3 p-3 rounded-xl" style={{ border: "var(--seam)", background: "var(--bg-inset)" }}>
      <div className="md:col-span-2 text-[12px] font-semibold" style={{ color: "var(--warn)" }}>
        Consent authorization — record where consent came from.
      </div>
      <Field label="Consent source" hint="Required — URL, contract id, or channel where consent was given.">
        <input className="input !text-xs" value={source} placeholder="e.g. signed release #221"
          onChange={(e) => setSource(e.target.value)} />
      </Field>
      <Field label="Granted by">
        <input className="input !text-xs" value={grantedBy} placeholder="e.g. Jane Doe, brand owner"
          onChange={(e) => setGrantedBy(e.target.value)} />
      </Field>
      <Field label="Statement" hint="Optional consent statement.">
        <input className="input !text-xs" value={statement} placeholder="I consent to…"
          onChange={(e) => setStatement(e.target.value)} />
      </Field>
      <div className="flex items-end mb-3">
        <button className="btn-primary !text-xs" disabled={busy || !source.trim()}
          onClick={() => onSubmit({ source: source.trim(), granted_by: grantedBy.trim(), statement: statement.trim(), authorization_evidence: {} })}>
          {busy ? "Authorizing…" : "Authorize"}
        </button>
      </div>
    </div>
  );
}

function RenderForm({ busy, onSubmit, unauthorized }: { busy: boolean; onSubmit: (audioRef: string) => void; unauthorized: boolean }) {
  const [audio, setAudio] = useState("");

  return (
    <div className="mt-3 p-3 rounded-xl" style={{ border: "var(--seam)", background: "var(--bg-inset)" }}>
      {unauthorized && (
        <div className="text-[12.5px] mb-2" style={{ color: "var(--warn)" }}>
          Consent is not authorized — the backend rejects this render with 403 before doing any work.
        </div>
      )}
      <div className="grid md:grid-cols-[1fr_auto] gap-2 items-end">
        <Field label="Audio ref" hint="Narration audio to lip-sync against.">
          <input className="input font-mono !text-xs" value={audio} placeholder="asset id / path…"
            onChange={(e) => setAudio(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && audio.trim() && onSubmit(audio.trim())} />
        </Field>
        <button className="btn-primary !text-xs mb-3" disabled={busy || !audio.trim()}
          onClick={() => onSubmit(audio.trim())}>
          {busy ? "Rendering…" : "Render avatar"}
        </button>
      </div>
    </div>
  );
}

/* ---- Lip-sync jobs + dubbing plans ---- */

function LipSyncPanel() {
  const health = useFetch(() => wsApi.get("/lipsync/health"), []);
  const jobs = useFetch(() => wsApi.get("/lipsync/jobs"), []);
  const items: any[] = (jobs.data as any)?.items ?? [];
  const h: any = health.data;
  const [video, setVideo] = useState("");
  const [audio, setAudio] = useState("");
  const [provider, setProvider] = useState("");
  const [busy, setBusy] = useState("");
  const [submitError, setSubmitError] = useState<string | null>(null);

  const anyActive = items.some((j) => ["QUEUED", "RUNNING"].includes(String(j.status || "").toUpperCase()));
  useInterval(() => { jobs.reload(); health.reload(); }, anyActive ? 4000 : null);

  async function submit() {
    if (!video.trim() || !audio.trim()) { toast("Video and audio refs are required", "warning"); return; }
    setBusy("submit");
    setSubmitError(null);
    try {
      await wsApi.post("/lipsync/jobs", {
        video_ref: video.trim(),
        audio_ref: audio.trim(),
        provider: provider.trim(),
        opts: {},
      });
      toast("Lip-sync job queued", "success");
      setVideo(""); setAudio("");
      jobs.reload();
    } catch (e: any) {
      setSubmitError(e?.message ?? "submit failed");
      toast(e?.message ?? "submit failed", "error", "Lip-sync rejected");
    } finally {
      setBusy("");
    }
  }

  async function cancel(id: string) {
    setBusy(`cancel:${id}`);
    try {
      await wsApi.post(`/lipsync/jobs/${id}/cancel`, {});
      toast("Cancel requested", "success");
      jobs.reload();
    } catch (e: any) {
      toast(e?.message ?? "cancel failed", "error", "Cancel failed");
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="space-y-4">
      <Card>
        <div className="flex items-center gap-2 flex-wrap">
          <b className="text-[14px]">Lip-sync health</b>
          {h && <Badge tone={statusToneLocal(h.status)}>{h.status ?? (h.available ? "AVAILABLE" : "UNAVAILABLE")}</Badge>}
          {h?.provider && <Badge tone="muted">{h.provider}</Badge>}
          <span className="ml-auto">
            <button className="btn-ghost !text-xs !py-1" onClick={health.reload}>Refresh</button>
          </span>
        </div>
        {health.loading && !health.data && <div className="mt-2"><Loading rows={1} /></div>}
        {health.error && !health.data && (
          <div className="text-[12.5px] mt-2" style={{ color: "var(--danger)" }}>
            {panelError(health.error, "Lip-sync health unavailable.")}
            <button className="btn-outline !text-xs ml-3" onClick={health.reload}>Retry</button>
          </div>
        )}
        {h && (
          <div className="mt-2 space-y-1.5 text-[12.5px]">
            {h.detail && <div style={{ color: "var(--text-muted)" }}>{h.detail}</div>}
            {!h.available && h.remediation && (
              <div style={{ color: "var(--warn)" }}>remediation: {h.remediation}</div>
            )}
            {h.queue && (
              <div className="font-mono text-[11.5px]" style={{ color: "var(--text-faint)" }}>
                queue: {Object.entries(h.queue).map(([k, v]) => `${k}:${String(v)}`).join(" · ")}
              </div>
            )}
            {h.checks && Object.keys(h.checks).length > 0 && (
              <div className="font-mono text-[11.5px]" style={{ color: "var(--text-faint)" }}>
                {Object.entries(h.checks).map(([k, v]) => `${k}:${String(v)}`).join(" · ")}
              </div>
            )}
          </div>
        )}
      </Card>

      <Card>
        <b className="text-[14px]">Submit lip-sync job</b>
        <div className="grid md:grid-cols-3 gap-x-4 mt-2">
          <Field label="Video ref" hint="Source video asset ref.">
            <input className="input font-mono !text-xs" value={video} placeholder="asset id / path…"
              onChange={(e) => setVideo(e.target.value)} />
          </Field>
          <Field label="Audio ref" hint="Driving audio asset ref.">
            <input className="input font-mono !text-xs" value={audio} placeholder="asset id / path…"
              onChange={(e) => setAudio(e.target.value)} />
          </Field>
          <Field label="Provider" hint="Optional — defaults to the active queue provider.">
            <input className="input font-mono !text-xs" value={provider} placeholder="e.g. musetalk"
              onChange={(e) => setProvider(e.target.value)} />
          </Field>
        </div>
        <button className="btn-primary !text-xs" disabled={busy === "submit" || !video.trim() || !audio.trim()} onClick={submit}>
          {busy === "submit" ? "Submitting…" : "Submit job"}
        </button>
        {submitError && (
          <div className="text-[12.5px] mt-2 font-mono break-words" style={{ color: "var(--danger)" }}>{submitError}</div>
        )}
      </Card>

      <Section data={items} loading={jobs.loading} error={jobs.error} onRetry={jobs.reload}
        empty="No lip-sync jobs yet"
        emptyHint="Submit a video + audio pair above — jobs fail closed with a remediation message when no backend is ready.">
        {(rows) => (
          <Card>
            <div className="flex items-center gap-2 flex-wrap">
              <b className="text-[14px]">Jobs</b>
              <Badge tone="info">{rows.length} total</Badge>
              <span className="ml-auto">
                <button className="btn-ghost !text-xs !py-1" onClick={jobs.reload}>Refresh</button>
              </span>
            </div>
            <div className="mt-3 space-y-2.5">
              {rows.map((j: any) => {
                const active = ["QUEUED", "RUNNING"].includes(String(j.status || "").toUpperCase());
                return (
                  <div key={j.id} className="p-2.5 rounded-lg" style={{ border: "var(--seam)", background: "var(--bg-inset)" }}>
                    <div className="flex items-center gap-2 flex-wrap">
                      <code className="text-[11.5px]">{String(j.id).slice(0, 8)}</code>
                      <Badge tone={statusToneLocal(j.status)}>{j.status}</Badge>
                      {j.provider && <Badge tone="muted">{j.provider}</Badge>}
                      <span className="font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>
                        {fmtAgo(j.created_at)}
                      </span>
                      {active && (
                        <button className="btn-outline !text-xs !py-0.5 ml-auto" disabled={busy === `cancel:${j.id}`}
                          onClick={() => cancel(j.id)}>
                          {busy === `cancel:${j.id}` ? "Cancelling…" : "Cancel"}
                        </button>
                      )}
                    </div>
                    {active && (
                      <div className="mt-2"><Progress value={Math.round((j.progress ?? 0) * 100)} /></div>
                    )}
                    <div className="font-mono text-[11.5px] mt-1.5 break-words" style={{ color: "var(--text-muted)" }}>
                      video: {j.video_ref} · audio: {j.audio_ref}
                      {j.result_asset_ref ? ` · result: ${j.result_asset_ref}` : ""}
                    </div>
                    {j.error && (
                      <div className="text-[12px] mt-1 font-mono break-words" style={{ color: "var(--danger)" }}>{j.error}</div>
                    )}
                  </div>
                );
              })}
            </div>
          </Card>
        )}
      </Section>

      <DubbingPlans />
    </div>
  );
}

function DubbingPlans() {
  const plans = useFetch(() => wsApi.get("/dubbing/plans"), []);
  const items: any[] = (plans.data as any)?.items ?? [];

  return (
    <Section data={items} loading={plans.loading} error={plans.error} onRetry={plans.reload}
      empty="No dubbing plans yet"
      emptyHint="Speaker-aware dubbing plans are built by the pipeline from cue sheets.">
      {(rows) => (
        <Card>
          <div className="flex items-center gap-2 flex-wrap">
            <b className="text-[14px]">Dubbing plans</b>
            <Badge tone="info">{rows.length} saved</Badge>
            <span className="ml-auto">
              <button className="btn-ghost !text-xs !py-1" onClick={plans.reload}>Refresh</button>
            </span>
          </div>
          <div className="mt-3 space-y-2">
            {rows.map((pl: any) => (
              <div key={pl.id} className="flex items-center gap-2 flex-wrap text-[12.5px] p-2 rounded-lg"
                style={{ border: "var(--seam)", background: "var(--bg-inset)" }}>
                <code className="text-[11.5px]">{String(pl.id).slice(0, 8)}</code>
                <Badge tone="muted">{pl.target_language || "—"}</Badge>
                <Badge tone={statusToneLocal(pl.status)}>{pl.status}</Badge>
                {pl.needs_review && <Badge tone="warning">needs review</Badge>}
                {pl.review_count > 0 && <Badge tone="muted">reviews: {pl.review_count}</Badge>}
                {pl.source_ref && (
                  <span className="font-mono text-[11px] truncate max-w-[220px]" style={{ color: "var(--text-muted)" }}>
                    {pl.source_ref}
                  </span>
                )}
                <span className="ml-auto font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>
                  {fmtAgo(pl.created_at)}
                </span>
              </div>
            ))}
          </div>
        </Card>
      )}
    </Section>
  );
}
