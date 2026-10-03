/* Work 13 editor panel: caption style, word emphasis, motion templates,
   transitions and effects.
 *
 * Every control here emits a TYPED Work 13 timeline operation
 * (update_caption_style / set_caption_words / apply_effect / remove_effect /
 * set_transition) so the edit goes through the SAME operation layer, undo
 * stack and version history the editor already has. Nothing in this file
 * renders anything itself -- the preview reflects the stored style, and the
 * renderer consumes exactly those fields.

 * Nothing here can express a raw ffmpeg fragment: effects come from the
 * bounded registry with their own typed parameter forms, and an effect the
 * brand forbids is disabled rather than hidden. */

import { useEffect, useMemo, useState } from "react";
import { Badge } from "../ui";
import { IntelErrorStrip, Row, SubHead } from "../intel/shared";
import {
  listEffects,
  listPresets,
  listTemplates,
  listTransitions,
  emphasisVocabulary,
  runCaptionMotionQc,
  type EffectWire,
  type EmphasisVocabulary,
  type MotionTemplateWire,
  type QcReportWire,
  type TransitionWire,
} from "./motionApi";
import { wsApi } from "../../lib/api";

export type MotionOp = Record<string, unknown>;

type Props = {
  clip: any;
  track: string;
  /** Commits a batch of typed timeline operations (same contract as the rest
   *  of the Inspector), so undo/redo keeps working. */
  commit: (ops: MotionOp[], label: string) => void;
  /** Non-mutating CaptionMotionQC over a timeline. */
  timelineId: string;
  /** Media-intelligence evidence, so the UI can explain a blocked control. */
  assetId?: string;
  readOnly?: boolean;
};

const call = <T,>(path: string, body?: unknown) =>
  (wsApi.get as unknown as (p: string) => Promise<T>)(
    path
  ) as Promise<T>;

const callPost = <T,>(path: string, body?: unknown) =>
  (wsApi.post as unknown as (p: string, b: unknown) => Promise<T>)(path, body);

const TONE: Record<string, string> = {
  PASS: "success",
  PASS_WITH_WARNINGS: "warning",
  REVIEW_REQUIRED: "warning",
  FAIL: "danger",
};

export default function CaptionMotionPanel({
  clip,
  track,
  commit,
  timelineId,
  assetId,
  readOnly,
}: Props) {
  const [presets, setPresets] = useState<Record<string, any>>({});
  const [templates, setTemplates] = useState<MotionTemplateWire[]>([]);
  const [effects, setEffects] = useState<EffectWire[]>([]);
  const [transitions, setTransitions] = useState<TransitionWire[]>([]);
  const [vocab, setVocab] = useState<EmphasisVocabulary | null>(null);
  const [qc, setQc] = useState<QcReportWire | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");

  // A transition may be stored on EITHER clip of the pair; show the one that
  // is actually attached so the type/duration editor never edits a phantom.
  const storedTransition: any | null =
    (clip as any)?.transition ??
    (clip as any)?.transition_spec ??
    null;

  // The renderer can only composite when a Work 12 mask is attached. Derived
  // from what the renderer itself looks for, so the UI cannot claim a mask
  // exists when the render would find none.
  const hasMask = Boolean(
    (clip as any)?.subject_mask_asset_id ||
      (clip as any)?.subject_mask_key ||
      (clip as any)?.tracking_ok
  );

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const [p, t, e, tr, v] = await Promise.all([
          call<{ items: any[] }>("/captions/presets"),
          call<{ items: MotionTemplateWire[] }>("/captions/templates"),
          call<{ items: EffectWire[] }>("/captions/effects"),
          call<{ items: TransitionWire[] }>("/captions/transitions"),
          call<EmphasisVocabulary>("/captions/emphasis"),
        ]);
        if (!live) return;
        setPresets(Object.fromEntries(p.items.map((i) => [i.key, i])));
        setTemplates(t.items);
        setEffects(e.items);
        setTransitions(tr.items);
        setVocab(v);
      } catch (exc) {
        if (live) setError(String(exc));
      }
    })();
    return () => {
      live = false;
    };
  }, []);

  const text = clip?.text ?? {};
  const presetKey: string = text.preset ?? "minimal";
  const preset = presets[presetKey];
  const animation = text.animation ?? {};
  const wordLevel = Boolean(clip?.word_level) && Array.isArray(clip?.words);
  const disabled = Boolean(readOnly);

  const styleOp = (patch: Record<string, unknown>, label: string) => {
    if (disabled) return;
    commit(
      [
        {
          type: "update_caption_style",
          track,
          clip_id: clip.id,
          ...(presetKey ? { preset: presetKey } : {}),
          style: patch,
        },
      ],
      label
    );
  };

  const effectOp = (effect: Record<string, unknown>, label: string) => {
    if (disabled) return;
    commit(
      [{ type: "apply_effect", track, clip_id: clip.id, effect }],
      label
    );
  };

  return (
    <div className="space-y-3">
      {error && <IntelErrorStrip error={error} />}

      <SubHead>Captions</SubHead>
      <label className="block">
        Preset
        <select
          className="select mt-0.5"
          value={presetKey}
          disabled={disabled}
          onChange={(e) =>
            commit(
              [
                {
                  type: "update_caption_style",
                  track,
                  clip_id: clip.id,
                  preset: e.target.value,
                },
              ],
              "Caption preset"
            )
          }
        >
          {Object.values(presets).map((p: any) => (
            <option key={p.key} value={p.key} disabled={!p.brand_approved}>
              {p.label}
              {p.brand_approved ? "" : " (blocked by brand)"}
            </option>
          ))}
        </select>
      </label>
      {preset && !preset.brand_approved && (
        <div className="text-[11px]" style={{ color: "var(--text-faint)" }}>
          BrandDNA does not approve this preset for this workspace.
        </div>
      )}

      <Row label="Font size">
        <input
          type="range"
          className="w-full"
          min={8}
          max={200}
          value={Number(text.size ?? preset?.style?.size ?? 56)}
          disabled={disabled}
          onMouseUp={(e) =>
            styleOp(
              { size: Number((e.target as HTMLInputElement).value) },
              "Caption size"
            )
          }
        />
      </Row>
      <Row label="Position">
        <select
          className="select"
          value={text.position ?? preset?.style?.position ?? "bottom"}
          disabled={disabled}
          onChange={(e) => styleOp({ position: e.target.value }, "Caption position")}
        >
          {["top", "middle", "bottom"].map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
      </Row>
      <Row label="Colour">
        <input
          type="color"
          value={String(text.primary_color ?? "#ffffff")}
          disabled={disabled}
          onChange={(e) =>
            styleOp({ primary_color: e.target.value }, "Caption colour")
          }
        />
      </Row>
      <Row label="Emphasis">
        <Badge tone={wordLevel ? "success" : "muted"}>
          {wordLevel
            ? `word-level (${clip.words.length} words)`
            : "segment level - no word timing"}
        </Badge>
      </Row>
      <Row label="Animation">
        <select
          className="select"
          value={animation.entrance ?? preset?.style?.animation?.entrance ?? "none"}
          disabled={disabled}
          onChange={(e) =>
            styleOp(
              { animation: { ...animation, entrance: e.target.value } },
              "Caption animation"
            )
          }
        >
          {(vocab?.entrances ?? ["none", "fade", "pop", "wipe"]).map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>
      </Row>
      {vocab && (
        <div className="text-[11px]" style={{ color: "var(--text-faint)" }}>
          Word animation: {animation.word_animation ?? "none"}
        </div>
      )}

      <SubHead>Motion templates</SubHead>
      <div className="flex flex-wrap gap-1">
        {templates
          .filter((t) => !t.brand_forbidden)
          .map((t) => (
            <button
              key={t.key}
              className="btn-outline !text-[11px] !py-0.5"
              disabled={disabled}
              title={t.slots.length ? t.slots.join(", ") : t.description}
              onClick={() => {
                const bindings: Record<string, string> = {};
                for (const slot of t.slots) {
                  const value = window.prompt(
                    `${t.label} - ${slot}:`,
                    ""
                  );
                  if (value === null) return;
                  if (value.trim()) bindings[slot] = value.trim();
                }
                if (!Object.keys(bindings).length) return;
                commit(
                  [
                    {
                      type: "add_item",
                      track: "text",
                      clip: buildInstance(t, bindings, clip),
                    },
                  ],
                  `Add ${t.label}`
                );
              }}
            >
              + {t.label}
            </button>
          ))}
      </div>

      <ClipKeyframeEditor
        clip={clip}
        track={track}
        commit={commit}
        disabled={disabled}
      />

      {track !== "caption" && (
        <>
          <SubHead>Transitions</SubHead>
          <Row label="From this clip">
            <select
              className="select"
              value=""
              disabled={disabled}
              onChange={(e) => {
                if (!e.target.value) return;
                const [kind, id] = e.target.value.split("|");
                const next = e.target.selectedOptions[0]?.dataset.next;
                if (!next) return;
                commit(
                  [
                    {
                      type: "set_transition",
                      track,
                      clip_id: id,
                      to_item: next,
                      transition: {
                        from_item: id,
                        to_item: next,
                        type: "dissolve",
                        duration: 0.5,
                      },
                    },
                  ],
                  "Transition"
                );
                void kind;
              }}
            >
              <option value="">choose a transition…</option>
              {transitions
                .filter((t) => t.cross)
                .map((t) => (
                  <option key={t.key} value={`${t.key}|${clip.id}`}>
                    {t.label} →
                  </option>
                ))}
            </select>
          </Row>
          {storedTransition ? (
            <Row label="Active transition">
              <div className="space-y-1">
                <select
                  className="select"
                  aria-label="Transition type"
                  value={String(storedTransition.type ?? "").toUpperCase()}
                  disabled={disabled}
                  onChange={(e) =>
                    commit(
                      [
                        {
                          type: "set_transition",
                          track,
                          clip_id: storedTransition.from_item ?? clip.id,
                          to_item: storedTransition.to_item,
                          transition: {
                            ...storedTransition,
                            type: e.target.value.toUpperCase(),
                          },
                        },
                      ],
                      "Transition type"
                    )
                  }
                >
                  {transitions.map((t) => (
                    <option key={t.key} value={t.key.toUpperCase()}>
                      {t.label}
                    </option>
                  ))}
                </select>
                <label className="flex items-center gap-1.5 text-[11px]">
                  <input
                    type="number"
                    className="input"
                    aria-label="Transition duration seconds"
                    min={0}
                    max={Math.max(0, Number(clip?.duration ?? 0))}
                    step={0.1}
                    defaultValue={Number(storedTransition.duration ?? 0.5)}
                    onBlur={(e) => {
                      const value = Number(e.target.value);
                      if (!Number.isFinite(value) || value < 0) return;
                      if (value === Number(storedTransition.duration)) return;
                      commit(
                        [
                          {
                            type: "set_transition",
                            track,
                            clip_id: storedTransition.from_item ?? clip.id,
                            to_item: storedTransition.to_item,
                            transition: { ...storedTransition, duration: value },
                          },
                        ],
                        "Transition duration"
                      );
                    }}
                  />
                  <span style={{ color: "var(--text-faint)" }}>s overlap</span>
                </label>
                <div
                  className="text-[11px]"
                  style={{ color: "var(--text-faint)" }}
                >
                  {String(storedTransition.type ?? "").toUpperCase()} over{" "}
                  {Number(storedTransition.duration ?? 0).toFixed(2)}s, into{" "}
                  {String(storedTransition.to_item ?? "?")}
                </div>
              </div>
            </Row>
          ) : (
            <div className="text-[11px]" style={{ color: "var(--text-faint)" }}>
              No transition on this clip — it renders as a cut.
            </div>
          )}
        </>
      )}

      <SubHead>Effects</SubHead>
      <div className="space-y-1">
        {(clip?.effects ?? []).map((e: any, i: number) => (
          <div
            key={i}
            className="flex items-center justify-between rounded-lg px-2 py-1 text-[11.5px]"
            style={{ border: "var(--seam)" }}
          >
            <span className="font-mono">{e.type}</span>
            {/* A composite effect with no Work 12 mask renders NOT_AVAILABLE.
                Showing that here is what stops a stored effect from looking
                active in the UI while doing nothing on screen. */}
            {COMPOSITE_EFFECTS.has(String(e.type ?? "").toUpperCase()) &&
            !hasMask ? (
              <span
                className="text-[10.5px] font-semibold"
                style={{ color: "var(--danger, #e5484d)" }}
                title="Run a Work 12 segmentation pass: this effect is stored but will not render."
              >
                NOT_AVAILABLE — no mask
              </span>
            ) : null}
            <button
              className="btn-ghost !text-[11px] !py-0.5"
              disabled={disabled}
              onClick={() =>
                commit(
                  [
                    {
                      type: "remove_effect",
                      track,
                      clip_id: clip.id,
                      effect: e.type,
                    },
                  ],
                  "Remove effect"
                )
              }
            >
              remove
            </button>
          </div>
        ))}
        <div className="flex flex-wrap gap-1 pt-1">
          {effects
            .filter((e) => e.brand_allowed)
            .map((e) => (
              <button
                key={e.key}
                className="btn-outline !text-[11px] !py-0.5"
                disabled={disabled}
                title={e.description}
                onClick={() =>
                  effectOp(
                    {
                      type: e.key,
                      params: Object.fromEntries(
                        e.params.map((p) => [p.name, p.default])
                      ),
                    },
                    `Apply ${e.label}`
                  )
                }
              >
                + {e.label}
              </button>
            ))}
        </div>
      </div>

      <SubHead
        right={
          <button
            className="btn-ghost !text-[11px] !py-0.5"
            disabled={busy === "qc"}
            onClick={async () => {
              setBusy("qc");
              setError("");
              try {
                setQc(await callPost<QcReportWire>("/captions/qc", {
                  timeline_id: timelineId,
                }));
              } catch (exc) {
                setError(String(exc));
              } finally {
                setBusy("");
              }
            }}
          >
            {busy === "qc" ? "Checking…" : "Run QC"}
          </button>
        }
      >
        Caption / motion QC
      </SubHead>
      {qc && (
        <div className="space-y-1">
          <Badge tone={TONE[qc.status] ?? "muted"}>{qc.status}</Badge>
          {qc.checks
            .filter((c) => !c.passed)
            .map((c) => (
              <div
                key={c.name}
                className="text-[11px]"
                style={{ color: "var(--text-faint)" }}
              >
                {c.name}: {c.detail}
              </div>
            ))}
          {qc.checks.every((c) => c.passed) && (
            <div className="text-[11px]" style={{ color: "var(--text-faint)" }}>
              All {qc.total} checks passed.
            </div>
          )}
        </div>
      )}
      {assetId && (
        <div className="text-[11px]" style={{ color: "var(--text-faint)" }}>
          Evidence: {wordLevel ? "word timing available" : "no word timing"}
        </div>
      )}
    </div>
  );
}

/** Effects the renderer can only execute as a multi-input graph (mirrors
 *  graph.COMPOSITE_EFFECTS). They need a Work 12 segmentation mask. */
const COMPOSITE_EFFECTS = new Set(["MASK", "BACKGROUND_BLUR"]);

/** The closed interpolation vocabulary (mirrors graph.EASINGS). */
const EASINGS = ["LINEAR", "EASE_IN", "EASE_OUT", "EASE_IN_OUT", "HOLD"] as const;

/** The keyframable properties (mirrors graph.KEYFRAME_PROPS). */
const KF_PROPS = [
  { key: "x", label: "X", min: 0, max: 1, step: 0.01 },
  { key: "y", label: "Y", min: 0, max: 1, step: 0.01 },
  { key: "scale", label: "Scale", min: 0.05, max: 4, step: 0.05 },
  { key: "opacity", label: "Opacity", min: 0, max: 1, step: 0.05 },
] as const;

/**
 * Canonical clip keyframe editor (Work 13.1 §7).
 *
 * NOT the same thing as intel/KeyframeEditor: that one edits the Work 12
 * reframe PLAN (a PATCH against /media-intel/reframe, with no timeline undo),
 * while this one edits the canonical TIMELINE clip's keyframes through the
 * typed operation layer, so undo/redo works like any other timeline edit and
 * there is no UI-only state here. The marker strip is a read-only projection of
 * the committed chain (it renders the same numbers the renderer interpolates),
 * so it can never disagree with the timeline.
 */
function ClipKeyframeEditor({
  clip,
  track,
  commit,
  disabled,
}: {
  clip: any;
  track: string;
  commit: (ops: MotionOp[], label: string) => void;
  disabled: boolean;
}) {
  const chain: any[] = Array.isArray(clip?.keyframes) ? clip.keyframes : [];
  const duration = Number(clip?.duration ?? 0);
  const [t, setT] = useState(0);
  const [prop, setProp] = useState<string>("x");
  const [value, setValue] = useState<number>(0.5);
  const [easing, setEasing] = useState<string>("LINEAR");

  // A keyframe is only legal inside the clip, so the slider is clamped to it
  // rather than letting the user author a time the server must reject.
  const clampedT = Math.min(Math.max(Number(t) || 0, 0), Math.max(duration, 0));

  const sorted = [...chain].sort(
    (a, b) => Number(a.t) - Number(b.t) || String(a.id).localeCompare(String(b.id))
  );

  const add = () => {
    const id = `kf_${prop}_${Math.round(clampedT * 1000)}`;
    if (sorted.some((f) => f?.id === id)) return;
    commit(
      [
        {
          type: "add_keyframe",
          track,
          clip_id: clip.id,
          keyframe: {
            id,
            t: Number(clampedT.toFixed(4)),
            easing: easing as any,
            props: { [prop]: Number(value) },
          },
        },
      ],
      `Keyframe ${prop}`
    );
  };

  const remove = (id: string) =>
    commit(
      [{ type: "delete_keyframe", track, clip_id: clip.id, keyframe_id: id }],
      "Delete keyframe"
    );

  const retime = (id: string, at: number) =>
    commit(
      [
        {
          type: "move_keyframe",
          track,
          clip_id: clip.id,
          keyframe_id: id,
          t: Number(Number(at).toFixed(4)),
        },
      ],
      "Move keyframe"
    );

  const reEase = (id: string, next: string) =>
    commit(
      [
        {
          type: "update_keyframe",
          track,
          clip_id: clip.id,
          keyframe_id: id,
          keyframe: { easing: next },
        },
      ],
      "Keyframe easing"
    );

  return (
    <>
      <SubHead>Keyframes</SubHead>
      {sorted.length === 0 ? (
        <div className="text-[11px]" style={{ color: "var(--text-faint)" }}>
          No keyframes — the clip renders at its static transform.
        </div>
      ) : (
        <>
          {/* timeline markers: position = time within the clip */}
          <div
            className="relative h-6"
            style={{ background: "var(--seam)", borderRadius: 6 }}
            role="group"
            aria-label="Keyframe markers"
          >
            {sorted.map((f) => (
              <button
                key={f.id}
                type="button"
                title={`${f.id} @ ${Number(f.t).toFixed(2)}s`}
                aria-label={`Keyframe ${f.id} at ${Number(f.t).toFixed(2)} seconds`}
                onClick={() => {
                  setT(Number(f.t));
                  const first = Object.keys(f.props ?? {})[0];
                  if (first) {
                    setProp(first);
                    setValue(Number((f.props as any)[first]));
                  }
                  setEasing(String(f.easing ?? "LINEAR"));
                }}
                style={{
                  position: "absolute",
                  left: `${duration > 0 ? (Number(f.t) / duration) * 100 : 0}%`,
                  top: 2,
                  width: 9,
                  height: 18,
                  marginLeft: -4,
                  borderRadius: 3,
                  border: "1px solid var(--line)",
                  background: "var(--accent)",
                }}
              />
            ))}
          </div>
          <div className="space-y-1">
            {sorted.map((f) => (
              <div
                key={f.id}
                className="flex items-center justify-between gap-1 text-[11.5px]"
              >
                <span className="font-mono" style={{ minWidth: 58 }}>
                  {Number(f.t).toFixed(2)}s
                </span>
                <span className="font-mono" style={{ color: "var(--text-faint)" }}>
                  {Object.entries(f.props ?? {})
                    .map(([k, v]) => `${k}=${Number(v).toFixed(2)}`)
                    .join(" ")}
                </span>
                <select
                  className="select !py-0.5 !text-[11px]"
                  aria-label={`Easing for ${f.id}`}
                  value={String(f.easing ?? "LINEAR")}
                  disabled={disabled}
                  onChange={(e) => reEase(f.id, e.target.value)}
                >
                  {EASINGS.map((e) => (
                    <option key={e} value={e}>
                      {e}
                    </option>
                  ))}
                </select>
                <input
                  type="number"
                  className="input !py-0.5 !text-[11px]"
                  aria-label={`Time for ${f.id}`}
                  min={0}
                  max={Math.max(duration, 0)}
                  step={0.05}
                  defaultValue={Number(f.t)}
                  disabled={disabled}
                  onBlur={(e) => {
                    const at = Number(e.target.value);
                    if (Number.isFinite(at) && at !== Number(f.t)) {
                      retime(f.id, Math.min(Math.max(at, 0), Math.max(duration, 0)));
                    }
                  }}
                />
                <button
                  className="btn-ghost !text-[11px] !py-0.5"
                  disabled={disabled}
                  onClick={() => remove(f.id)}
                >
                  ✕
                </button>
              </div>
            ))}
          </div>
        </>
      )}

      <Row label="Add keyframe">
        <div className="space-y-1">
          <div className="flex items-center gap-1.5">
            <select
              className="select"
              aria-label="Keyframe property"
              value={prop}
              disabled={disabled}
              onChange={(e) => setProp(e.target.value)}
            >
              {KF_PROPS.map((p) => (
                <option key={p.key} value={p.key}>
                  {p.label}
                </option>
              ))}
            </select>
            <input
              type="number"
              className="input"
              aria-label="Keyframe value"
              min={KF_PROPS.find((p) => p.key === prop)?.min ?? 0}
              max={KF_PROPS.find((p) => p.key === prop)?.max ?? 1}
              step={KF_PROPS.find((p) => p.key === prop)?.step ?? 0.01}
              value={value}
              disabled={disabled}
              onChange={(e) => setValue(Number(e.target.value))}
            />
          </div>
          <div className="flex items-center gap-1.5">
            <input
              type="number"
              className="input"
              aria-label="Keyframe time"
              min={0}
              max={Math.max(duration, 0)}
              step={0.05}
              value={Number(clampedT.toFixed(3))}
              disabled={disabled}
              onChange={(e) => setT(Number(e.target.value))}
            />
            <select
              className="select"
              aria-label="Keyframe easing"
              value={easing}
              disabled={disabled}
              onChange={(e) => setEasing(e.target.value)}
            >
              {EASINGS.map((e) => (
                <option key={e} value={e}>
                  {e}
                </option>
              ))}
            </select>
            <button
              className="btn-ghost !text-[11px]"
              disabled={disabled}
              onClick={add}
            >
              + keyframe
            </button>
          </div>
        </div>
      </Row>
    </>
  );
}

/** Build a motion-template clip for an `add_item` op (mirrors MotionInstance). */
function buildInstance(
  tpl: MotionTemplateWire,
  bindings: Record<string, string>,
  anchor: any
): Record<string, unknown> {
  const render = (body: string) =>
    body.replace(/\{\{\s*([a-z0-9_]+)\s*\}\}/g, (_m, k) => bindings[k] ?? "");
  const content = [render(tpl.body), render(tpl.sub_body || "")]
    .filter(Boolean)
    .join("\n");
  const start = Number(anchor?.start ?? 0);
  return {
    id: `motion_${tpl.key}_${Math.round(start * 1000)}`,
    name: content.split("\n")[0],
    start,
    duration: tpl.default_duration,
    source: {},
    effects: [],
    source_start: 0,
    volume: 1,
    speed: 1,
    fade_in: 0,
    fade_out: 0,
    transform: {},
    text: { content, preset: tpl.type.toLowerCase(), ...tpl.style },
    transition_in: "cut",
    transition_out: "cut",
  };
}