/* Small formatting + domain helpers shared by all pages. */

export function fmtDate(iso?: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export function fmtAgo(iso?: string | null): string {
  if (!iso) return "—";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function fmtUSD(n?: number | null): string {
  return `$${(n ?? 0).toFixed(4)}`;
}

export function fmtCompact(n?: number | null): string {
  const v = n ?? 0;
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000) return `${(v / 1_000).toFixed(1)}K`;
  return `${v}`;
}

export function statusTone(status?: string): "success" | "warning" | "error" | "info" | "muted" {
  const s = (status || "").toUpperCase();
  if (["PUBLISHED", "LEARNED", "READY", "COMPLETED", "COMPLETE", "FINISHED", "CONNECTED", "PASSED", "DONE", "APPROVED", "ACTIVE", "SUCCEEDED"].includes(s))
    return "success";
  if (["FAILED", "DEAD", "ERROR", "EXPIRED", "BLOCKED", "REJECTED"].includes(s)) return "error";
  if (["RUNNING", "RENDERING", "PROCESSING", "PUBLISHING", "QUEUED", "DISPATCHING", "RETRYING", "PAUSED", "IN_PROGRESS",
       "IN_REVIEW", "CHANGES_REQUESTED", "PENDING", "SCHEDULED", "STALE", "UNAVAILABLE"].includes(s))
    return "warning";
  if (["EMERGING", "RISING", "CREATE_NOW", "PRODUCE", "MOCK"].includes(s)) return "info";
  // Intentionally muted (neutral, not a problem): DRAFT, CANCELLED, ARCHIVED,
  // and anything unrecognized -- unknown tokens must never imply success.
  return "muted";
}

export function lifecycleTone(lc?: string): "success" | "warning" | "error" | "info" | "muted" {
  switch ((lc || "").toUpperCase()) {
    case "EMERGING":
      return "success";
    case "RISING":
      return "info";
    case "PEAK":
      return "warning";
    case "DECLINING":
      return "error";
    case "EVERGREEN":
      return "success";
    default:
      return "muted";
  }
}

export const PLATFORMS = ["youtube", "tiktok", "facebook", "instagram"] as const;

export function platformLabel(p: string): string {
  return { youtube: "YouTube", tiktok: "TikTok", facebook: "Facebook", instagram: "Instagram" }[p] ?? p;
}

export function scoreColor(v: number): string {
  if (v >= 75) return "var(--accent)";
  if (v >= 55) return "var(--warn)";
  return "var(--danger)";
}
