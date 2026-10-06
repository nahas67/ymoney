/* Design-system primitives.
 *
 * One file, deliberately. The old `components/ui.tsx` was a 366-line god-file
 * that every page also re-declared variants of; splitting it by concern is
 * worth doing only once there is a second consumer per piece. The rule here is
 * that a primitive NEVER fetches, NEVER knows about a route, and NEVER
 * hardcodes a colour literal.
 */

import {
  forwardRef,
  useEffect,
  useId,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";
import type { QueryState } from "../api/queries";

const cx = (...parts: (string | false | null | undefined)[]) =>
  parts.filter(Boolean).join(" ");

/* ==========================================================================
 * Status vocabulary
 *
 * These maps exist so a backend enum can be rendered without every call site
 * inventing its own colour. Two rules they enforce:
 *
 *  1. An UNKNOWN value renders as `neutral`, never as a guess. Work 15.7-15.9
 *     added real enums (SUBMISSION_UNKNOWN, UNKNOWN_EXPOSURE, CONTRACT_TESTED,
 *     MANUAL_OVERRIDE); a screen that maps them to "warning" because it never
 *     heard of them is how a money incident gets mistaken for a hiccup.
 *
 *  2. LIVE / MOCK / HANDOFF never share a colour. They are different facts.
 * ======================================================================= */

export type Tone =
  | "neutral"
  | "success"
  | "warning"
  | "danger"
  | "info"
  | "unknown"
  | "live"
  | "mock"
  | "handoff";

export const TONE_CLASS: Record<Tone, string> = {
  neutral: "ym-tone-neutral",
  success: "ym-tone-success",
  warning: "ym-tone-warning",
  danger: "ym-tone-danger",
  info: "ym-tone-info",
  unknown: "ym-tone-unknown",
  live: "ym-tone-live",
  mock: "ym-tone-mock",
  handoff: "ym-tone-handoff",
};

/** Backend status string -> tone. Unknown strings fall back to `neutral`. */
export function toneForStatus(status: string | null | undefined): Tone {
  const s = (status ?? "").toUpperCase();
  if (!s) return "neutral";

  // Paid execution: ambiguity is its own category, never a plain warning.
  if (s.includes("SUBMISSION_UNKNOWN") || s.includes("UNKNOWN_EXPOSURE")) return "unknown";
  if (s.includes("RECONCIL") || s.includes("UNRECONCILABLE")) return "unknown";

  if (s.includes("SUCCEEDED") || s.includes("SUCCESS") || s.includes("COMPLETED")
      || s.includes("PUBLISHED") || s.includes("LIVE_VERIFIED") || s.includes("VERIFIED")
      || s === "OK" || s === "READY" || s === "HEALTHY" || s === "DONE") return "success";

  if (s.includes("FAILED") || s.includes("FAILURE") || s.includes("ERROR")
      || s.includes("DEAD") || s.includes("CRITICAL") || s.includes("BLOCKED")) return "danger";

  if (s.includes("RUNNING") || s.includes("PROCESSING") || s.includes("IN_PROGRESS")
      || s.includes("QUEUED") || s.includes("CLAIMED") || s.includes("PENDING")
      || s.includes("SCHEDULED") || s.includes("RENDERING")) return "info";

  if (s.includes("RETRY") || s.includes("DEGRADED") || s.includes("CONTRACT_TESTED")
      || s.includes("MOCK") || s.includes("PARTIAL")) return "warning";

  return "neutral";
}

/** Publication mode has its own vocabulary and must never be inferred. */
export function toneForMode(mode: string | null | undefined): Tone {
  const m = (mode ?? "").toUpperCase();
  if (m === "LIVE") return "live";
  if (m === "MOCK") return "mock";
  if (m === "HANDOFF") return "handoff";
  return "neutral";
}

export function humanize(value: string | null | undefined): string {
  if (!value) return "—";
  return value
    .replace(/[_-]+/g, " ")
    .toLowerCase()
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/* ==========================================================================
 * Layout
 * ======================================================================= */

export function Panel({
  title,
  subtitle,
  actions,
  children,
  className,
  dense,
  ...rest
}: {
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  dense?: boolean;
} & React.HTMLAttributes<HTMLElement>) {
  return (
    <section className={cx("ym-panel", className)} {...rest}>
      {(title || actions) && (
        <header className="ym-panel-head">
          <div className="ym-panel-heading">
            {title && <h2 className="ym-panel-title">{title}</h2>}
            {subtitle && <p className="ym-panel-subtitle">{subtitle}</p>}
          </div>
          {actions && <div className="ym-panel-actions">{actions}</div>}
        </header>
      )}
      <div className={dense ? "ym-panel-body ym-panel-body--dense" : "ym-panel-body"}>
        {children}
      </div>
    </section>
  );
}

export function PageHeader({
  title,
  description,
  actions,
  breadcrumb,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  breadcrumb?: ReactNode;
}) {
  return (
    <div className="ym-page-header">
      <div className="ym-page-header-text">
        {breadcrumb && <div className="ym-breadcrumb">{breadcrumb}</div>}
        <h1 className="ym-page-title">{title}</h1>
        {description && <p className="ym-page-description">{description}</p>}
      </div>
      {actions && <div className="ym-page-actions">{actions}</div>}
    </div>
  );
}

export function Grid({
  children,
  min = 260,
  gap = "md",
}: {
  children: ReactNode;
  min?: number;
  gap?: "sm" | "md" | "lg";
}) {
  return (
    <div
      className={cx("ym-grid", `ym-grid--${gap}`)}
      style={{ gridTemplateColumns: `repeat(auto-fit, minmax(${min}px, 1fr))` }}
    >
      {children}
    </div>
  );
}

/* ==========================================================================
 * Data display
 * ======================================================================= */

/**
 * A metric that shows its own provenance.
 *
 * `unavailable` is a first-class state, not an error and not a zero. The work
 * order forbids manufacturing metrics the providers do not report, so a tile
 * that has no source says UNAVAILABLE rather than 0.
 */
export function StatTile({
  label,
  value,
  unit,
  tone = "neutral",
  hint,
  unavailable = false,
  source,
}: {
  label: string;
  value?: ReactNode;
  unit?: string;
  tone?: Tone;
  hint?: ReactNode;
  unavailable?: boolean;
  /** Where the number came from, when it is not obvious. */
  source?: string;
}) {
  return (
    <div className={cx("ym-stat", TONE_CLASS[tone])}>
      <div className="ym-stat-label">{label}</div>
      {unavailable ? (
        <div className="ym-stat-value ym-stat-value--unavailable" title="No provider reports this metric">
          UNAVAILABLE
        </div>
      ) : (
        <div className="ym-stat-value">
          {value}
          {unit && <span className="ym-stat-unit">{unit}</span>}
        </div>
      )}
      {hint && <div className="ym-stat-hint">{hint}</div>}
      {source && <div className="ym-stat-source">{source}</div>}
    </div>
  );
}

export function Badge({
  children,
  tone = "neutral",
  title,
  dot,
}: {
  children: ReactNode;
  tone?: Tone;
  title?: string;
  dot?: boolean;
}) {
  return (
    <span className={cx("ym-badge", TONE_CLASS[tone])} title={title}>
      {dot && <span className="ym-badge-dot" aria-hidden="true" />}
      {children}
    </span>
  );
}

export function StatusBadge({ status }: { status: string | null | undefined }) {
  return (
    <Badge tone={toneForStatus(status)} dot>
      {humanize(status)}
    </Badge>
  );
}

export function ModeBadge({ mode }: { mode: string | null | undefined }) {
  const tone = toneForMode(mode);
  return (
    <Badge tone={tone} title={`Publication mode: ${mode ?? "unknown"}`}>
      {mode ? mode.toUpperCase() : "UNKNOWN"}
    </Badge>
  );
}

export type Column<T> = {
  key: string;
  header: ReactNode;
  /** Cell renderer. Returning null renders an em dash rather than blank. */
  cell: (row: T) => ReactNode;
  align?: "left" | "right" | "center";
  width?: string;
  /** Hide below this breakpoint to keep dense tables readable. */
  hideBelow?: "sm" | "md" | "lg";
};

/**
 * The dense professional table.
 *
 * Two behaviours are deliberate and tested: a numeric column right-aligns with
 * tabular figures (a column of money that does not line up is unreadable), and
 * an empty table says WHY it is empty rather than rendering headers over
 * nothing.
 */
export function DataTable<T>({
  rows,
  columns,
  rowKey,
  empty,
  emptyHint,
  onRowClick,
  caption,
  maxHeight,
}: {
  rows: T[];
  columns: Column<T>[];
  rowKey: (row: T, index: number) => string;
  empty: ReactNode;
  emptyHint?: ReactNode;
  onRowClick?: (row: T) => void;
  caption?: string;
  maxHeight?: number;
}) {
  if (rows.length === 0) {
    return (
      <div className="ym-table-empty" role="status">
        <div className="ym-table-empty-title">{empty}</div>
        {emptyHint && <div className="ym-table-empty-hint">{emptyHint}</div>}
      </div>
    );
  }
  return (
    <div className="ym-table-scroll" style={maxHeight ? { maxHeight, overflowY: "auto" } : undefined}>
      <table className="ym-table">
        {caption && <caption className="ym-sr-only">{caption}</caption>}
        <thead>
          <tr>
            {columns.map((c) => (
              <th
                key={c.key}
                scope="col"
                style={{ width: c.width, textAlign: c.align ?? "left" }}
                className={cx(c.hideBelow && `ym-hide-below-${c.hideBelow}`)}
              >
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr
              key={rowKey(row, i)}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              className={cx(onRowClick && "ym-row-clickable")}
              tabIndex={onRowClick ? 0 : undefined}
              onKeyDown={
                onRowClick
                  ? (e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        onRowClick(row);
                      }
                    }
                  : undefined
              }
            >
              {columns.map((c) => (
                <td
                  key={c.key}
                  style={{ textAlign: c.align ?? "left" }}
                  className={cx(
                    c.align === "right" && "ym-num",
                    c.hideBelow && `ym-hide-below-${c.hideBelow}`,
                  )}
                >
                  {c.cell(row) ?? <span className="ym-muted">—</span>}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Tabs. Keyboard-navigable, because §16 requires an accessibility baseline. */
export function Tabs({
  tabs,
  active,
  onChange,
}: {
  tabs: { id: string; label: ReactNode; count?: number; disabled?: boolean }[];
  active: string;
  onChange: (id: string) => void;
}) {
  const onKeyDown = (e: React.KeyboardEvent) => {
    const enabled = tabs.filter((t) => !t.disabled);
    const i = enabled.findIndex((t) => t.id === active);
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      const next = e.key === "ArrowRight" ? (i + 1) % enabled.length : (i - 1 + enabled.length) % enabled.length;
      onChange(enabled[next].id);
    }
  };
  return (
    <div className="ym-tabs" role="tablist" onKeyDown={onKeyDown}>
      {tabs.map((t) => (
        <button
          key={t.id}
          role="tab"
          type="button"
          aria-selected={t.id === active}
          disabled={t.disabled}
          className={cx("ym-tab", t.id === active && "ym-tab--active")}
          onClick={() => onChange(t.id)}
        >
          {t.label}
          {t.count !== undefined && <span className="ym-tab-count">{t.count}</span>}
        </button>
      ))}
    </div>
  );
}

/* ==========================================================================
 * Controls
 * ======================================================================= */

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "sm" | "md";
  loading?: boolean;
  icon?: ReactNode;
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", loading, icon, children, className, disabled, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type="button"
      className={cx("ym-btn", `ym-btn--${variant}`, `ym-btn--${size}`, className)}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading && <span className="ym-spinner" aria-hidden="true" />}
      {icon && !loading && <span className="ym-btn-icon" aria-hidden="true">{icon}</span>}
      {children}
    </button>
  );
});

/**
 * A destructive control that states the blast radius.
 *
 * §16 requires consistent destructive-action confirmation, and §14 forbids a
 * generic retry on paid work. `confirmLabel` is therefore required when
 * `destructive` is set: an operator must read what they are about to lose, not
 * just "are you sure?".
 */
export function DestructiveButton({
  confirmLabel,
  onConfirm,
  children,
  ...rest
}: { confirmLabel: string; onConfirm: () => void } & ButtonProps) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const t = window.setTimeout(() => setArmed(false), 6000);
    return () => window.clearTimeout(t);
  }, [armed]);

  if (!armed) {
    return (
      <Button variant="ghost" size="sm" onClick={() => setArmed(true)} {...rest}>
        {children}
      </Button>
    );
  }
  return (
    <span className="ym-confirm-inline" role="alertdialog" aria-label={confirmLabel}>
      <span className="ym-confirm-text">{confirmLabel}</span>
      <Button variant="danger" size="sm" onClick={() => { setArmed(false); onConfirm(); }}>
        Confirm
      </Button>
      <Button variant="ghost" size="sm" onClick={() => setArmed(false)}>
        Cancel
      </Button>
    </span>
  );
}

export const Field = forwardRef<
  HTMLInputElement,
  InputHTMLAttributes<HTMLInputElement> & { label?: ReactNode; hint?: ReactNode; error?: string }
>(function Field({ label, hint, error, id, className, ...rest }, ref) {
  const auto = useId();
  const fieldId = id ?? auto;
  return (
    <div className={cx("ym-field", error && "ym-field--error", className)}>
      {label && <label className="ym-label" htmlFor={fieldId}>{label}</label>}
      <input ref={ref} id={fieldId} className="ym-input" aria-invalid={error ? true : undefined}
             aria-describedby={error ? `${fieldId}-err` : undefined} {...rest} />
      {hint && !error && <p className="ym-hint">{hint}</p>}
      {error && <p className="ym-error" id={`${fieldId}-err`}>{error}</p>}
    </div>
  );
});

export const Select = forwardRef<
  HTMLSelectElement,
  SelectHTMLAttributes<HTMLSelectElement> & { label?: ReactNode }
>(function Select({ label, id, children, className, ...rest }, ref) {
  const auto = useId();
  const fieldId = id ?? auto;
  return (
    <div className={cx("ym-field", className)}>
      {label && <label className="ym-label" htmlFor={fieldId}>{label}</label>}
      <select ref={ref} id={fieldId} className="ym-select" {...rest}>
        {children}
      </select>
    </div>
  );
});

export const Textarea = forwardRef<
  HTMLTextAreaElement,
  TextareaHTMLAttributes<HTMLTextAreaElement> & { label?: ReactNode }
>(function Textarea({ label, id, className, ...rest }, ref) {
  const auto = useId();
  const fieldId = id ?? auto;
  return (
    <div className={cx("ym-field", className)}>
      {label && <label className="ym-label" htmlFor={fieldId}>{label}</label>}
      <textarea ref={ref} id={fieldId} className="ym-textarea" {...rest} />
    </div>
  );
});

export function Toggle({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: ReactNode;
  disabled?: boolean;
}) {
  return (
    <label className={cx("ym-toggle", disabled && "ym-toggle--disabled")}>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="ym-toggle-track" aria-hidden="true"><span className="ym-toggle-thumb" /></span>
      <span className="ym-toggle-label">{label}</span>
    </label>
  );
}

/* ==========================================================================
 * States: the part that makes §3 honest
 * ======================================================================= */

export function Skeleton({ rows = 3, height = 14 }: { rows?: number; height?: number }) {
  return (
    <div className="ym-skeleton" aria-hidden="true">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="ym-skeleton-row" style={{ height, width: `${100 - i * 7}%` }} />
      ))}
    </div>
  );
}

export function EmptyState({
  title,
  description,
  action,
  icon,
}: {
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  icon?: ReactNode;
}) {
  return (
    <div className="ym-empty" role="status">
      {icon && <div className="ym-empty-icon" aria-hidden="true">{icon}</div>}
      <div className="ym-empty-title">{title}</div>
      {description && <div className="ym-empty-desc">{description}</div>}
      {action && <div className="ym-empty-action">{action}</div>}
    </div>
  );
}

/**
 * Error surface that always offers a way forward.
 *
 * A dead end is a bug: an operator staring at "failed" with no retry and no
 * detail will escalate, and the underlying message is usually the only clue.
 */
export function ErrorState({
  title = "Could not load",
  message,
  onRetry,
  detail,
}: {
  title?: string;
  message: string;
  onRetry?: () => void;
  detail?: ReactNode;
}) {
  return (
    <div className="ym-error-state" role="alert">
      <div className="ym-error-title">{title}</div>
      <div className="ym-error-message">{message}</div>
      {detail}
      {onRetry && (
        <Button variant="secondary" size="sm" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}

/**
 * Renders the correct state for a query, so no screen has to remember.
 *
 * `refreshing` deliberately does NOT blank the data: a background poll that
 * wipes the screen every few seconds is unusable, and it is also how an
 * operator misses a value that just changed.
 */
/**
 * Distinguishes a PERMISSION REFUSAL from a generic failure (Work 16.5.3 §16).
 *
 * §16 is explicit: "403 != empty state", and a denied action must not read as
 * "the server is broken" or "there is no data". Before this, every denied read
 * on all twenty-two routes rendered through `ErrorState` with a Retry button --
 * which is worse than useless, because retrying a 403 can never succeed and the
 * button invites the operator to keep trying.
 *
 * So a 403/401 renders as its own state: a shield, the reason, and NO retry.
 * The backend stays authoritative; this only stops the UI from misreporting what
 * it already told us.
 */
export function PermissionAwareError({
  message,
  status,
  onRetry,
  stale = false,
}: {
  message: string;
  /** The HTTP status behind `message`, when the caller knows it. See `isPermissionDenial`. */
  status?: number | null;
  onRetry?: () => void;
  stale?: boolean;
}) {
  const denied = isPermissionDenial(message, status);

  if (denied) {
    // A 401 and a 403 are DIFFERENT problems and must not share one explanation.
    //
    // 403 = you are authenticated and your role is too low. Someone can grant it.
    // 401 = the credential itself is absent, expired or revoked. No role change
    //      would help, and telling a user with a dead session to "ask a workspace
    //      admin" sends them to the wrong person entirely.
    const unauthenticated = status === 401;
    return (
      <div className="ym-error-state ym-error-state--denied" role="alert">
        <div className="ym-error-title">
          {unauthenticated ? "Signed out" : "Permission denied"}
        </div>
        <div className="ym-error-message">
          {unauthenticated ? (
            <>
              Your session is no longer valid, so the server refused the request.
              Sign in again to continue.
            </>
          ) : (
            <>
              Your workspace role does not allow this. The server refused the
              request, so nothing is missing -- ask a workspace admin if you need
              it.
            </>
          )}
        </div>
        {/* The server's own words, not a paraphrase. It may say which role. */}
        <p className="ym-stale-note">{message}</p>
        {stale && (
          <p className="ym-stale-note">
            Showing the last good data — the refresh was refused, not the load.
          </p>
        )}
      </div>
    );
  }

  return (
    <ErrorState
      message={message}
      onRetry={onRetry}
      detail={
        stale ? (
          <p className="ym-stale-note">
            Showing the last good data — the refresh failed, not the load.
          </p>
        ) : null
      }
    />
  );
}

/**
 * Is this error a permission refusal?
 *
 * THE STATUS IS AUTHORITATIVE. `QueryState.errorStatus` carries the HTTP status
 * that produced the message, so a refusal is recognised as a refusal because the
 * server returned 401/403 -- not because its prose happened to match.
 *
 * That distinction is not pedantry. The previous version could only see
 * `ApiError.message`, which is the server's `detail` and contains no status at
 * all, so its `/\b40[13]\b/` branch was DEAD CODE. The wording branch carried the
 * whole load, and it was incomplete: of the seven distinct 403 details the backend
 * emits (`scripts/denial_vocabulary.py`), only "insufficient role" matched.
 *
 * The four that did not -- including "not a workspace member", the most common one
 * -- each rendered as a generic load failure WITH a Retry button that could never
 * succeed. That is the exact failure this component exists to prevent.
 *
 * The wording list below is retained ONLY as a fallback for call sites that pass a
 * bare string with no status, and it is written against the backend's measured
 * vocabulary rather than against English. New backend wording no longer needs a
 * frontend edit; this list is belt-and-braces.
 */
function isPermissionDenial(message: string, status?: number | null): boolean {
  // Authoritative path: the server said 401 or 403. Nothing else counts.
  if (typeof status === "number") return status === 401 || status === 403;

  // Fallback only, for string-only call sites. Keep this in sync with
  // `scripts/denial_vocabulary.py`, which regenerates it from the source.
  return (
    /\b40[13]\b/.test(message) ||
    /\b(forbidden|unauthorized)\b/i.test(message) ||
    /\binsufficient (project )?role\b/i.test(message) ||
    /\bnot a workspace member\b/i.test(message) ||
    /\bnot authenticated\b/i.test(message) ||
    /\binvalid (or (revoked|expired) )?(api key|credentials|refresh token|media link)\b/i.test(
      message,
    ) ||
    /\bapi key (required|not valid for this workspace)\b/i.test(message) ||
    /\b(requires the admin role|is disabled for this workspace)\b/i.test(message) ||
    /\bprivacy mode blocks\b/i.test(message)
  );
}

/** `QueryBoundary` (Work 16.5.3 §16). */
export function QueryBoundary<T>({
  query,
  children,
  skeletonRows,
  empty,
  emptyHint,
}: {
  query: QueryState<T>;
  children: (data: T) => ReactNode;
  skeletonRows?: number;
  empty?: ReactNode;
  emptyHint?: ReactNode;
}) {
  if (query.loading) return <Skeleton rows={skeletonRows} />;
  if (query.error) {
    return (
      <PermissionAwareError
        message={query.error}
        status={query.errorStatus}
        onRetry={query.reload}
        stale={query.data !== null}
      />
    );
  }
  if (query.data === null) return <Skeleton rows={skeletonRows} />;
  return (
    <>
      {query.refreshing && (
        <div className="ym-refresh-flag" role="status" aria-live="polite">
          <span className="ym-spinner ym-spinner--sm" aria-hidden="true" /> Refreshing
        </div>
      )}
      {children(query.data)}
    </>
  );
}

/* ==========================================================================
 * Money
 *
 * Never rendered by a bare toFixed. A money figure always carries its unit,
 * and an unknown figure says so rather than showing 0.
 * ======================================================================= */

export function Money({
  usd,
  precision = 4,
  tone,
}: {
  usd: number | null | undefined;
  precision?: number;
  tone?: Tone;
}) {
  if (usd === null || usd === undefined || Number.isNaN(usd)) {
    return <span className="ym-money ym-money--unknown" title="No amount reported">unknown</span>;
  }
  return (
    <span className={cx("ym-money", tone && TONE_CLASS[tone])}>
      ${usd.toFixed(precision)}
    </span>
  );
}

/* ==========================================================================
 * Modal
 * ======================================================================= */

export function Modal({
  open,
  onClose,
  title,
  children,
  footer,
  width = 560,
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  width?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    ref.current?.focus();
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="ym-modal-backdrop" onClick={onClose} role="presentation">
      <div
        ref={ref}
        className="ym-modal"
        style={{ maxWidth: width }}
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === "string" ? title : undefined}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
      >
        <header className="ym-modal-head">
          <h2 className="ym-modal-title">{title}</h2>
          <button className="ym-modal-close" onClick={onClose} aria-label="Close">×</button>
        </header>
        <div className="ym-modal-body">{children}</div>
        {footer && <footer className="ym-modal-foot">{footer}</footer>}
      </div>
    </div>
  );
}

export { cx };