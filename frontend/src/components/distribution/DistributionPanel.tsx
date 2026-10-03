/* Work 14 §12 — distribution panel: capability badges, the optimization
 * diff, platform-specific warnings, readiness, provider state, remote id and
 * the LIVE / MOCK / HANDOFF / UNAVAILABLE badge.
 *
 * Two rules this component exists to enforce in the UI:
 *
 * 1. A platform whose publish mode is USER_HANDOFF says so in plain language
 *    ("User handoff required") and is never rendered as a publish control.
 * 2. An UNDOCUMENTED constraint is shown as "unknown" — never as a number.
 *    The backend deliberately returns `value: "UNKNOWN", verified: false` for
 *    those, and this component renders that state rather than guessing.
 *
 * Everything is read-only. No token, no publish action, no UI-only state that
 * pretends to be a server decision.
 */

import { useEffect, useMemo, useState } from "react";

import { wsApi } from "../../lib/api";
import { Badge } from "../ui";

export type PublishMode = "DIRECT_PUBLISH" | "USER_HANDOFF" | string;

export interface CapabilityRow {
  platform: string;
  capabilities: string[];
  publish_mode: PublishMode;
  direct_publish: boolean;
  user_handoff: boolean;
  supports_inbox: boolean;
  supports_analytics: boolean;
  campaign_platforms: string[];
}

export interface PlatformRow {
  platform: string;
  media_types: string[];
  capabilities: string[];
  publish_mode: PublishMode;
  verified_limits: string[];
  unverified: string[];
  verified_notes: string[];
}

export interface LimitField {
  value: unknown;
  source: string;
  note: string;
  verified: boolean;
}

export interface OptimizationDecision {
  field: string;
  before: unknown;
  after: unknown;
  reason: string;
  source: string;
  priority: number;
}

export interface OptimizationResult {
  platform: string;
  spec: Record<string, any>;
  decisions: OptimizationDecision[];
  skipped: string[];
  blocked: string[];
  changed_fields: string[];
}

/** Human label for each priority level, matching the backend's order. */
const PRIORITY_LABEL: Record<string, string> = {
  platform_constraint: "platform limit",
  compliance: "compliance",
  brand_dna: "brand",
  campaign_override: "your override",
  learned: "learned",
};

/** Badges worth showing first, in the order an operator cares about. */
const BADGE_ORDER = [
  "DIRECT_PUBLISH",
  "USER_HANDOFF",
  "TEXT",
  "IMAGE",
  "VIDEO",
  "CAROUSEL",
  "REPLY",
  "LINK",
  "ALT_TEXT",
  "COMMENTS",
  "METRICS",
];

const TONE: Record<PublishMode, string> = {
  DIRECT_PUBLISH: "success",
  USER_HANDOFF: "warning",
};

async function call<T>(path: string, body?: unknown): Promise<T> {
  if (body === undefined) {
    return (wsApi.get as unknown as (p: string) => Promise<T>)(path);
  }
  return (wsApi.post as unknown as (p: string, b: unknown) => Promise<T>)(
    path,
    body
  );
}

/**
 * One platform's card.
 *
 * `publication` is the current LIVE/MOCK/HANDOFF/UNAVAILABLE state for a
 * PublishedPost, if the caller has one. It is rendered as-is and is never
 * derived from the capability list: a platform can be DIRECT_PUBLISH and a
 * given attempt still be HANDOFF.
 */
export default function DistributionPanel({
  platform,
  publication,
}: {
  platform: string;
  publication?: {
    publication_mode: string;
    remote_post_id?: string;
    remote_url?: string;
    handoff_payload?: Record<string, any> | null;
  } | null;
}) {
  const [rows, setRows] = useState<CapabilityRow[]>([]);
  const [detail, setDetail] = useState<Record<string, LimitField> | null>(null);
  const [notes, setNotes] = useState<string[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const data = await call<{ items: CapabilityRow[] }>(
          "/distribution/capabilities"
        );
        if (live) setRows(data.items);
      } catch (exc) {
        if (live) setError(String(exc));
      }
    })();
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => {
    let live = true;
    setDetail(null);
    setNotes([]);
    (async () => {
      try {
        const data = await call<Record<string, any>>(
          `/distribution/platforms/${platform}`
        );
        if (!live) return;
        const { verified_notes, ...fields } = data;
        setDetail(fields as Record<string, LimitField>);
        setNotes((verified_notes as string[]) ?? []);
      } catch (exc) {
        if (live) setError(String(exc));
      }
    })();
    return () => {
      live = false;
    };
  }, [platform]);

  const row = useMemo(
    () => rows.find((r) => r.platform === platform) ?? null,
    [rows, platform]
  );

  if (error) {
    return (
      <div className="text-[11.5px]" style={{ color: "var(--danger, #e5484d)" }}>
        {error}
      </div>
    );
  }

  const isHandoff = row?.publish_mode === "USER_HANDOFF";
  const mode = publication?.publication_mode ?? "";

  return (
    <div className="space-y-2 text-[11.5px]">
      <div className="flex flex-wrap items-center gap-1.5">
        {row
          ? BADGE_ORDER.filter((b) => row.capabilities.includes(b)).map((b) => (
              <Badge key={b} tone={TONE[b] ?? "muted"}>
                {b}
              </Badge>
            ))
          : null}
        {row?.supports_analytics ? <Badge tone="muted">METRICS</Badge> : null}
        {row?.supports_inbox ? <Badge tone="muted">COMMENTS</Badge> : null}
      </div>

      {isHandoff ? (
        <div
          className="rounded-lg px-2 py-1.5"
          style={{ border: "var(--seam)" }}
        >
          <Badge tone="warning">User handoff required</Badge>
          <div className="mt-1" style={{ color: "var(--text-faint)" }}>
            {publication?.handoff_payload?.instruction ??
              "This platform has no self-serve publishing API. The media is prepared and a human publishes it in the app — it is not published by YMONEY."}
          </div>
        </div>
      ) : null}

      {publication ? (
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge tone={mode === "LIVE" ? "success" : "warning"}>{mode}</Badge>
          {publication.remote_url ? (
            <a
              className="underline"
              href={publication.remote_url}
              target="_blank"
              rel="noreferrer"
            >
              remote link
            </a>
          ) : null}
          {publication.remote_post_id ? (
            <span className="font-mono" style={{ color: "var(--text-faint)" }}>
              id {publication.remote_post_id.slice(0, 18)}
            </span>
          ) : (
            <span style={{ color: "var(--text-faint)" }}>
              no remote id — nothing is live
            </span>
          )}
        </div>
      ) : null}

      {detail ? (
        <div className="space-y-1">
          {Object.entries(detail)
            .filter(([, v]) => v && typeof v === "object" && "verified" in v)
            .map(([field, v]) => (
              <div key={field} className="flex items-start justify-between gap-2">
                <span className="font-mono">{field}</span>
                {v.verified ? (
                  <span
                    className="text-right"
                    title={`${v.source}${v.note ? ` — ${v.note}` : ""}`}
                  >
                    {String(v.value)}
                  </span>
                ) : (
                  <span
                    className="text-right italic"
                    style={{ color: "var(--text-faint)" }}
                    title={v.note}
                  >
                    unknown
                  </span>
                )}
              </div>
            ))}
        </div>
      ) : null}

      {notes.length > 0 ? (
        <details>
          <summary style={{ color: "var(--text-faint)", cursor: "pointer" }}>
            verified platform notes ({notes.length})
          </summary>
          <ul
            className="mt-1 list-disc pl-4"
            style={{ color: "var(--text-faint)" }}
          >
            {notes.map((n, i) => (
              <li key={i}>{n}</li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  );
}

/**
 * The optimizer diff.
 *
 * Renders every decision with its priority so an operator can see WHY a
 * request was not honoured, plus the platform-specific warnings the backend
 * recorded. Blocked platforms are shown as a refusal, not as a diff.
 */
export function OptimizationDiff({
  result,
}: {
  result: OptimizationResult | null;
}) {
  if (!result) return null;
  if (result.blocked.length > 0) {
    return (
      <div
        className="rounded-lg px-2 py-1.5"
        style={{ border: "var(--seam)", color: "var(--danger, #e5484d)" }}
      >
        <Badge tone="danger">unsupported here</Badge>
        <ul className="mt-1 list-disc pl-4">
          {result.blocked.map((b, i) => (
            <li key={i}>{b}</li>
          ))}
        </ul>
      </div>
    );
  }
  return (
    <div className="space-y-1.5 text-[11.5px]">
      {result.decisions.length === 0 ? (
        <div style={{ color: "var(--text-faint)" }}>
          Already optimal for {result.platform}.
        </div>
      ) : null}
      {result.decisions.map((d, i) => (
        <div key={i} className="flex items-start justify-between gap-2">
          <span className="font-mono">{d.field}</span>
          <span className="text-right">
            <span style={{ color: "var(--text-faint)" }}>
              {String(d.before)}
            </span>
            {" → "}
            <strong>{String(d.after)}</strong>
            <span style={{ color: "var(--text-faint)" }}>
              {" "}
              ({PRIORITY_LABEL[d.source] ?? d.source})
            </span>
          </span>
        </div>
      ))}
      {result.skipped.length > 0 ? (
        <div style={{ color: "var(--text-faint)" }}>
          <div className="font-semibold">platform-specific warnings</div>
          <ul className="list-disc pl-4">
            {result.skipped.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
