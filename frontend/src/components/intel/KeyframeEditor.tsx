/* Work 12 FE lane -- editable reframe keyframes.
 *
 * `POST /media-intel/reframe` never bakes the crop: it persists keyframes, and
 * `PATCH /media-intel/reframe/keyframes/{id}` is the edit (the previous values
 * land in the keyframe's own history, which the route echoes back as
 * `history_entry`). Editing a keyframe mutates the PLAN, not the timeline, so
 * no QC verdict and no undo entry is involved -- but every edit is still an
 * explicit PATCH, never a silent local re-layout.
 */

import { useState } from "react";
import { Badge, toast } from "../ui";
import { patchKeyframe, parseIntelError, type IntelError, type ReframeKeyframe } from "./intelApi";
import { IntelErrorStrip, sec, shortId } from "./shared";

const SOURCE_TONE: Record<string, string> = {
  active_speaker: "info",
  subject: "info",
  focal_point: "muted",
  fallback: "muted",
  operator: "warning",
};

type Props = {
  keyframes: ReframeKeyframe[];
  readOnly: boolean;
  onPatched: (keyframe: ReframeKeyframe) => void;
};

/** The five fields a keyframe patch accepts (KeyframePatchBody). */
const FIELDS: { key: "t_s" | "x" | "y" | "scale"; label: string; step: number }[] = [
  { key: "t_s", label: "t", step: 0.1 },
  { key: "x", label: "x", step: 0.01 },
  { key: "y", label: "y", step: 0.01 },
  { key: "scale", label: "scale", step: 0.01 },
];

export default function KeyframeEditor({ keyframes, readOnly, onPatched }: Props) {
  const [openId, setOpenId] = useState<string | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState("");
  const [error, setError] = useState<IntelError | null>(null);

  async function save(kf: ReframeKeyframe) {
    const patch: Record<string, number> = {};
    for (const f of FIELDS) {
      const raw = draft[f.key];
      if (raw == null || raw === "") continue;
      const value = Number(raw);
      if (!Number.isFinite(value)) {
        setError({ status: 422, message: `${f.label} must be a number`, kind: "plain", qc: null, conflict: null });
        return;
      }
      if (value !== kf[f.key]) patch[f.key] = value;
    }
    if (!Object.keys(patch).length) {
      setOpenId(null);
      return;
    }
    setBusy(kf.id);
    setError(null);
    try {
      const res = await patchKeyframe(kf.id, patch);
      onPatched(res);
      setOpenId(null);
      setDraft({});
      toast(`keyframe @ ${sec(res.t_s)} saved`, "success", "Reframe");
    } catch (e) {
      setError(parseIntelError(e));
    } finally {
      setBusy("");
    }
  }

  if (!keyframes.length) {
    return (
      <div className="text-[11.5px] py-2" style={{ color: "var(--text-faint)" }}>
        No keyframes — run Auto Reframe to compute an editable crop path.
      </div>
    );
  }

  return (
    <div className="space-y-1.5">
      {error && <IntelErrorStrip error={error.message} />}
      <div className="max-h-[260px] overflow-auto space-y-1 pr-1">
        {keyframes.map((kf) => {
          const open = openId === kf.id;
          const rect = kf.rect ?? {};
          return (
            <div key={kf.id} className="rounded-lg px-2 py-1.5 text-[11.5px]" style={{ border: "var(--seam)" }}>
              <div className="flex items-center justify-between gap-1">
                <span className="font-mono" style={{ color: "var(--text-muted)" }}>
                  {sec(kf.t_s)} · x{kf.x.toFixed(2)} y{kf.y.toFixed(2)} s{kf.scale.toFixed(2)}
                </span>
                <Badge tone={SOURCE_TONE[kf.source] ?? "muted"}>{kf.source || "unknown"}</Badge>
              </div>
              <div className="font-mono" style={{ color: "var(--text-faint)" }}>
                {shortId(kf.id, 8)}
                {kf.reason ? ` · ${kf.reason}` : ""}
                {rect && Object.keys(rect).length
                  ? ` · ${Object.entries(rect)
                      .map(([k, v]) => `${k}=${String(v)}`)
                      .join(" ")}`
                  : ""}
              </div>
              {!readOnly && (
                <button
                  className="btn-ghost !text-[11px] !py-0.5 mt-0.5"
                  onClick={() => {
                    setOpenId(open ? null : kf.id);
                    setDraft(
                      open
                        ? {}
                        : { t_s: String(kf.t_s), x: String(kf.x), y: String(kf.y), scale: String(kf.scale) }
                    );
                    setError(null);
                  }}
                >
                  {open ? "✕ cancel" : "✎ edit"}
                </button>
              )}
              {open && !readOnly && (
                <div className="mt-1 space-y-1">
                  <div className="grid grid-cols-2 gap-1">
                    {FIELDS.map((f) => (
                      <label key={f.key} className="block">
                        <span className="font-mono">{f.label}</span>
                        <input
                          className="input !py-0.5 !text-[11.5px] mt-0.5"
                          type="number"
                          step={f.step}
                          value={draft[f.key] ?? ""}
                          onChange={(e) => setDraft((prev) => ({ ...prev, [f.key]: e.target.value }))}
                        />
                      </label>
                    ))}
                  </div>
                  <button
                    className="btn-primary !text-[11px] !py-0.5"
                    disabled={busy === kf.id}
                    onClick={() => void save(kf)}
                  >
                    {busy === kf.id ? "Saving…" : "Save keyframe"}
                  </button>
                  <div className="text-[10.5px]" style={{ color: "var(--text-faint)" }}>
                    saved as source=operator; the previous values stay in the keyframe history
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
