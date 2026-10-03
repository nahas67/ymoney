/* Work 12 FE lane -- shared presentational bits for the two intel panels.
 *
 * House style comes from components/collab (Reviews.tsx / CommentsPanel.tsx):
 * one honest error strip with the server's own words and a retry, a distinct
 * empty state, a short read-only note for viewers. Nothing here decides
 * anything -- it only renders facts the API already returned.
 *
 * NO SENSITIVE INFERENCE is ever rendered (contracts §0): speakers appear as
 * anonymous `SPEAKER_00` ids or as an explicit unresolved reason, never as a
 * guess about a person.
 */

import type { ReactNode } from "react";
import { Badge } from "../ui";
import type { IntelRun, QcResult, QcVerdict } from "./intelApi";

/** mm:ss.d for media time. */
export function sec(t: number | null | undefined): string {
  const v = Number(t ?? 0);
  const m = Math.floor(v / 60);
  const s = (Math.abs(v) % 60).toFixed(1).padStart(4, "0");
  return `${String(m).padStart(2, "0")}:${s}`;
}

export function shortId(s: string | null | undefined, n = 8): string {
  if (!s) return "—";
  return s.length > n ? `${s.slice(0, n)}…` : s;
}

export function pct(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(Number(v))) return "—";
  return `${(Number(v) * 100).toFixed(0)}%`;
}

export const VERDICT_TONE: Record<string, string> = {
  PASS: "success",
  PASS_WITH_WARNINGS: "warning",
  REVIEW_REQUIRED: "warning",
  FAIL: "error",
};

export const RUN_TONE: Record<string, string> = {
  COMPLETED: "success",
  PENDING: "muted",
  RUNNING: "info",
  FAILED: "error",
  CANCELLED: "muted",
  UNAVAILABLE: "warning",
};

/**
 * One honest error strip: the server's message verbatim, plus a retry.
 * `ApiError.message` is already the server's `detail` (a stringified dict when
 * the detail was an object) -- parseIntelError() has turned that into a
 * sentence, so nothing is dumped as raw JSON.
 */
export function IntelErrorStrip({
  error,
  onRetry,
  retryLabel = "Retry",
  children,
}: {
  error: string;
  onRetry?: () => void;
  retryLabel?: string;
  children?: ReactNode;
}) {
  if (!error) return null;
  return (
    <div
      className="rounded-lg px-2.5 py-2 text-[12px]"
      style={{ border: "1px solid var(--danger)", background: "var(--danger-dim)" }}
      role="alert"
    >
      <div className="font-semibold" style={{ color: "var(--danger)" }}>
        Request failed
      </div>
      <div className="mt-0.5 break-words" style={{ color: "var(--text-muted)" }}>
        {error}
      </div>
      {children}
      {onRetry && (
        <button className="btn-outline !text-xs !py-0.5 mt-2" onClick={onRetry}>
          {retryLabel}
        </button>
      )}
    </div>
  );
}

/**
 * A capability that is genuinely dark. The action stays disabled and the
 * provider's own reason is printed -- never a fake success, never a silent
 * no-op (contracts §0/§15).
 */
export function UnavailableNote({ reason, children }: { reason: string; children?: ReactNode }) {
  if (!reason) return null;
  return (
    <div
      className="rounded-lg px-2.5 py-2 text-[11.5px]"
      style={{ border: "1px solid var(--warn)", background: "var(--warn-dim)" }}
    >
      <Badge tone="warning">UNAVAILABLE</Badge>{" "}
      <span style={{ color: "var(--text-muted)" }}>{reason}</span>
      {children}
    </div>
  );
}

/**
 * Provenance on EVERY result: provider, model/version, cache hit and license
 * commercial-use. A cached run is visibly marked: the backend records no event
 * and no cost for a cache hit, so an unmarked cache hit would read as fresh
 * work that never happened.
 */
export function Provenance({
  run,
  cacheHit,
  provider,
  license,
  modelVersion,
  qc,
  extra,
}: {
  run?: IntelRun | null;
  cacheHit?: boolean;
  provider?: string | null;
  license?: { commercial_use?: string; code_license?: string; model_license?: string } | null;
  modelVersion?: string | null;
  qc?: QcResult | null;
  extra?: ReactNode;
}) {
  const providerKey = provider ?? run?.provider_key ?? "";
  const model = modelVersion ?? run?.model_version ?? "";
  const hit = cacheHit ?? run?.cache_hit ?? false;
  return (
    <div className="text-[11.5px] font-mono leading-relaxed" style={{ color: "var(--text-faint)" }}>
      <div className="flex flex-wrap items-center gap-1">
        {providerKey && <span title="provider">{providerKey}</span>}
        {model && <span title="model version">· {model}</span>}
        {hit && <Badge tone="info">cache hit</Badge>}
        {!hit && run && <Badge tone="muted">fresh run</Badge>}
        {license?.commercial_use && (
          <Badge tone={license.commercial_use === "PERMITTED" ? "muted" : "warning"}>
            commercial: {license.commercial_use}
          </Badge>
        )}
        {run && <Badge tone={RUN_TONE[run.status] ?? "muted"}>{run.status}</Badge>}
        {qc && <QcVerdictBadge qc={qc} />}
      </div>
      {run && (run.warnings?.length ?? 0) > 0 && (
        <div className="mt-0.5" style={{ color: "var(--warn)" }}>
          ⚠ {run.warnings.join(" · ")}
        </div>
      )}
      {run?.error_code && (
        <div style={{ color: "var(--danger)" }}>
          error: {run.error_code}
        </div>
      )}
      {extra}
    </div>
  );
}

/** The QC verdict, plus whether applying still needs an override. */
export function QcVerdictBadge({ qc }: { qc: QcResult | null | undefined }) {
  if (!qc) return null;
  return (
    <span className="inline-flex items-center gap-1" title={`QC kind: ${qc.kind}`}>
      <Badge tone={VERDICT_TONE[qc.verdict] ?? "muted"}>QC {qc.verdict}</Badge>
      {qc.apply_requires_override && <Badge tone="error">override required</Badge>}
      {qc.override && <Badge tone="warning">overridden</Badge>}
    </span>
  );
}

/** An empty state that says what to do, distinct from an error. */
export function IntelEmpty({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="text-[12px] py-3" style={{ color: "var(--text-faint)" }}>
      <div>{title}</div>
      {hint && <div className="mt-0.5">{hint}</div>}
    </div>
  );
}

/** Read-only panels for a confirmed viewer, matching the collab pattern. */
export function ReadOnlyNote({ what }: { what: string }) {
  return (
    <div className="text-[11.5px] mt-2" style={{ color: "var(--text-muted)" }}>
      Read-only — your workspace role is <b>viewer</b>. {what} is performed by editors; you can still
      read every result, its provenance and its QC verdict below.
    </div>
  );
}

/** A labelled row for the compact inspector column. */
export function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-baseline gap-2 text-[12px]">
      <span className="shrink-0" style={{ color: "var(--text-faint)" }}>
        {label}
      </span>
      <span className="min-w-0 break-words" style={{ color: "var(--text-muted)" }}>
        {children}
      </span>
    </div>
  );
}

/** Section heading inside a panel. */
export function SubHead({ children, right }: { children: ReactNode; right?: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-2 mt-3 mb-1">
      <b className="text-[12.5px]">{children}</b>
      {right}
    </div>
  );
}

export function verdictTone(v: QcVerdict | string | undefined): string {
  return VERDICT_TONE[String(v)] ?? "muted";
}
