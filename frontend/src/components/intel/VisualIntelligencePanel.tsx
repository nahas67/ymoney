/* Work 12 FE lane -- Visual Intelligence panel (contracts §15).
 *
 * Five actions, each one a real endpoint:
 *   Track Subject     POST /media-intel/face-tracks
 *   Auto Reframe      POST /media-intel/reframe            (editable keyframes)
 *   Speaker Layout    POST /media-intel/reframe            (layout vocabulary)
 *   Background Blur   POST /media-intel/background         operation=background_blur
 *   Background Replace POST /media-intel/background        operation=background_replace
 *
 * Speaker Layout also maps voices to faces first (POST /media-intel/active-speaker)
 * when the operator asks for it, because an ACTIVE_SPEAKER / PODCAST_DYNAMIC
 * crop follows the resolved speaker. UNRESOLVED rows are rendered as
 * "unresolved (reason)" and never as a chosen speaker.
 *
 * Nothing here writes the timeline document: a reframe is stored as keyframes
 * and a background op as a NEW derived asset. No sensitive attribute is ever
 * displayed -- speakers are anonymous SPEAKER_xx ids or an explicit reason.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge, Card, Loading, toast } from "../ui";
import { useFetch } from "../../hooks/hooks";
import { wsApi } from "../../lib/api";
import KeyframeEditor from "./KeyframeEditor";
import {
  REFRAME_LAYOUTS,
  TARGET_ASPECTS,
  capabilityAvailable,
  getActiveSpeaker,
  getFaceTracks,
  getReframe,
  listProviders,
  mapActiveSpeaker,
  parseIntelError,
  planReframe,
  runBackground,
  trackFaces,
  unavailableReasons,
  type ActiveSpeakerRow,
  type BackgroundResult,
  type FaceTrack,
  type IntelError,
  type IntelRun,
  type ProviderListing,
  type ReframePlan,
} from "./intelApi";
import { IntelEmpty, IntelErrorStrip, Provenance, ReadOnlyNote, Row, SubHead, UnavailableNote, pct, sec, shortId } from "./shared";

type MediaAssetRow = { id: string; type: string; storage_key: string; duration_seconds?: number };

type Props = {
  assetId: string | null;
  readOnly: boolean;
};

export default function VisualIntelligencePanel({ assetId, readOnly }: Props) {
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<string | null>(assetId);
  const [layout, setLayout] = useState<string>("ACTIVE_SPEAKER");
  const [aspect, setAspect] = useState<string>("9:16");
  const [participants, setParticipants] = useState("2");
  const [gridRows, setGridRows] = useState("2");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState<IntelError | null>(null);
  const [tracks, setTracks] = useState<FaceTrack[]>([]);
  const [plan, setPlan] = useState<ReframePlan | null>(null);
  const [speakers, setSpeakers] = useState<ActiveSpeakerRow[]>([]);
  const [background, setBackground] = useState<BackgroundResult | null>(null);
  const [speakerRun, setSpeakerRun] = useState<IntelRun | null>(null);
  const [faceRun, setFaceRun] = useState<IntelRun | null>(null);

  useEffect(() => {
    setSelected(assetId);
  }, [assetId]);

  const providers = useFetch<ProviderListing>(() => listProviders(), [selected]);
  const assets = useFetch<{ items?: MediaAssetRow[] }>(
    () => wsApi.get("/assets/media?limit=200") as Promise<{ items?: MediaAssetRow[] }>,
    [selected]
  );

  const current = assetId ?? selected;
  const reframeDark = unavailableReasons(providers.data, "reframe");
  const faceDark = unavailableReasons(providers.data, "face_tracking");
  const segmentDark = unavailableReasons(providers.data, "segmentation");

  const needsSpeaker = layout === "ACTIVE_SPEAKER" || layout === "PODCAST_DYNAMIC";

  function guard(need: string): boolean {
    if (readOnly) {
      toast("Your workspace role is viewer — this action needs an editor", "warning");
      return false;
    }
    if (!current) {
      toast("Pick a media asset first", "warning", "No asset");
      return false;
    }
    const dark = unavailableReasons(providers.data, need);
    if (dark) {
      toast(`${need} is unavailable: ${dark}`, "warning", "Unavailable");
      return false;
    }
    return true;
  }

  const mapSpeakers = useCallback(async () => {
    if (!current) return;
    setBusy("speakers");
    setError(null);
    try {
      const res = await mapActiveSpeaker(current as string, {
        face_run_id: faceRun?.id,
      });
      setSpeakers(res.items ?? []);
      setSpeakerRun(res.run);
      const unresolved = (res.items ?? []).filter((i) => i.status === "UNRESOLVED");
      toast(
        `${(res.items ?? []).length - unresolved.length} resolved, ${unresolved.length} unresolved`,
        unresolved.length ? "warning" : "success",
        "Active speaker"
      );
    } catch (e) {
      setError(parseIntelError(e));
    } finally {
      setBusy("");
    }
  }, [current, faceRun?.id]);

  async function runTrackSubject() {
    if (!guard("face_tracking")) return;
    setBusy("faces");
    setError(null);
    try {
      const res = await trackFaces(current as string);
      setFaceRun(res.run);
      if (res.run.status === "UNAVAILABLE") {
        setTracks([]);
        toast(res.run.warnings.join("; ") || "face tracking is unavailable", "warning", "Unavailable");
        return;
      }
      if (res.cache_hit) {
        const full = await getFaceTracks(res.run.id);
        setTracks(full.items ?? []);
        toast("cache hit — prior tracks reused", "info", "Track subject");
      } else {
        setTracks(res.items ?? []);
        toast(`${(res.items ?? []).length} face track(s)`, "success", "Track subject");
      }
    } catch (e) {
      setError(parseIntelError(e));
    } finally {
      setBusy("");
    }
  }

  async function runReframe(kind: "auto" | "layout") {
    if (!guard("reframe")) return;
    setBusy(kind);
    setError(null);
    try {
      if (kind === "layout" && needsSpeaker && speakers.length === 0) {
        // The layout follows the voice; map first so the crop path is grounded
        // in something measured rather than guessed.
        await mapSpeakers();
      }
      const res = await planReframe({
        asset_id: current as string,
        aspect,
        layout,
        evidence_run_id: speakerRun?.id ?? null,
        participants: Number(participants) || null,
        grid_rows: layout === "GRID" ? Number(gridRows) || null : null,
      });
      setPlan(res);
      toast(
        `${res.layout} → ${res.aspect}: ${res.keyframe_count} editable keyframe(s), not baked`,
        "success",
        "Reframe"
      );
    } catch (e) {
      setError(parseIntelError(e));
    } finally {
      setBusy("");
    }
  }

  async function runBg(op: "background_blur" | "background_replace") {
    if (!guard("segmentation")) return;
    setBusy(op);
    setError(null);
    try {
      const res = await runBackground({ asset_id: current as string, operation: op, aspect });
      setBackground(res);
      if (res.status === "UNAVAILABLE") {
        toast(res.reason || "background op unavailable", "warning", "Unavailable");
      } else {
        toast(`${op} → derived asset ${shortId(res.output_asset_id, 8)}`, "success", "Background");
      }
    } catch (e) {
      setError(parseIntelError(e));
    } finally {
      setBusy("");
    }
  }

  const resolvedSpeakers = useMemo(() => speakers.filter((s) => s.status === "RESOLVED"), [speakers]);
  const unresolvedSpeakers = useMemo(() => speakers.filter((s) => s.status === "UNRESOLVED"), [speakers]);

  return (
    <Card>
      <button className="w-full flex items-center justify-between gap-2 text-left" onClick={() => setOpen((v) => !v)}>
        <b className="text-[13px]">🎥 Visual Intelligence</b>
        <span className="text-[11.5px]" style={{ color: "var(--text-faint)" }}>
          {open ? "▾" : "▸"}
        </span>
      </button>

      {open && (
        <div className="mt-2 space-y-2">
          {providers.loading && <Loading rows={1} />}
          {providers.error && <IntelErrorStrip error={providers.error} onRetry={providers.reload} />}
          {readOnly && <ReadOnlyNote what="Tracking, reframing and background work" />}

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

          {error && <IntelErrorStrip error={error.message} />}

          {/* ---- track subject ---- */}
          <button
            className="btn-outline !text-[11.5px] w-full"
            disabled={readOnly || busy === "faces" || !current || Boolean(faceDark)}
            title={faceDark || "detect + track faces in a video"}
            onClick={() => void runTrackSubject()}
          >
            {busy === "faces" ? "Tracking…" : "Track Subject"}
          </button>
          {faceDark && <UnavailableNote reason={faceDark} />}
          {faceRun && (
            <>
              <Provenance
                run={faceRun}
                cacheHit={faceRun.cache_hit}
                provider={faceRun.provider_key}
                modelVersion={faceRun.model_version}
              />
              {/* a viewer (or a re-opened panel) reads the stored tracks without
                  re-running detection */}
              <button
                className="btn-ghost !text-[11px]"
                disabled={busy === "faceRead"}
                onClick={() => {
                  setBusy("faceRead");
                  getFaceTracks(faceRun.id)
                    .then((d) => setTracks(d.items ?? []))
                    .catch((e) => setError(parseIntelError(e)))
                    .finally(() => setBusy(""));
                }}
              >
                {busy === "faceRead" ? "loading…" : `view stored tracks (${faceRun.id.slice(0, 8)})`}
              </button>
            </>
          )}
          {tracks.length > 0 && (
            <div className="text-[11.5px] space-y-0.5">
              {tracks.map((t) => (
                <div key={t.id} className="flex items-center gap-1.5">
                  <Badge tone={t.unresolved_crossing ? "warning" : "muted"}>{t.track_id}</Badge>
                  <span className="font-mono" style={{ color: "var(--text-muted)" }}>
                    {sec(t.start_s)}–{sec(t.end_s)}
                  </span>
                  <span style={{ color: "var(--text-faint)" }}>
                    {t.sample_count} samples · conf {pct(t.confidence_max)}
                    {t.truncated ? " · truncated" : ""}
                    {t.unresolved_crossing ? " · unresolved crossing" : ""}
                  </span>
                </div>
              ))}
            </div>
          )}

          {/* ---- reframe + layout ---- */}
          <SubHead>Reframe</SubHead>
          <div className="grid grid-cols-2 gap-1">
            <select className="select !text-[11.5px]" value={layout} onChange={(e) => setLayout(e.target.value)}>
              {REFRAME_LAYOUTS.map((l) => (
                <option key={l} value={l}>
                  {l}
                </option>
              ))}
            </select>
            <select className="select !text-[11.5px]" value={aspect} onChange={(e) => setAspect(e.target.value)}>
              {TARGET_ASPECTS.map((a) => (
                <option key={a} value={a}>
                  {a}
                </option>
              ))}
            </select>
          </div>
          {layout === "GRID" && (
            <label className="block text-[11.5px]">
              grid rows
              <input
                className="input !py-0.5 !text-[11.5px] mt-0.5"
                type="number"
                min={1}
                value={gridRows}
                onChange={(e) => setGridRows(e.target.value)}
              />
            </label>
          )}
          {layout !== "ACTIVE_SPEAKER" && (
            <label className="block text-[11.5px]">
              participants
              <input
                className="input !py-0.5 !text-[11.5px] mt-0.5"
                type="number"
                min={1}
                value={participants}
                onChange={(e) => setParticipants(e.target.value)}
              />
            </label>
          )}
          <div className="grid grid-cols-2 gap-1">
            <button
              className="btn-outline !text-[11.5px]"
              disabled={readOnly || busy === "auto" || !current || Boolean(reframeDark)}
              title={reframeDark || "ACTIVE_SPEAKER crop, editable keyframes"}
              onClick={() => void runReframe("auto")}
            >
              {busy === "auto" ? "…" : "Auto Reframe"}
            </button>
            <button
              className="btn-outline !text-[11.5px]"
              disabled={readOnly || busy === "layout" || !current || Boolean(reframeDark)}
              title={reframeDark || "apply the selected multi-speaker layout"}
              onClick={() => void runReframe("layout")}
            >
              {busy === "layout" ? "…" : "Speaker Layout"}
            </button>
          </div>
          {reframeDark && <UnavailableNote reason={reframeDark} />}
          {!capabilityAvailable(providers.data, "reframe") && !reframeDark && (
            <IntelEmpty title="Reframe provider not selected" hint="The reframe chain has no available member." />
          )}

          {/* ---- active speaker (anonymous ids + explicit unresolved) ---- */}
          <SubHead
            right={
              <button
                className="btn-ghost !text-[11px]"
                disabled={readOnly || busy === "speakers" || !current}
                onClick={() => void mapSpeakers()}
              >
                {busy === "speakers" ? "mapping…" : "map voices → faces"}
              </button>
            }
          >
            Active speaker
          </SubHead>
          {speakerRun && (
            <>
              <Provenance
                run={speakerRun}
                cacheHit={speakerRun.cache_hit}
                provider={speakerRun.provider_key}
                modelVersion={speakerRun.model_version}
              />
              <button
                className="btn-ghost !text-[11px]"
                disabled={busy === "speakerRead"}
                onClick={() => {
                  setBusy("speakerRead");
                  getActiveSpeaker(speakerRun.id)
                    .then((d) => setSpeakers(d.items ?? []))
                    .catch((e) => setError(parseIntelError(e)))
                    .finally(() => setBusy(""));
                }}
              >
                {busy === "speakerRead" ? "loading…" : "view stored mapping"}
              </button>
            </>
          )}
          {speakers.length === 0 ? (
            <IntelEmpty
              title="No mapping yet"
              hint="Anonymous speaker ids are matched to face tracks by measured overlap. An ambiguous window is reported as unresolved, never guessed."
            />
          ) : (
            <div className="space-y-0.5 text-[11.5px]">
              {resolvedSpeakers.map((s) => (
                <div key={s.id} className="flex items-center gap-1.5">
                  <Badge tone="success">RESOLVED</Badge>
                  <span className="font-mono" style={{ color: "var(--text-muted)" }}>
                    {s.speaker_id ?? "—"}
                  </span>
                  <span style={{ color: "var(--text-faint)" }}>
                    {sec(s.start_s)}–{sec(s.end_s)} · conf {pct(s.confidence)}
                  </span>
                </div>
              ))}
              {unresolvedSpeakers.map((s) => (
                <div key={s.id} className="flex items-center gap-1.5 flex-wrap">
                  <Badge tone="warning">UNRESOLVED</Badge>
                  <span style={{ color: "var(--text-muted)" }}>
                    unresolved ({s.reason || "reason not recorded"})
                  </span>
                  <span className="font-mono" style={{ color: "var(--text-faint)" }}>
                    {sec(s.start_s)}–{sec(s.end_s)}
                    {s.speaker_id ? ` · ${s.speaker_id} (voice only)` : ""}
                  </span>
                </div>
              ))}
            </div>
          )}

          {/* ---- keyframes ---- */}
          <SubHead
            right={
              plan && (
                <button
                  className="btn-ghost !text-[11px]"
                  onClick={() => {
                    void getReframe(plan.id)
                      .then(setPlan)
                      .catch((e) => setError(parseIntelError(e)));
                  }}
                >
                  ↻ reload
                </button>
              )
            }
          >
            Keyframes {plan ? `(${plan.layout} · ${plan.aspect} · ${plan.keyframe_count})` : ""}
          </SubHead>
          {plan ? (
            <div className="space-y-1.5">
              <Row label="strategy">{plan.strategy}</Row>
              <Row label="editable">
                yes — the crop is stored as keyframes, nothing is baked
              </Row>
              {plan.jitter && Object.keys(plan.jitter).length > 0 && (
                <Row label="jitter">{JSON.stringify(plan.jitter)}</Row>
              )}
              <KeyframeEditor
                keyframes={plan.keyframes}
                readOnly={readOnly}
                onPatched={(kf) =>
                  setPlan((prev) =>
                    prev
                      ? {
                          ...prev,
                          keyframes: prev.keyframes.map((k) => (k.id === kf.id ? { ...k, ...kf } : k)),
                        }
                      : prev
                  )
                }
              />
            </div>
          ) : (
            <IntelEmpty
              title="No reframe plan yet"
              hint="Auto Reframe computes a 9:16 crop path you can edit keyframe by keyframe — the media is never re-encoded."
            />
          )}

          {/* ---- background ---- */}
          <SubHead>Background</SubHead>
          <div className="grid grid-cols-2 gap-1">
            <button
              className="btn-outline !text-[11.5px]"
              disabled={readOnly || busy === "background_blur" || !current || Boolean(segmentDark)}
              title={segmentDark || "blur everything outside the person mask"}
              onClick={() => void runBg("background_blur")}
            >
              {busy === "background_blur" ? "…" : "Background Blur"}
            </button>
            <button
              className="btn-outline !text-[11.5px]"
              disabled={readOnly || busy === "background_replace" || !current || Boolean(segmentDark)}
              title={segmentDark || "replace the background outside the person mask (needs a background asset id)"}
              onClick={() => void runBg("background_replace")}
            >
              {busy === "background_replace" ? "…" : "Background Replace"}
            </button>
          </div>
          {segmentDark && (
            <UnavailableNote
              reason={`${segmentDark} — no PERSON mask exists yet, so both background ops answer UNAVAILABLE rather than producing a bad mask`}
            />
          )}
          {background &&
            (background.status === "UNAVAILABLE" ? (
              <UnavailableNote reason={background.reason || "background op unavailable"}>
                {background.required && (
                  <div className="mt-1 font-mono" style={{ color: "var(--text-faint)" }}>
                    required: {JSON.stringify(background.required)}
                  </div>
                )}
              </UnavailableNote>
            ) : (
              <div className="text-[11.5px] space-y-0.5">
                <Row label="status">
                  <Badge tone="success">COMPLETED</Badge> {background.operation}
                </Row>
                <Row label="derived">{shortId(background.output_asset_id, 10)}</Row>
                <Row label="rendered">{background.rendered ? "yes" : "mask reference only"}</Row>
                {background.mask && (
                  <Row label="mask">
                    {background.mask.mask_kind} · area {pct(background.mask.mask_area_ratio as number)} ·{" "}
                    {background.mask.mask_provider}
                  </Row>
                )}
              </div>
            ))}
        </div>
      )}
    </Card>
  );
}
