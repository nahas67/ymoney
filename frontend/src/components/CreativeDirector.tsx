/* CreativeDirector — Work 08 Lane B/C surface inside the editor.
 *
 * Flow (all workspace-scoped routes from backend/app/api/v1/creative.py):
 *   NL text → POST /creative/parse    → typed command list (no mutation)
 *          → POST /creative/preview   → ChangeSet diff + estimates (read-only)
 *          → POST /creative/apply     → versioned mutation (409 = stale preview)
 *          → POST /creative/undo/{tl} → restore the version apply replaced
 *   GET    /creative/commands         → audit ledger
 *   GET    /creative/catalog          → approved commands + component catalog
 *
 * Rendering of catalog-bound fields goes through CommandRenderer (whitelist
 * only — see the header comment there). No generated code is ever executed.
 */
import { useCallback, useEffect, useState } from "react";
import { wsApi } from "../lib/api";
import { Badge, Card, toast } from "./ui";
import { CommandRenderer, useCreativeCatalog } from "./CommandRenderer";

type Command = Record<string, any>;

type ChangeEntry = {
  index: number;
  type: string;
  scope?: string;
  target?: Record<string, unknown>;
  status: string;
  reasons?: string[];
  lines?: string[];
  affected?: { tracks?: string[]; clips?: string[]; scenes?: string[] };
  estimate?: { rerender_seconds?: number; cost_usd?: number; heuristic?: string };
  reversible?: boolean;
  approval_required?: boolean;
  auto_apply?: boolean;
  risk?: string;
};

type ChangeSet = {
  preview_id?: string;
  timeline_id?: string;
  timeline_version?: number | null;
  manifest_hash?: string;
  policy_source?: string;
  auto_apply_enabled?: boolean;
  changes?: ChangeEntry[];
  totals?: {
    rerender_seconds?: number;
    cost_usd?: number;
    commands?: number;
    rejected?: number;
    approval_required?: boolean;
    reversible?: boolean;
  };
  commands?: Command[];
};

type AuditRow = {
  id: string;
  status: string;
  text_input?: string;
  timeline_id?: string;
  created_at?: string;
  parent_version?: number | null;
  commands?: Command[];
};

function statusTone(s: string): string {
  if (s === "ok" || s === "applied" || s === "previewed") return "success";
  if (s === "rejected" || s === "stale") return "error";
  if (s === "parsed" || s === "undone") return "info";
  return "muted";
}

function riskTone(r?: string): string {
  return r === "high" ? "error" : r === "medium" ? "warning" : "success";
}

export default function CreativeDirector({
  timelineId,
  contentId,
  onApplied,
}: {
  timelineId: string;
  contentId?: string;
  onApplied?: () => void;
}) {
  const { catalog } = useCreativeCatalog();
  const [text, setText] = useState("");
  const [commands, setCommands] = useState<Command[]>([]);
  const [preview, setPreview] = useState<ChangeSet | null>(null);
  const [approve, setApprove] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [stale, setStale] = useState("");
  const [approvalHint, setApprovalHint] = useState("");
  const [audit, setAudit] = useState<AuditRow[]>([]);
  const [showAudit, setShowAudit] = useState(false);

  const loadAudit = useCallback(() => {
    if (!timelineId) return;
    wsApi
      .get(`/creative/commands?timeline_id=${encodeURIComponent(timelineId)}&limit=20`)
      .then((r: any) => setAudit(r.items ?? []))
      .catch(() => setAudit([]));
  }, [timelineId]);

  useEffect(() => {
    loadAudit();
  }, [loadAudit]);

  function fail(e: any): void {
    const msg = String(e?.message ?? e ?? "request failed");
    if (e?.status === 409) {
      setStale(
        "The timeline changed since this preview — reload the preview and review again before applying. Manually edited work is never overwritten."
      );
      setError("");
    } else if (msg.includes("APPROVAL_REQUIRED")) {
      setApprovalHint("This workspace requires approval for one or more commands — tick “Approve & apply” to continue.");
      setError(msg);
    } else {
      setError(msg);
    }
    toast(msg.slice(0, 160), "error", "Creative Director");
  }

  async function doParse() {
    if (!text.trim()) return;
    setBusy("parse");
    setError("");
    setStale("");
    setApprovalHint("");
    setPreview(null);
    try {
      const context: Record<string, unknown> = { timeline_id: timelineId };
      if (contentId) context.content_id = contentId;
      const r: any = await wsApi.post("/creative/parse", { text, context, persist: true });
      setCommands(r.commands ?? []);
      if (!(r.commands ?? []).length) {
        setError("No command matched that request — try phrasing like “change captions preset to pop” or “shorten to 20 seconds”.");
      }
      loadAudit();
    } catch (e: any) {
      fail(e);
    } finally {
      setBusy("");
    }
  }

  async function doPreview() {
    if (!commands.length) return;
    setBusy("preview");
    setError("");
    setStale("");
    try {
      const r: any = await wsApi.post("/creative/preview", { commands, text_input: text });
      setPreview(r);
      loadAudit();
    } catch (e: any) {
      fail(e);
    } finally {
      setBusy("");
    }
  }

  async function doApply() {
    if (!preview || !commands.length) return;
    setBusy("apply");
    setError("");
    setStale("");
    setApprovalHint("");
    try {
      const r: any = await wsApi.post("/creative/apply", {
        commands,
        preview_id: preview.preview_id ?? null,
        base_version: preview.timeline_version ?? null,
        approve,
        text_input: text,
      });
      toast(
        `Applied v${r?.version ?? "?"} (${(r?.commands ?? []).length || commands.length} command(s))`,
        "success",
        "Creative Director"
      );
      setPreview(null);
      setCommands([]);
      setApprove(false);
      loadAudit();
      onApplied?.();
    } catch (e: any) {
      fail(e);
      loadAudit();
    } finally {
      setBusy("");
    }
  }

  async function doUndo() {
    if (!timelineId) return;
    setBusy("undo");
    setError("");
    setStale("");
    try {
      const r: any = await wsApi.post(`/creative/undo/${timelineId}`, {});
      toast(`Restored version ${r?.version ?? ""}`, "success", "Undone");
      setPreview(null);
      setCommands([]);
      loadAudit();
      onApplied?.();
    } catch (e: any) {
      fail(e);
    } finally {
      setBusy("");
    }
  }

  const totals = preview?.totals;
  const changes = preview?.changes ?? [];

  return (
    <Card>
      <div className="flex items-center gap-2 flex-wrap mb-2">
        <b className="text-[13px]">✦ Creative Director</b>
        <Badge tone="muted">parse → preview → apply</Badge>
        {preview?.policy_source && <Badge tone="info">policy: {preview.policy_source}</Badge>}
        <button className="btn-ghost !text-xs ml-auto" onClick={() => { setShowAudit((v) => !v); loadAudit(); }}>
          {showAudit ? "Hide audit" : `Audit (${audit.length})`}
        </button>
        <button className="btn-ghost !text-xs" onClick={doUndo} disabled={busy === "undo" || !timelineId}>
          {busy === "undo" ? "Undoing…" : "↩ Undo last apply"}
        </button>
      </div>

      <textarea
        className="textarea w-full"
        rows={3}
        value={text}
        placeholder='Describe an edit… e.g. “change captions preset to pop”, “use a female voice”, “shorten to 20 seconds”, “hook to: You’re missing out on free money”'
        onChange={(e) => setText(e.target.value)}
      />
      <div className="flex gap-2 flex-wrap items-center mt-2">
        <button className="btn-primary !text-xs" disabled={busy === "parse" || !text.trim()} onClick={doParse}>
          {busy === "parse" ? "Parsing…" : "1 · Parse"}
        </button>
        <button className="btn-outline !text-xs" disabled={busy === "preview" || !commands.length} onClick={doPreview}>
          {busy === "preview" ? "Previewing…" : "2 · Preview"}
        </button>
        <label className="flex items-center gap-1.5 text-[12px]" style={{ color: "var(--text-muted)" }}>
          <input type="checkbox" checked={approve} onChange={(e) => setApprove(e.target.checked)} />
          Approve &amp; apply
        </label>
        <button className="btn-primary !text-xs" disabled={busy === "apply" || !preview || !commands.length} onClick={doApply}>
          {busy === "apply" ? "Applying…" : "3 · Apply"}
        </button>
        {totals && (
          <span className="flex items-center gap-1.5 ml-auto flex-wrap">
            <Badge tone="info">~{totals.rerender_seconds ?? 0}s rerender</Badge>
            <Badge tone="warning">~${totals.cost_usd ?? 0} est.</Badge>
            <Badge tone={totals.reversible ? "success" : "muted"}>{totals.reversible ? "reversible" : "not reversible"}</Badge>
            <Badge tone={totals.approval_required ? "warning" : "success"}>
              {totals.approval_required ? "approval required" : "auto-apply ok"}
            </Badge>
          </span>
        )}
      </div>

      {stale && (
        <div className="mt-3 rounded-lg px-3 py-2.5 text-[12.5px]" style={{ background: "var(--warn-dim)", color: "var(--warn)", border: "1px solid var(--warn)" }}>
          <b>Stale preview (409).</b> {stale}
          <button className="btn-outline !text-xs ml-2" onClick={() => { setPreview(null); setStale(""); doPreview(); }}>
            Refresh preview
          </button>
        </div>
      )}
      {approvalHint && !stale && (
        <div className="mt-3 rounded-lg px-3 py-2.5 text-[12.5px]" style={{ background: "var(--info-dim)", color: "var(--info)" }}>{approvalHint}</div>
      )}
      {error && !stale && (
        <div className="mt-3 rounded-lg px-3 py-2.5 text-[12.5px] font-mono break-words" style={{ background: "var(--danger-dim)", color: "var(--danger)" }}>
          {error}
        </div>
      )}

      {/* Parsed commands — fields rendered through the approved catalog only */}
      {commands.length > 0 && (
        <div className="mt-3 space-y-2">
          <div className="panel-label">Parsed commands ({commands.length})</div>
          {commands.map((c, i) => {
            const entry = (catalog?.commands ?? []).find((x) => x.type === c.type);
            return (
              <div key={i} className="rounded-lg p-3" style={{ border: "var(--seam)", background: "var(--bg-inset)" }}>
                <div className="flex items-center gap-2 flex-wrap">
                  <b className="text-[12.5px] font-mono">{c.type}</b>
                  <Badge tone={riskTone(entry?.risk)}>{entry?.risk ?? "medium"} risk</Badge>
                  <Badge tone="muted">{c.scope ?? "timeline"}</Badge>
                  {entry?.reversible === false && <Badge tone="warning">irreversible</Badge>}
                  {c.target?.platform && <Badge tone="info">for {c.target.platform}</Badge>}
                  {entry?.description && (
                    <span className="text-[11.5px]" style={{ color: "var(--text-faint)" }}>{entry.description}</span>
                  )}
                </div>
                <CommandRenderer command={c} catalog={catalog} onJump={() => undefined} />
              </div>
            );
          })}
        </div>
      )}

      {/* ChangeSet diff */}
      {preview && (
        <div className="mt-3">
          <div className="flex items-center gap-2 flex-wrap mb-1.5">
            <div className="panel-label">ChangeSet preview</div>
            <Badge tone="muted">v{preview.timeline_version ?? "?"}</Badge>
            {totals?.rejected ? <Badge tone="error">{totals.rejected} rejected</Badge> : null}
          </div>
          <div className="space-y-2">
            {changes.map((ch) => (
              <div key={ch.index} className="rounded-lg p-3" style={{ border: "var(--seam)", background: "var(--bg-inset)" }}>
                <div className="flex items-center gap-2 flex-wrap">
                  <Badge tone={statusTone(ch.status)}>{ch.status}</Badge>
                  <b className="text-[12.5px] font-mono">{ch.type}</b>
                  <Badge tone={riskTone(ch.risk)}>{ch.risk} risk</Badge>
                  {ch.reversible ? <Badge tone="success">reversible</Badge> : <Badge tone="warning">irreversible</Badge>}
                  <Badge tone={ch.approval_required ? "warning" : "success"}>
                    {ch.approval_required ? "approval required" : "no approval needed"}
                  </Badge>
                  {ch.auto_apply && <Badge tone="info">auto-apply</Badge>}
                </div>
                <div className="mt-1.5 space-y-0.5 text-[12.5px]">
                  {(ch.lines ?? []).map((line, li) => (
                    <div key={li} className="font-mono break-words">› {line}</div>
                  ))}
                </div>
                {(ch.affected?.tracks?.length || ch.affected?.clips?.length || ch.affected?.scenes?.length) ? (
                  <div className="mt-1.5 flex flex-wrap gap-1.5 items-center text-[11.5px]" style={{ color: "var(--text-muted)" }}>
                    <span style={{ color: "var(--text-faint)" }}>affects:</span>
                    {(ch.affected?.tracks ?? []).map((t) => <span key={`t${t}`} className="chip !text-[11px] !py-0.5">track:{t}</span>)}
                    {(ch.affected?.scenes ?? []).map((s) => <span key={`s${s}`} className="chip !text-[11px] !py-0.5">scene {s}</span>)}
                    {(ch.affected?.clips ?? []).slice(0, 6).map((c) => <span key={`c${c}`} className="chip !text-[11px] !py-0.5">clip:{String(c).slice(0, 8)}</span>)}
                  </div>
                ) : null}
                <div className="mt-1.5 flex flex-wrap gap-1.5">
                  <Badge tone="info">~{ch.estimate?.rerender_seconds ?? 0}s rerender</Badge>
                  <Badge tone="warning">~${ch.estimate?.cost_usd ?? 0}</Badge>
                  {ch.scope && <Badge tone="muted">scope: {ch.scope}</Badge>}
                </div>
                {(ch.reasons ?? []).length > 0 && (
                  <div className="mt-1.5 text-[12px]" style={{ color: "var(--danger)" }}>
                    {(ch.reasons ?? []).map((r, ri) => <div key={ri}>⛔ {r}</div>)}
                  </div>
                )}
              </div>
            ))}
          </div>
          {preview.manifest_hash && (
            <div className="mt-2 font-mono text-[10.5px] truncate" style={{ color: "var(--text-faint)" }}>
              manifest {preview.manifest_hash.slice(0, 24)}… · preview {String(preview.preview_id ?? "").slice(0, 8)}
            </div>
          )}
        </div>
      )}

      {/* Audit ledger */}
      {showAudit && (
        <div className="mt-3 pt-3" style={{ borderTop: "var(--seam)" }}>
          <div className="panel-label mb-1.5">Command audit (GET /creative/commands)</div>
          {!audit.length && <div className="text-[12.5px]" style={{ color: "var(--text-faint)" }}>No commands recorded for this timeline yet.</div>}
          <div className="space-y-1">
            {audit.map((row) => (
              <div key={row.id} className="flex items-center gap-2 flex-wrap py-1.5 text-[12px]" style={{ borderBottom: "var(--seam)" }}>
                <Badge tone={statusTone(row.status)}>{row.status}</Badge>
                <span className="font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>
                  {String(row.created_at ?? "").replace("T", " ").slice(0, 19)} · v{row.parent_version ?? "?"}
                </span>
                <span className="truncate flex-1" style={{ color: "var(--text-muted)" }}>
                  {row.text_input || (row.commands ?? []).map((c) => c.type).join(", ")}
                </span>
                <span className="font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>{row.id.slice(0, 8)}</span>
              </div>
            ))}
          </div>
          <button className="btn-ghost !text-xs mt-2" onClick={loadAudit}>Refresh audit</button>
        </div>
      )}
    </Card>
  );
}
