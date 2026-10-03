/* Work 12 FE lane -- proposal preview + the apply gate.
 *
 * THE RULE THIS FILE EXISTS TO ENFORCE: nothing is ever cut before it is
 * shown. Detection proposes, the operator decides, QC runs, and only then can
 * a batch be applied. The write itself is `POST /media-intel/proposals/apply`,
 * which the backend submits through `api.v1.timelines.apply_timeline_operations`
 * -- the very function the editor's `POST /timelines/{id}/operations` calls, so
 * the Work 02 `base_version` gate and the scene resync are the same ones. The
 * panel never writes the document and never invents a second save route; it
 * hands the canonical response to the editor (`onApplied`), which adopts it and
 * pushes the returned operations onto the same undo stack `commitOps` uses.
 *
 * The 409s are NOT swallowed. `QCApplyBlocked.as_dict()` arrives stringified in
 * `ApiError.message` (lib/api.ts:81); parseIntelError() reads it back and the
 * server's own sentence is printed verbatim, with the failing check names and
 * whether an override is required.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge, ConfirmButton, toast } from "../ui";
import { wsApi } from "../../lib/api";
import {
  applyProposals,
  decideProposal,
  getQc,
  getTimeMap,
  parseIntelError,
  recordQcOverride,
  runQc,
  type IntelError,
  type Proposal,
  type ProposalDecision,
  type QcResult,
  type TimeMap,
} from "./intelApi";
import { IntelEmpty, IntelErrorStrip, QcVerdictBadge, Row, SubHead, pct, sec, shortId } from "./shared";

const KIND_TONE: Record<string, string> = {
  REMOVE_RANGE: "error",
  SHORTEN_RANGE: "warning",
  KEEP: "muted",
};

const DECISION_LABEL: Record<ProposalDecision, string> = {
  keep: "KEEP",
  remove: "REMOVE",
  shorten: "SHORTEN",
};

/** What the backend would actually apply (plan_apply: DECIDED + remove/shorten). */
function isApplicable(p: Proposal): boolean {
  return p.status === "DECIDED" && (p.decision === "remove" || p.decision === "shorten");
}

type Gate = {
  runIds: string[];
  /** contributing runs the server has never judged -> apply answers 409 */
  missing: string[];
  /** FAIL with no recorded override -> apply answers 409 */
  failing: string[];
  /** FAIL the operator has already overridden -> apply must carry override=true */
  overridden: string[];
  overrideFlag: boolean;
  ok: boolean;
};

export type AppliedPlan = {
  operations: Record<string, any>[];
  timeline: { id: string; version: number; [k: string]: unknown };
  applied: number;
  time_map: TimeMap;
  qc: { verdict?: string; allowed?: boolean; override_used?: boolean }[];
  skipped: string[];
  mapped_scenes: number;
};

type Props = {
  timelineId: string;
  assetId: string;
  /** the editor's live version -- the Work 02 base_version */
  baseVersion: number;
  proposals: Proposal[];
  policyId?: string;
  readOnly: boolean;
  /** the editor adopts the canonical result: doc, version, undo entry */
  onApplied: (result: AppliedPlan, label: string) => void;
  /** the editor shows its existing conflict notice for a stale base_version */
  onConflict: (detail: unknown) => void;
  onChanged: () => void;
};

export default function ProposalPreview({
  timelineId,
  assetId,
  baseVersion,
  proposals,
  policyId,
  readOnly,
  onApplied,
  onConflict,
  onChanged,
}: Props) {
  const [picked, setPicked] = useState<string[]>([]);
  const [qc, setQc] = useState<Record<string, QcResult>>({});
  const [qcBusy, setQcBusy] = useState("");
  const [overrideReasons, setOverrideReasons] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState("");
  const [error, setError] = useState<IntelError | null>(null);
  const [timeMap, setTimeMap] = useState<TimeMap | null>(null);
  const [mapNote, setMapNote] = useState("");
  const [probe, setProbe] = useState("");

  useEffect(() => {
    setPicked(proposals.filter(isApplicable).map((p) => p.id));
  }, [proposals]);

  const loadMap = useCallback(async () => {
    setMapNote("");
    try {
      setTimeMap(await getTimeMap(assetId, policyId));
    } catch (e) {
      const parsed = parseIntelError(e);
      // 404 = nothing applied yet, which is a state, not a failure.
      setTimeMap(null);
      setMapNote(parsed.status === 404 ? parsed.message : `time map unavailable — ${parsed.message}`);
    }
  }, [assetId, policyId]);

  useEffect(() => {
    void loadMap();
  }, [loadMap]);

  const selected = useMemo(() => proposals.filter((p) => picked.includes(p.id)), [proposals, picked]);
  const runIds = useMemo(
    () => [...new Set(selected.map((p) => p.run_id).filter(Boolean))].sort(),
    [selected]
  );
  const runKey = runIds.join(",");

  /* Read the verdict the SERVER stored for each contributing run, so the badge
   * is never this session's guess. A run with no result row simply has none --
   * which is exactly the state the apply gate refuses. */
  useEffect(() => {
    let cancelled = false;
    if (!runKey) return;
    for (const runId of runKey.split(",")) {
      getQc(runId)
        .then((listing) => {
          if (cancelled) return;
          const latest = Object.values(listing.latest ?? {})[0];
          if (latest) setQc((prev) => ({ ...prev, [runId]: latest }));
        })
        .catch(() => {
          /* unreviewed / unreachable: the gate below says so explicitly */
        });
    }
    return () => {
      cancelled = true;
    };
  }, [runKey]);

  const gate: Gate = useMemo(() => {
    const missing = runIds.filter((id) => !qc[id]);
    const failing = runIds.filter((id) => qc[id]?.verdict === "FAIL" && !qc[id]?.override);
    const overridden = runIds.filter((id) => qc[id]?.verdict === "FAIL" && Boolean(qc[id]?.override));
    return {
      runIds,
      missing,
      failing,
      overridden,
      overrideFlag: failing.length + overridden.length > 0,
      ok: runIds.length > 0 && !missing.length && !failing.length,
    };
  }, [runIds, qc]);

  async function runQcFor(runId: string) {
    setQcBusy(runId);
    setError(null);
    try {
      const result = await runQc(runId, { kind: "audio" });
      setQc((prev) => ({ ...prev, [runId]: result }));
      toast(
        result.verdict === "FAIL"
          ? `QC FAIL — ${result.failures.join(", ") || "hard violation"}`
          : `QC ${result.verdict}`,
        result.verdict === "FAIL" ? "error" : "success",
        "Quality check"
      );
    } catch (e) {
      setError(parseIntelError(e));
    } finally {
      setQcBusy("");
    }
  }

  async function recordOverride(runId: string) {
    const reason = (overrideReasons[runId] ?? "").trim();
    if (!reason) {
      setError({
        status: 422,
        message: "an override must state why it was made",
        kind: "plain",
        qc: null,
        conflict: null,
      });
      return;
    }
    setBusy(`override:${runId}`);
    setError(null);
    try {
      // The acting user IS the author server-side (get_current_user); `reason`
      // is the other half of "who + why" and the server requires both.
      const result = await recordQcOverride(runId, { reason, kind: qc[runId]?.kind ?? "audio" });
      setQc((prev) => ({ ...prev, [runId]: result }));
      setOverrideReasons((prev) => ({ ...prev, [runId]: "" }));
      toast("Override recorded — you are the attributed author", "warning", "QC override");
    } catch (e) {
      setError(parseIntelError(e));
    } finally {
      setBusy("");
    }
  }

  async function decide(p: Proposal, decision: ProposalDecision) {
    setBusy(`decide:${p.id}`);
    setError(null);
    try {
      await decideProposal(p.id, decision);
      onChanged();
      toast(`proposal ${shortId(p.id, 6)} → ${DECISION_LABEL[decision]}`, "success", "Decision recorded");
    } catch (e) {
      setError(parseIntelError(e));
    } finally {
      setBusy("");
    }
  }

  const runApply = useCallback(async () => {
    setBusy("apply");
    setError(null);
    try {
      const result = await applyProposals({
        timeline_id: timelineId,
        base_version: baseVersion,
        proposal_ids: picked,
        asset_id: assetId,
        override: gate.overrideFlag,
      });
      onApplied(result as unknown as AppliedPlan, `Apply ${result.applied} intel edit(s)`);
      onChanged();
      await loadMap();
      toast(
        `applied ${result.applied} operation(s) through the canonical operations route`,
        "success",
        "Edits applied"
      );
    } catch (e) {
      const parsed = parseIntelError(e);
      setError(parsed);
      if (parsed.kind === "version_conflict") {
        // A stale base_version is the WORK 02 conflict, not a QC problem: hand
        // it to the editor so its existing notice + reload path runs.
        onConflict(parsed.conflict);
      }
    } finally {
      setBusy("");
    }
  }, [assetId, baseVersion, gate.overrideFlag, loadMap, onApplied, onChanged, onConflict, picked, timelineId]);

  /* Map one source instant through the canonical mapping function so the
   * operator can see where a caption/scene time lands after the cut. */
  async function mapInstant() {
    const t = Number(probe);
    if (!Number.isFinite(t) || t < 0) return;
    const q = new URLSearchParams({ asset_id: assetId, source_time: String(t) });
    if (policyId) q.set("policy_id", policyId);
    try {
      setTimeMap((await wsApi.get(`/media-intel/time-map?${q.toString()}`)) as TimeMap);
      setMapNote("");
    } catch (e) {
      setMapNote(parseIntelError(e).message);
    }
  }

  if (!proposals.length) {
    return (
      <IntelEmpty
        title="No proposals yet"
        hint="Run Detect Silence or Detect Fillers — every cut is proposed here first, and nothing is applied until you decide."
      />
    );
  }

  return (
    <div className="space-y-2">
      {error && (
        <IntelErrorStrip
          error={error.message}
          onRetry={
            error.status === 409
              ? () => {
                  /* a QC-blocked 409 is cleared by judging, not by retrying */
                  const first = error.qc?.run_id ?? gate.runIds[0];
                  if (first) void runQcFor(first);
                }
              : undefined
          }
          retryLabel="Run QC"
        >
          {error.qc?.failures?.length ? (
            <div className="mt-1" style={{ color: "var(--danger)" }}>
              failed checks: {error.qc.failures.join(", ")}
            </div>
          ) : null}
        </IntelErrorStrip>
      )}

      {/* ---- the proposals ---- */}
      <div className="max-h-[240px] overflow-auto space-y-1.5 pr-1">
        {proposals.map((p) => {
          const on = picked.includes(p.id);
          return (
            <div
              key={p.id}
              className="rounded-lg px-2 py-1.5 text-[11.5px]"
              style={{ border: "var(--seam)", background: "var(--bg-inset)", opacity: on ? 1 : 0.72 }}
            >
              <label className="flex items-start gap-1.5 cursor-pointer">
                <input
                  type="checkbox"
                  className="mt-0.5"
                  checked={on}
                  onChange={() => setPicked((prev) => (on ? prev.filter((x) => x !== p.id) : [...prev, p.id]))}
                />
                <span className="min-w-0 flex-1">
                  <span className="flex items-center gap-1 flex-wrap">
                    <Badge tone={KIND_TONE[p.kind] ?? "muted"}>{p.kind}</Badge>
                    <span className="font-mono" style={{ color: "var(--text-muted)" }}>
                      {sec(p.start_s)}–{sec(p.end_s)}
                    </span>
                    <span style={{ color: "var(--text-faint)" }}>{p.duration_s.toFixed(2)}s</span>
                    {p.confidence != null && (
                      <span style={{ color: "var(--text-faint)" }}>conf {pct(p.confidence)}</span>
                    )}
                  </span>
                  <span className="block mt-0.5" style={{ color: "var(--text-muted)" }}>
                    {p.reason || "no reason recorded"}
                  </span>
                  <span className="block mt-0.5 font-mono" style={{ color: "var(--text-faint)" }}>
                    {p.status}
                    {p.decision ? ` · ${DECISION_LABEL[p.decision]}` : " · undecided"}
                    {p.decided_by ? ` · by ${shortId(p.decided_by, 6)}` : ""}
                  </span>
                </span>
              </label>
              {!readOnly && (
                <div className="flex gap-1 mt-1.5 pl-5">
                  {(["keep", "shorten", "remove"] as ProposalDecision[]).map((d) => (
                    <button
                      key={d}
                      className="btn-outline !text-[11px] !py-0.5"
                      disabled={busy === `decide:${p.id}` || p.status === "APPLIED"}
                      onClick={() => void decide(p, d)}
                    >
                      {DECISION_LABEL[d]}
                    </button>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* ---- source <-> edited mapping ---- */}
      <SubHead
        right={
          <button className="btn-ghost !text-[11px]" onClick={() => void loadMap()}>
            ↻ mapping
          </button>
        }
      >
        Source ↔ edited mapping
      </SubHead>
      {timeMap ? (
        <div className="text-[11.5px] space-y-0.5">
          <Row label="source">{sec(timeMap.source_duration_s)}</Row>
          <Row label="edited">{sec(timeMap.output_duration_s)}</Row>
          <Row label="removed">
            {sec(timeMap.removed_duration_s)} ({pct(timeMap.removal_ratio)})
          </Row>
          <Row label="cuts">{timeMap.removals.length}</Row>
          {timeMap.mapped?.time && (
            <Row label="probe">
              source {sec(timeMap.mapped.time.source_s)} → edited {sec(timeMap.mapped.time.edited_s)}
            </Row>
          )}
          <div className="flex items-center gap-1 mt-1">
            <input
              className="input !py-0.5 !text-[11.5px]"
              placeholder="source seconds"
              value={probe}
              onChange={(e) => setProbe(e.target.value)}
            />
            <button className="btn-outline !text-[11px] !py-0.5" onClick={() => void mapInstant()}>
              map
            </button>
          </div>
        </div>
      ) : (
        <IntelEmpty
          title={mapNote || "No applied plan yet"}
          hint="The mapping appears once an apply has saved a cut plan; it is produced by the same map the captions and scenes are re-timed with."
        />
      )}

      {/* ---- QC gate: detect -> decide -> QC -> apply ---- */}
      <SubHead>QC gate</SubHead>
      {gate.runIds.length === 0 ? (
        <div className="text-[11.5px]" style={{ color: "var(--text-faint)" }}>
          Select the proposals to apply — every contributing run is QC'd separately.
        </div>
      ) : (
        <div className="space-y-1.5">
          {gate.runIds.map((runId) => {
            const result = qc[runId];
            return (
              <div key={runId} className="rounded-lg px-2 py-1.5 text-[11.5px]" style={{ border: "var(--seam)" }}>
                <div className="flex items-center justify-between gap-2">
                  <span className="font-mono" style={{ color: "var(--text-faint)" }}>
                    run {shortId(runId)}
                  </span>
                  {result ? (
                    <QcVerdictBadge qc={result} />
                  ) : (
                    <Badge tone="warning">no verdict — unreviewed</Badge>
                  )}
                </div>
                {result?.verdict === "FAIL" && (
                  <div className="mt-1">
                    <div style={{ color: "var(--danger)" }}>
                      failed checks: {result.failures.join(", ") || "hard violation"}
                    </div>
                    {result.override ? (
                      <div className="mt-0.5" style={{ color: "var(--text-muted)" }}>
                        overridden{result.override.reason ? `: ${result.override.reason}` : ""}
                      </div>
                    ) : readOnly ? (
                      <div className="mt-0.5" style={{ color: "var(--text-muted)" }}>
                        An editor must record an attributable override before this plan can be applied.
                      </div>
                    ) : (
                      <div className="mt-1 space-y-1">
                        <input
                          className="input !py-0.5 !text-[11.5px]"
                          placeholder="why are you overriding this verdict?"
                          value={overrideReasons[runId] ?? ""}
                          onChange={(e) => setOverrideReasons((prev) => ({ ...prev, [runId]: e.target.value }))}
                        />
                        <button
                          className="btn-danger !text-[11px] !py-0.5"
                          disabled={busy === `override:${runId}`}
                          onClick={() => void recordOverride(runId)}
                        >
                          Record override (who + why)
                        </button>
                      </div>
                    )}
                  </div>
                )}
                {!readOnly && (
                  <button
                    className="btn-outline !text-[11px] !py-0.5 mt-1"
                    disabled={qcBusy === runId}
                    onClick={() => void runQcFor(runId)}
                  >
                    {qcBusy === runId ? "Running QC…" : result ? "Re-run QC" : "Run QC"}
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* ---- apply ---- */}
      {readOnly ? (
        <div className="text-[11.5px] mt-2" style={{ color: "var(--text-muted)" }}>
          Applying an edit plan needs an editor.
        </div>
      ) : (
        <div className="mt-2 space-y-1">
          <ConfirmButton
            className="btn-primary !text-[11.5px]"
            confirmText={`Apply ${picked.length} edit(s) to timeline v${baseVersion}?`}
            disabled={!gate.ok || busy === "apply" || picked.length === 0}
            onConfirm={() => void runApply()}
          >
            Apply {picked.length} decided edit(s)
          </ConfirmButton>
          {!gate.ok && (
            <div className="text-[11px]" style={{ color: "var(--warn)" }}>
              {gate.missing.length
                ? `Run QC first — ${gate.missing.length} contributing run(s) have no verdict, and the server answers 409 without one.`
                : gate.failing.length
                  ? `Record an attributable override for ${gate.failing.length} FAIL run(s) before applying.`
                  : "Select at least one decided edit to apply."}
            </div>
          )}
          {gate.overrideFlag && gate.ok && (
            <div className="text-[11px]" style={{ color: "var(--warn)" }}>
              This batch carries a FAIL verdict, so the apply is sent with <code>override=true</code>.
            </div>
          )}
        </div>
      )}
    </div>
  );
}
