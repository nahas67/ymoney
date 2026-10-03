/* Work 12 FE lane -- Audio Intelligence panel (contracts §15).
 *
 * Five actions, each one a real endpoint:
 *   Enhance Voice   POST /media-intel/audio/enhance  stages [dereverb, voice_isolation]
 *   Remove Noise    POST /media-intel/audio/enhance  stages [denoise]
 *   Normalize       POST /media-intel/audio/enhance  stages [loudness_normalization]
 *   Detect Silence  POST /media-intel/audio/silence
 *   Detect Fillers  POST /media-intel/audio/fillers
 *
 * The two that touch the TIMELINE (silence, fillers) only ever produce
 * proposals here; the cut plan is previewed in ProposalPreview and applied
 * through the QC gate. Enhancement is derived-only: it writes a NEW asset and
 * the source is never touched, so it needs no preview -- but its provenance,
 * cache hit and stage-by-stage honesty are shown all the same.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge, Card, Loading, toast } from "../ui";
import { useFetch } from "../../hooks/hooks";
import { wsApi } from "../../lib/api";
import ProposalPreview, { type AppliedPlan } from "./ProposalPreview";
import {
  ENHANCE_STAGES,
  capabilityAvailable,
  detectFillers,
  detectSilence,
  enhanceAudio,
  listProposals,
  listProviders,
  parseIntelError,
  unavailableReasons,
  type DetectionResponse,
  type EnhanceResponse,
  type EnhanceStageRow,
  type IntelError,
  type Proposal,
  type ProviderListing,
} from "./intelApi";
import { IntelEmpty, IntelErrorStrip, Provenance, ReadOnlyNote, Row, SubHead, UnavailableNote, pct, shortId } from "./shared";

type MediaAssetRow = { id: string; type: string; storage_key: string; duration_seconds?: number };

const STAGE_TONE: Record<string, string> = {
  applied: "success",
  skipped: "muted",
  unavailable: "warning",
  failed: "error",
};

/** The five buttons -> the stages each one submits. */
const ACTIONS: { key: string; label: string; kind: string; stages: string[]; hint: string }[] = [
  {
    key: "enhance_voice",
    label: "Enhance Voice",
    kind: "enhancement",
    stages: ["dereverb", "voice_isolation"],
    hint: "dereverb + voice isolation",
  },
  { key: "denoise", label: "Remove Noise", kind: "denoise", stages: ["denoise"], hint: "afftdn / arnndn" },
  {
    key: "normalize",
    label: "Normalize",
    kind: "enhancement",
    stages: ["loudness_normalization"],
    hint: "EBU R128 loudnorm",
  },
];

type Props = {
  timelineId: string;
  baseVersion: number;
  /** the selected clip's source asset, when there is one */
  assetId: string | null;
  readOnly: boolean;
  onApplied: (result: AppliedPlan, label: string) => void;
  onConflict: (detail: unknown) => void;
};

export default function AudioIntelligencePanel({ timelineId, baseVersion, assetId, readOnly, onApplied, onConflict }: Props) {
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<string | null>(assetId);
  const [extraStages, setExtraStages] = useState<string[]>([]);
  const [force, setForce] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState<IntelError | null>(null);
  const [enhance, setEnhance] = useState<EnhanceResponse | null>(null);
  const [detection, setDetection] = useState<DetectionResponse | null>(null);
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [policyId, setPolicyId] = useState<string | undefined>(undefined);

  /* The selected clip drives the default asset, but the operator can point the
   * panel at any media asset in the workspace. */
  useEffect(() => {
    setSelected(assetId);
  }, [assetId]);

  const providers = useFetch<ProviderListing>(() => listProviders(), [timelineId]);
  const assets = useFetch<{ items?: MediaAssetRow[] }>(
    () => wsApi.get("/assets/media?limit=200") as Promise<{ items?: MediaAssetRow[] }>,
    [timelineId]
  );

  const current = assetId ?? selected;
  const currentAsset = useMemo(
    () => (assets.data?.items ?? []).find((a) => a.id === current) ?? null,
    [assets.data, current]
  );

  const reloadProposals = useCallback(async () => {
    if (!current) {
      setProposals([]);
      return;
    }
    try {
      const listing = await listProposals({ asset_id: current });
      setProposals(listing.items ?? []);
    } catch (e) {
      setError(parseIntelError(e));
    }
  }, [current]);

  useEffect(() => {
    if (open) void reloadProposals();
  }, [open, reloadProposals]);

  function guard(need?: string): boolean {
    if (readOnly) {
      toast("Your workspace role is viewer — this action needs an editor", "warning");
      return false;
    }
    if (!current) {
      toast("Pick a media asset first", "warning", "No asset");
      return false;
    }
    if (need) {
      const dark = unavailableReasons(providers.data, need);
      if (dark) {
        toast(`${need} is unavailable: ${dark}`, "warning", "Unavailable");
        return false;
      }
    }
    return true;
  }

  async function runEnhance(key: string, stages: string[]) {
    const kind = ACTIONS.find((a) => a.key === key)?.kind ?? "enhancement";
    if (!guard(kind)) return;
    const all = [...new Set([...stages, ...extraStages])];
    setBusy(key);
    setError(null);
    try {
      const res = await enhanceAudio(current as string, all, { force });
      setEnhance(res);
      setDetection(null);
      if (res.cache_hit) toast("cache hit — no work re-run, no cost recorded", "info", "Enhancement");
      else toast(`stage matrix: ${res.stages.length} row(s)`, "success", "Enhancement");
    } catch (e) {
      setError(parseIntelError(e));
    } finally {
      setBusy("");
    }
  }

  async function detect(kind: "silence" | "fillers") {
    // Silence needs the real speech-activity provider. Fillers do NOT: with no
    // word alignment the backend degrades to caption cues and reports
    // `text_source.available=false` + its reason, so the action stays enabled
    // and the result says what it was measured against.
    if (!guard(kind === "silence" ? "speech_activity" : undefined)) return;
    setBusy(kind);
    setError(null);
    try {
      const res = kind === "silence" ? await detectSilence(current as string, { timelineId, force }) : await detectFillers(current as string, { timelineId, force });
      setDetection(res);
      setPolicyId(res.policy_id);
      setProposals(res.proposals ?? []);
      if (res.cache_hit) {
        toast("cache hit — proposals reused from a prior run", "info", "Detection");
      } else {
        toast(`${res.proposals?.length ?? 0} proposal(s) — nothing applied yet`, "success", "Detection");
      }
    } catch (e) {
      setError(parseIntelError(e));
    } finally {
      setBusy("");
    }
  }

  const stageRows: EnhanceStageRow[] = enhance?.stages ?? [];
  const manifest = enhance?.manifest ?? {};
  const license = (manifest.license ?? null) as { commercial_use?: string; code_license?: string; model_license?: string } | null;

  return (
    <Card>
      <button
        className="w-full flex items-center justify-between gap-2 text-left"
        onClick={() => setOpen((v) => !v)}
      >
        <b className="text-[13px]">🎚 Audio Intelligence</b>
        <span className="text-[11.5px]" style={{ color: "var(--text-faint)" }}>
          {open ? "▾" : "▸"}
        </span>
      </button>

      {open && (
        <div className="mt-2 space-y-2">
          {providers.loading && <Loading rows={1} />}
          {providers.error && <IntelErrorStrip error={providers.error} onRetry={providers.reload} />}
          {readOnly && <ReadOnlyNote what="Enhancement, detection, deciding and applying" />}

          {/* ---- asset + stage selection ---- */}
          <div className="space-y-1.5">
            <select
              className="select !text-[12px]"
              value={current ?? ""}
              onChange={(e) => setSelected(e.target.value || null)}
            >
              <option value="">— pick a media asset —</option>
              {(assets.data?.items ?? []).map((a) => (
                <option key={a.id} value={a.id}>
                  {a.type} · {(a.storage_key ?? "").split("/").pop()}
                </option>
              ))}
            </select>
            {currentAsset && (
              <div className="text-[11px] font-mono" style={{ color: "var(--text-faint)" }}>
                {currentAsset.id.slice(0, 8)} · {currentAsset.duration_seconds?.toFixed?.(1) ?? "?"}s
              </div>
            )}
          </div>

          {!readOnly && (
            <details className="text-[11.5px]">
              <summary style={{ color: "var(--text-muted)" }}>extra stages</summary>
              <div className="flex flex-wrap gap-2 mt-1">
                {ENHANCE_STAGES.map((s) => (
                  <label key={s} className="flex items-center gap-1">
                    <input
                      type="checkbox"
                      checked={extraStages.includes(s)}
                      onChange={() =>
                        setExtraStages((prev) => (prev.includes(s) ? prev.filter((x) => x !== s) : [...prev, s]))
                      }
                    />
                    <span className="font-mono">{s}</span>
                  </label>
                ))}
              </div>
              <label className="flex items-center gap-1 mt-1">
                <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} />
                <span>force (bypass the run cache)</span>
              </label>
            </details>
          )}

          {error && <IntelErrorStrip error={error.message} />}

          {/* ---- the three derived-asset actions ---- */}
          <div className="grid grid-cols-3 gap-1">
            {ACTIONS.map((a) => {
              const dark = unavailableReasons(providers.data, a.kind);
              return (
                <button
                  key={a.key}
                  className="btn-outline !text-[11px]"
                  disabled={readOnly || busy === a.key || !current || Boolean(dark)}
                  title={dark || a.hint}
                  onClick={() => void runEnhance(a.key, a.stages)}
                >
                  {busy === a.key ? "…" : a.label}
                </button>
              );
            })}
          </div>
          {/* one note per dark capability, not one per button (Enhance Voice
              and Normalize share the enhancement chain) */}
          {[...new Set(ACTIONS.map((a) => a.kind))]
            .filter((kind) => unavailableReasons(providers.data, kind))
            .map((kind) => (
              <UnavailableNote key={kind} reason={unavailableReasons(providers.data, kind)} />
            ))}

          {/* ---- the two proposal producers ---- */}
          <div className="grid grid-cols-2 gap-1">
            <button
              className="btn-outline !text-[11px]"
              disabled={readOnly || busy === "silence" || !current || !capabilityAvailable(providers.data, "speech_activity")}
              title={unavailableReasons(providers.data, "speech_activity") || "ffmpeg silencedetect"}
              onClick={() => void detect("silence")}
            >
              {busy === "silence" ? "…" : "Detect Silence"}
            </button>
            <button
              className="btn-outline !text-[11px]"
              disabled={readOnly || busy === "fillers" || !current}
              title={
                unavailableReasons(providers.data, "alignment") ||
                "word-level fillers when an alignment exists, else caption cues"
              }
              onClick={() => void detect("fillers")}
            >
              {busy === "fillers" ? "…" : "Detect Fillers"}
            </button>
          </div>
          {unavailableReasons(providers.data, "speech_activity") && (
            <UnavailableNote reason={unavailableReasons(providers.data, "speech_activity")} />
          )}
          {unavailableReasons(providers.data, "alignment") && (
            <UnavailableNote reason={`no word alignment provider (${unavailableReasons(providers.data, "alignment")}) — filler detection will fall back to caption cues and say so in the result`} />
          )}

          {/* ---- enhancement result: provenance + the stage matrix ---- */}
          {enhance && (
            <div className="space-y-1.5">
              <Provenance
                run={enhance.run}
                cacheHit={enhance.cache_hit}
                provider={manifest.provider as string}
                modelVersion={manifest.model_version as string}
                license={license}
              />
              {enhance.run.output_asset_id && (
                <Row label="derived">
                  {shortId(enhance.run.output_asset_id, 10)} (source untouched)
                </Row>
              )}
              <div className="space-y-0.5">
                {stageRows.map((s) => (
                  <div key={s.stage} className="flex items-center gap-1.5 text-[11.5px]">
                    <Badge tone={STAGE_TONE[s.status] ?? "muted"}>{s.status}</Badge>
                    <span className="font-mono" style={{ color: "var(--text-muted)" }}>
                      {s.stage}
                    </span>
                    {s.reason && (
                      <span className="min-w-0 break-words" style={{ color: "var(--text-faint)" }}>
                        {s.reason}
                      </span>
                    )}
                  </div>
                ))}
              </div>
              {(manifest.stages_unavailable ?? []).length > 0 && (
                <UnavailableNote
                  reason={(manifest.stages_unavailable ?? [])
                    .map((u) => `${u.stage}: ${u.reason}`)
                    .join("; ")}
                />
              )}
            </div>
          )}

          {/* ---- detection result ---- */}
          {detection && (
            <div className="space-y-1.5">
              <Provenance
                run={detection.run}
                cacheHit={detection.cache_hit}
                provider={detection.run.provider_key}
                modelVersion={detection.run.model_version}
                license={null}
              />
              {detection.text_source && (
                <div className="text-[11.5px]" style={{ color: "var(--text-muted)" }}>
                  text source: {detection.text_source.source} ({detection.text_source.units} {detection.text_source.unit}s)
                  {!detection.text_source.available && detection.text_source.reason
                    ? ` — ${detection.text_source.reason}`
                    : ""}
                </div>
              )}
              {detection.removal_ratio && (
                <Row label="removal">
                  {pct(detection.removal_ratio.planned)} planned / {pct(detection.removal_ratio.cap)} cap
                  {detection.removal_ratio.triggered ? " · cap triggered" : ""}
                </Row>
              )}
              {detection.qc?.required && (
                <div className="text-[11.5px]" style={{ color: "var(--warn)" }}>
                  QC required: {detection.qc.reason}
                </div>
              )}
            </div>
          )}

          {/* ---- proposals: preview, decide, QC, apply ---- */}
          <SubHead
            right={
              <button className="btn-ghost !text-[11px]" onClick={() => void reloadProposals()}>
                ↻ proposals
              </button>
            }
          >
            Proposal preview
          </SubHead>
          {proposals.length === 0 ? (
            <IntelEmpty
              title="No proposals for this asset"
              hint="Detection proposes cuts here; the timeline is only touched after you decide, QC runs and you confirm the apply."
            />
          ) : (
            <ProposalPreview
              timelineId={timelineId}
              assetId={current as string}
              baseVersion={baseVersion}
              proposals={proposals}
              policyId={policyId}
              readOnly={readOnly}
              onApplied={onApplied}
              onConflict={onConflict}
              onChanged={() => void reloadProposals()}
            />
          )}
        </div>
      )}
    </Card>
  );
}
