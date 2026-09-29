import { useMemo, useState } from "react";
import { api, getToken, wsApi } from "../lib/api";
import { useFetch, useInterval } from "../hooks/hooks";
import {
  Accordion,
  Badge,
  Card,
  ConfirmButton,
  Empty,
  ErrorBox,
  Field,
  Loading,
  Modal,
  PageHeader,
  Progress,
  Tabs,
  toast,
} from "../components/ui";
import { fmtAgo, fmtDate, statusTone } from "../lib/format";

/* ---- Wire shapes. The export row is LOCKED (contracts §11) --------------- */

/* engine/exporter/formats.py::FormatSpec.as_dict -> {format, available, reason,
 * kind, media_type, suffix}; `reason` is always populated when unavailable. */
type ExportFormat = {
  format: string;
  available: boolean;
  reason?: string | null;
  kind?: string;
  media_type?: string;
  suffix?: string;
};

type ProfileConfig = {
  width: number | null;
  height: number | null;
  fps: number | null;
  video_codec?: string | null;
  bitrate_kbps?: number | null;
  audio_codec?: string | null;
  audio_bitrate_kbps?: number | null;
  audio_channels?: number | null;
  captions?: { enabled?: boolean; formats?: string[] } | null;
  watermark?: { enabled?: boolean; text?: string | null } | null;
  color?: { matrix?: string; transfer?: string } | null;
};

type ExportProfile = {
  id: string;
  name: string;
  preset: string;
  config: ProfileConfig;
  is_builtin?: boolean;
  created_at?: string;
};

type Check = { name: string; passed: boolean; detail?: string | null };

type ExportRow = {
  id: string;
  format: string;
  /** _profile_brief(): always an object; name/preset are "" when the profile
   *  row was removed (profile_id is ON DELETE SET NULL). */
  profile: { name: string; preset: string };
  target: { type: string; id: string };
  state: "QUEUED" | "RUNNING" | "COMPLETE" | "FAILED" | "CANCELLED" | string;
  progress: number;
  verification: { complete: boolean; checks?: Check[] } | null;
  artifact: { url: string; size: number | null; checksum: string | null } | null;
  error: string | null;
  attempt: number;
  created_at: string;
  finished_at: string | null;
};

/* api/v1/exports.py::EXPORT_TARGET_TYPES — NOT the project_targets vocabulary */
const TARGET_TYPES = ["timeline", "video", "asset"];

/* contracts §11 + engine/exporter/profiles.py: the two preset families whose
 * legal containers are enumerated. Everything else is unconstrained here and
 * left to the server's validator. */
const PRESET_FORMATS: Record<string, string[]> = {
  AUDIO_ONLY: ["MP3", "WAV"],
  CAPTIONS_ONLY: ["SRT", "VTT", "ASS", "TXT"],
};

/* engine/exporter/jobs.py::MAX_ATTEMPTS */
const MAX_ATTEMPTS = 5;

type Tab = "jobs" | "formats" | "profiles";

function isFetchable(url: string): boolean {
  return /^https?:\/\//i.test(url) || url.startsWith("/");
}

function fmtSize(bytes?: number | null): string {
  if (bytes == null) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function configSummary(c: ProfileConfig, preset: string): string {
  const bits: string[] = [];
  if (preset === "AUDIO_ONLY") {
    bits.push("audio only (no video stream)");
  } else if (preset === "CAPTIONS_ONLY") {
    bits.push("captions only (no media)");
  } else if (c.width && c.height) {
    bits.push(`${c.width}×${c.height}${c.fps ? ` @${Math.round(c.fps)}` : ""}`);
  } else {
    bits.push("source passthrough");
  }
  if (c.video_codec) bits.push(c.video_codec);
  if (c.bitrate_kbps) bits.push(`${(c.bitrate_kbps / 1000).toFixed(1)} Mbps`);
  if (c.audio_codec) bits.push(`${c.audio_codec}${c.audio_bitrate_kbps ? ` ${c.audio_bitrate_kbps}k` : ""}`);
  if (c.captions?.enabled) bits.push(`captions ${(c.captions.formats ?? []).join("/") || "on"}`);
  if (c.watermark?.enabled) bits.push(`watermark${c.watermark.text ? ` "${c.watermark.text}"` : ""}`);
  if (c.color?.matrix) bits.push(c.color.matrix);
  return bits.join(" · ");
}

export default function Exports() {
  const [tab, setTab] = useState<Tab>("jobs");
  const [busy, setBusy] = useState("");
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ target_type: "timeline", target_id: "", profile_id: "", format: "" });

  const jobs = useFetch<{ items?: ExportRow[] }>(() => wsApi.get("/exports"), []);
  const formats = useFetch<{ items?: ExportFormat[] } | ExportFormat[]>(() => wsApi.get("/exports/formats"), []);
  const profiles = useFetch<{ items?: ExportProfile[] } | ExportProfile[]>(() => wsApi.get("/exports/profiles"), []);

  /* RBAC: POST /exports, /retry and /cancel all carry the workspace `member`
   * floor (api/v1/exports.py:286/346/363) while GETs are viewer-floor. Resolve
   * MY workspace role the same way CommentsPanel does: /auth/me for my id,
   * /members for the role map. A confirmed viewer never sees the mutating
   * buttons; if either call fails we stay permissive and any denied call
   * surfaces as a 403 toast instead of a silent no-op. */
  const me = useFetch<{ id?: string; is_superuser?: boolean }>(
    () => api("GET", "/auth/me") as Promise<{ id?: string; is_superuser?: boolean }>,
    []
  );
  const members = useFetch<{ items?: { user_id: string; role: string }[] }>(
    () => wsApi.get("/members") as Promise<{ items?: { user_id: string; role: string }[] }>,
    []
  );
  const myRole = members.data?.items?.find((m) => m.user_id === me.data?.id)?.role;
  const canMutate = !(myRole === "viewer" && !me.data?.is_superuser);

  const formatRows: ExportFormat[] = useMemo(() => {
    const d = formats.data as any;
    if (Array.isArray(d)) return d;
    return d?.items ?? [];
  }, [formats.data]);
  const profileRows: ExportProfile[] = useMemo(() => {
    const d = profiles.data as any;
    if (Array.isArray(d)) return d;
    return d?.items ?? [];
  }, [profiles.data]);
  const jobs_ = jobs.data?.items ?? [];

  // Poll only while something is actually in flight.
  const inFlight = jobs_.some((j) => j.state === "QUEUED" || j.state === "RUNNING");
  useInterval(jobs.reload, inFlight ? 4000 : null);

  function reloadAll() {
    jobs.reload();
    formats.reload();
    profiles.reload();
  }

  async function run(fn: () => Promise<unknown>, success: string, failure: string) {
    setBusy(failure);
    try {
      await fn();
      toast(success, "success");
      jobs.reload();
      return true;
    } catch (e: any) {
      toast(e?.message ?? "request failed", "error", failure);
      return false;
    } finally {
      setBusy("");
    }
  }

  const selectedFormat = formatRows.find((f) => f.format === form.format) ?? null;
  const selectedProfile = profileRows.find((p) => p.id === form.profile_id) ?? null;
  const allowed = selectedProfile ? PRESET_FORMATS[selectedProfile.preset] ?? null : null;
  const unavailableReason = selectedFormat && !selectedFormat.available
    ? selectedFormat.reason || "the exporter reported no reason"
    : null;
  const formatNotAllowed = !!allowed && !!form.format && !allowed.includes(form.format)
    ? `${selectedProfile?.preset} only produces ${allowed.join(" or ")}`
    : null;
  const blocked = !form.target_id.trim() || !form.profile_id || !form.format || !!unavailableReason || !!formatNotAllowed;

  async function create() {
    if (blocked || !canMutate) return;
    const ok = await run(
      () => wsApi.post("/exports", {
        profile_id: form.profile_id,
        format: form.format,
        target_type: form.target_type,
        target_id: form.target_id.trim(),
      }),
      `Export queued — ${form.format}`,
      "Export could not start"
    );
    if (ok) {
      setOpen(false);
      setForm({ target_type: form.target_type, target_id: "", profile_id: "", format: "" });
      setTab("jobs");
    }
  }

  async function download(row: ExportRow) {
    const art = row.artifact;
    if (!art?.url) return;
    if (!isFetchable(art.url)) {
      toast(`Artifact is stored at a server path, not a URL: ${art.url}`, "warning", "No download link");
      return;
    }
    try {
      const headers: Record<string, string> = {};
      const token = getToken();
      if (token) headers.Authorization = `Bearer ${token}`;
      const res = await fetch(art.url, { headers });
      if (!res.ok) throw new Error(`server answered ${res.status}`);
      const blob = await res.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `export-${row.id.slice(0, 8)}.${row.format.toLowerCase()}`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 5000);
      toast("Artifact downloaded", "success");
    } catch (e: any) {
      toast(e?.message ?? "download failed", "error", "Download failed");
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Exports"
        subtitle="Verified deliverables — every artifact is probed and checksummed before it is called complete, and missing encoders are reported as NOT_AVAILABLE rather than faked."
        actions={
          canMutate ? (
            <button className="btn-primary !text-xs" onClick={() => setOpen(true)}>
              + New export
            </button>
          ) : (
            <span className="text-[11.5px]" style={{ color: "var(--text-faint)" }}>
              workspace viewer — queueing is hidden; downloads stay available
            </span>
          )
        }
      />

      <Tabs
        tabs={[
          { key: "jobs", label: "Jobs", count: jobs_.length },
          { key: "formats", label: "Formats", count: formatRows.length },
          { key: "profiles", label: "Profiles", count: profileRows.length },
        ]}
        active={tab}
        onChange={setTab}
      />

      {tab === "jobs" &&
        (jobs.loading && !jobs.data ? (
          <Loading rows={3} />
        ) : jobs.error && !jobs.data ? (
          <ErrorBox error={jobs.error} onRetry={jobs.reload} />
        ) : !jobs_.length ? (
          <Card>
            <Empty
              title="No exports yet"
              hint="Queue one from a timeline, campaign or content item. Jobs are verified before they report complete — check the verdict, not just the state."
              action={
                <button className="btn-outline !text-xs" onClick={jobs.reload}>
                  Refresh
                </button>
              }
            />
          </Card>
        ) : (
          <div className="space-y-3">
            {jobs_.map((j) => (
              <JobRow
                key={j.id}
                job={j}
                busy={busy}
                canMutate={canMutate}
                onRetry={() => run(() => wsApi.post(`/exports/${j.id}/retry`), `Retrying ${j.format} export`, "Retry rejected")}
                onCancel={() => run(() => wsApi.post(`/exports/${j.id}/cancel`), "Export cancelled", "Cancel rejected")}
                onDownload={() => download(j)}
              />
            ))}
          </div>
        ))}

      {tab === "formats" &&
        (formats.loading && !formats.data ? (
          <Loading rows={2} />
        ) : formats.error && !formats.data ? (
          <ErrorBox error={formats.error} onRetry={formats.reload} />
        ) : !formatRows.length ? (
          <Card>
            <Empty
              title="No formats reported"
              hint="The exporter publishes an honest availability probe per format — if this list is empty, the registry did not load."
              action={
                <button className="btn-outline !text-xs" onClick={formats.reload}>
                  Re-probe
                </button>
              }
            />
          </Card>
        ) : (
          <>
            <div className="text-[12.5px]" style={{ color: "var(--text-muted)" }}>
              {formatRows.filter((f) => f.available).length} of {formatRows.length} formats available in this
              environment. Unavailable ones stay listed with the reason — nothing is silently dropped.
            </div>
            <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-3">
              {formatRows.map((f) => (
                <Card key={f.format}>
                  <div className="flex items-center gap-2 flex-wrap">
                    <b className="font-mono text-[13.5px]">{f.format}</b>
                    {f.kind && <Badge tone="muted">{f.kind}</Badge>}
                    <Badge tone={f.available ? "success" : "muted"}>
                      {f.available ? "AVAILABLE" : "NOT_AVAILABLE"}
                    </Badge>
                  </div>
                  <div className="text-[12px] mt-1.5" style={{ color: f.available ? "var(--text-faint)" : "var(--warn)" }}>
                    {f.available
                      ? "Exporter and required encoders are present."
                      : f.reason || "the capability probe reported no reason."}
                  </div>
                </Card>
              ))}
            </div>
          </>
        ))}

      {tab === "profiles" &&
        (profiles.loading && !profiles.data ? (
          <Loading rows={2} />
        ) : profiles.error && !profiles.data ? (
          <ErrorBox error={profiles.error} onRetry={profiles.reload} />
        ) : !profileRows.length ? (
          <Card>
            <Empty
              title="No export profiles"
              hint="The eight built-in presets (YouTube 4K/1080p, Shorts, Reels, TikTok, Archive Master, Audio only, Captions only) are seeded on first read."
              action={
                <button className="btn-outline !text-xs" onClick={profiles.reload}>
                  Refresh
                </button>
              }
            />
          </Card>
        ) : (
          <Card pad={false}>
            <div className="overflow-x-auto">
              <table className="table">
                <thead>
                  <tr>
                    <th>Profile</th>
                    <th>Preset</th>
                    <th>Configuration</th>
                    <th>Legal formats</th>
                  </tr>
                </thead>
                <tbody>
                  {profileRows.map((p) => (
                    <tr key={p.id}>
                      <td>
                        <div className="font-medium">{p.name}</div>
                        {p.is_builtin && (
                          <div className="mt-1">
                            <Badge tone="info">built-in</Badge>
                          </div>
                        )}
                      </td>
                      <td className="font-mono text-[12px] whitespace-nowrap">{p.preset}</td>
                      <td className="text-[12px]" style={{ color: "var(--text-muted)" }}>
                        {configSummary(p.config ?? {}, p.preset)}
                      </td>
                      <td className="font-mono text-[12px]" style={{ color: "var(--text-muted)" }}>
                        {PRESET_FORMATS[p.preset]?.join(", ") ?? "any available format"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        ))}

      {/* ---- new export ---------------------------------------------------- */}
      <Modal open={open} onClose={() => setOpen(false)} title="New export">
        <Field label="Target type">
          <select
            className="select"
            value={form.target_type}
            onChange={(e) => setForm({ ...form, target_type: e.target.value })}
          >
            {TARGET_TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Target id" hint="The timeline, video or asset row you are exporting.">
          <input
            className="input font-mono"
            value={form.target_id}
            onChange={(e) => setForm({ ...form, target_id: e.target.value })}
            placeholder="timeline id"
          />
        </Field>
        <Field label="Profile" hint="Presets fix resolution, codecs, bitrate, captions and watermark.">
          <select
            className="select"
            value={form.profile_id}
            onChange={(e) => setForm({ ...form, profile_id: e.target.value })}
          >
            <option value="">Select a profile…</option>
            {profileRows.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} — {p.preset}
              </option>
            ))}
          </select>
        </Field>
        <Field
          label="Format"
          hint={
            unavailableReason
              ? undefined
              : formatNotAllowed ?? "Formats reported NOT_AVAILABLE are disabled below with the reason."
          }
        >
          <select
            className="select"
            value={form.format}
            onChange={(e) => setForm({ ...form, format: e.target.value })}
          >
            <option value="">Select a format…</option>
            {formatRows.map((f) => (
              <option key={f.format} value={f.format} disabled={!f.available}>
                {f.format}
                {f.available ? "" : ` — NOT_AVAILABLE: ${f.reason || "no reason reported"}`}
              </option>
            ))}
          </select>
        </Field>

        {selectedProfile && (
          <div className="text-[12px] mb-3" style={{ color: "var(--text-muted)" }}>
            {selectedProfile.preset} → {configSummary(selectedProfile.config ?? {}, selectedProfile.preset)}
          </div>
        )}
        {unavailableReason && (
          <div
            className="rounded-xl px-3 py-2 mb-3 text-[12.5px]"
            style={{ background: "var(--warn-dim)", border: "1px solid var(--warn)" }}
          >
            <b style={{ color: "var(--warn)" }}>{form.format} is NOT_AVAILABLE</b> — {unavailableReason}
          </div>
        )}
        {formatNotAllowed && (
          <div
            className="rounded-xl px-3 py-2 mb-3 text-[12.5px]"
            style={{ background: "var(--warn-dim)", border: "1px solid var(--warn)" }}
          >
            <b style={{ color: "var(--warn)" }}>Profile/format mismatch</b> — {formatNotAllowed}.
          </div>
        )}

        <div className="flex justify-end gap-2">
          <button className="btn-ghost !text-xs" onClick={() => setOpen(false)}>
            Cancel
          </button>
          <button className="btn-primary !text-xs" disabled={blocked || busy !== ""} onClick={create}>
            {busy ? "…" : "Queue export"}
          </button>
        </div>
      </Modal>
    </div>
  );
}

function JobRow({
  job,
  busy,
  canMutate,
  onRetry,
  onCancel,
  onDownload,
}: {
  job: ExportRow;
  busy: string;
  /** false for workspace viewers — retry/cancel carry the member floor. */
  canMutate: boolean;
  onRetry: () => void;
  onCancel: () => void;
  onDownload: () => void;
}) {
  const checks = job.verification?.checks ?? [];
  const failed = checks.filter((c) => !c.passed);
  /* engine/exporter/jobs.py: RETRYABLE_STATES=(FAILED,CANCELLED),
   * CANCELLABLE_STATES=(QUEUED,RUNNING), MAX_ATTEMPTS=5. */
  const retryable = job.state === "FAILED" || job.state === "CANCELLED";
  const exhausted = retryable && job.attempt >= MAX_ATTEMPTS;
  const canRetry = retryable && !exhausted;
  const canCancel = job.state === "QUEUED" || job.state === "RUNNING";
  return (
    <Card>
      <div className="flex items-start gap-3 flex-wrap">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <b className="font-mono text-[13.5px]">{job.format}</b>
            {/* lib/format.ts::statusTone has no "COMPLETE" entry (its success
             * list holds "COMPLETED"), and exports finish as COMPLETE — map
             * that one state locally so a finished job doesn't render muted. */}
            <Badge tone={job.state === "COMPLETE" ? "success" : statusTone(job.state)}>
              {job.state}
            </Badge>
            {job.profile && <Badge tone="muted">{job.profile.preset}</Badge>}
            {job.attempt > 0 && <Badge tone="muted">attempt {job.attempt}</Badge>}
          </div>
          <div className="font-mono text-[11.5px] mt-1" style={{ color: "var(--text-faint)" }}>
            {job.profile?.name ?? "no profile"} · {job.target ? `${job.target.type}:${job.target.id}` : "no target"} ·
            queued {fmtAgo(job.created_at)}
            {job.finished_at ? ` · finished ${fmtDate(job.finished_at)}` : ""}
          </div>
        </div>
        <div className="flex gap-2 items-center">
          {job.artifact && (
            <button className="btn-primary !text-xs" onClick={onDownload} title={job.artifact.url}>
              Download
            </button>
          )}
          {canMutate && canRetry && (
            <button className="btn-outline !text-xs" disabled={busy !== ""} onClick={onRetry}>
              {busy === "Retry rejected" ? "…" : "Retry"}
            </button>
          )}
          {canMutate && exhausted && (
            <span className="text-[11.5px]" style={{ color: "var(--text-faint)" }}>
              {MAX_ATTEMPTS} attempts used — queue a new export
            </span>
          )}
          {canMutate && canCancel && (
            <ConfirmButton onConfirm={onCancel} confirmText="Cancel job?">
              Cancel
            </ConfirmButton>
          )}
        </div>
      </div>

      {job.state === "RUNNING" || job.state === "QUEUED" ? (
        <div className="mt-3">
          <div className="flex items-center justify-between text-[11.5px] mb-1" style={{ color: "var(--text-faint)" }}>
            <span>{job.state === "QUEUED" ? "waiting for a worker" : "building"}</span>
            <span className="font-mono">{job.progress ?? 0}%</span>
          </div>
          <Progress value={job.progress ?? 0} />
        </div>
      ) : null}

      {job.verification && (
        <div className="mt-3">
          <div className="flex items-center gap-2 flex-wrap">
            <Badge tone={job.verification.complete ? "success" : "error"}>
              {job.verification.complete ? "verified" : "verification failed"}
            </Badge>
            <span className="text-[11.5px]" style={{ color: "var(--text-faint)" }}>
              {checks.length ? `${checks.length - failed.length}/${checks.length} checks passed` : "no checks recorded"}
            </span>
          </div>
          {checks.length > 0 && (
            <div className="mt-2">
              <Accordion title="Verification checks" badge={<Badge tone="muted">{checks.length}</Badge>}>
                <div className="space-y-1.5">
                  {checks.map((c, i) => (
                    <div key={`${c.name}-${i}`} className="flex items-start gap-2 text-[12.5px]">
                      <span style={{ color: c.passed ? "var(--accent)" : "var(--danger)" }}>{c.passed ? "✓" : "✕"}</span>
                      <span className="font-mono text-[12px]">{c.name}</span>
                      {c.detail && (
                        <span className="flex-1" style={{ color: "var(--text-faint)" }}>
                          {c.detail}
                        </span>
                      )}
                    </div>
                  ))}
                </div>
              </Accordion>
            </div>
          )}
        </div>
      )}

      {job.artifact && (
        <div className="font-mono text-[11.5px] mt-2 break-all" style={{ color: "var(--text-faint)" }}>
          {fmtSize(job.artifact.size)}
          {job.artifact.checksum ? ` · sha256 ${job.artifact.checksum.slice(0, 16)}…` : ""}
        </div>
      )}

      {job.error && (
        <div className="text-[12.5px] mt-2.5 break-words" style={{ color: "var(--danger)" }}>
          ⓘ {job.error}
        </div>
      )}
    </Card>
  );
}
