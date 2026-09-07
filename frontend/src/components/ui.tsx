import React, { createContext, useCallback, useContext, useEffect, useState } from "react";

/* ---------------------------------------------------------------------------
   YMONEY design-system primitives. Every screen composes from these so the
   product stays visually coherent and accessible.
--------------------------------------------------------------------------- */

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: string;
  actions?: React.ReactNode;
}) {
  return (
    <header className="flex items-start justify-between gap-4 flex-wrap">
      <div>
        <h1 className="text-xl font-bold tracking-tight">{title}</h1>
        {subtitle && <p className="text-[13px] mt-0.5" style={{ color: "var(--text-muted)" }}>{subtitle}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </header>
  );
}

export function Card({ children, className = "", pad = true }: any) {
  return <div className={`card ${pad ? "p-5" : ""} ${className}`}>{children}</div>;
}

type BadgeTone = "neutral" | "success" | "error" | "warning" | "info";
const badgeToneCss: Record<BadgeTone, React.CSSProperties> = {
  neutral: { background: "var(--bg-subtle)", color: "var(--text-muted)", border: "1px solid var(--border)" },
  success: { background: "var(--accent-dim)", color: "var(--accent)", border: "1px solid var(--accent)" },
  error: { background: "var(--danger-dim)", color: "var(--danger)", border: "1px solid var(--danger)" },
  warning: { background: "var(--warn-dim)", color: "var(--warn)", border: "1px solid var(--warn)" },
  info: { background: "var(--info-dim)", color: "var(--info)", border: "1px solid var(--info)" },
};

export function Badge({ tone = "neutral", children }: { tone?: BadgeTone; children: React.ReactNode }) {
  return <span className="badge" style={badgeToneCss[tone]}>{children}</span>;
}

export function StatusDot({ tone = "neutral", pulse }: { tone?: BadgeTone; pulse?: boolean }) {
  const colors: Record<BadgeTone, string> = {
    neutral: "var(--text-faint)", success: "var(--accent)", error: "var(--danger)",
    warning: "var(--warn)", info: "var(--info)",
  };
  return (
    <span className="relative inline-flex h-2 w-2 mr-1.5 align-middle">
      {pulse && <span className="animate-ping absolute h-full w-full rounded-full opacity-60" style={{ background: colors[tone] }} />}
      <span className="relative inline-flex rounded-full h-2 w-2" style={{ background: colors[tone] }} />
    </span>
  );
}

export function Tabs({ tabs, active, onChange }: {
  tabs: { key: string; label: string; count?: number }[];
  active: string;
  onChange: (k: string) => void;
}) {
  return (
    <div className="flex gap-1 flex-wrap" role="tablist">
      {tabs.map((t) => (
        <button
          key={t.key}
          role="tab"
          aria-selected={active === t.key}
          className={`tab ${active === t.key ? "active" : ""}`}
          onClick={() => onChange(t.key)}
        >
          {t.label}
          {t.count != null && (
            <span className="ml-1.5 text-[11px] opacity-60">{t.count}</span>
          )}
        </button>
      ))}
    </div>
  );
}

export function Modal({ open, onClose, children, wide }: any) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose?.();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div
      className="fixed inset-0 bg-black/60 backdrop-blur-[2px] grid place-items-center p-4 z-50"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
    >
      <div
        className={`card w-full ${wide ? "max-w-3xl" : "max-w-lg"} max-h-[88vh] overflow-y-auto p-6`}
        style={{ background: "var(--bg-panel)" }}
        onClick={(e) => e.stopPropagation()}
      >
        {children}
      </div>
    </div>
  );
}

export function EmptyState({ icon = "◌", title, hint, action }: {
  icon?: string; title: string; hint?: string; action?: React.ReactNode;
}) {
  return (
    <div className="py-14 text-center px-4">
      <div className="text-3xl mb-3 font-mono" style={{ color: "var(--text-faint)" }} aria-hidden>{icon}</div>
      <p className="font-medium">{title}</p>
      {hint && <p className="text-[13px] mt-1" style={{ color: "var(--text-muted)" }}>{hint}</p>}
      {action && <div className="mt-4 flex justify-center">{action}</div>}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <Card className="border-red-500/30">
      <div className="flex items-start gap-3 py-2">
        <span className="text-red-500 text-lg leading-none mt-0.5">⚠</span>
        <div className="flex-1">
          <p className="font-medium text-sm">Something went wrong</p>
          <p className="text-[13px] mt-0.5" style={{ color: "var(--text-muted)" }}>{message}</p>
        </div>
        {onRetry && (
          <button className="btn-outline shrink-0" onClick={onRetry}>Retry</button>
        )}
      </div>
    </Card>
  );
}

export function Skeleton({ rows = 3, height = 44 }: { rows?: number; height?: number }) {
  return (
    <div className="space-y-2.5" aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="skeleton" style={{ height }} />
      ))}
    </div>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="block text-xs font-medium mb-1.5" style={{ color: "var(--text-muted)" }}>{label}</span>
      {children}
      {hint && <span className="block text-[11px] mt-1" style={{ color: "var(--text-muted)" }}>{hint}</span>}
    </label>
  );
}

export function Toggle({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label?: string }) {
  return (
    <button
      role="switch"
      aria-checked={checked}
      aria-label={label ?? "toggle"}
      onClick={() => onChange(!checked)}
      className="relative w-9 h-5 rounded-full transition-colors shrink-0"
      style={checked
        ? { background: "var(--accent)", boxShadow: "0 0 10px -2px var(--accent-glow)" }
        : { background: "var(--border-strong)" }}
    >
      <span
        className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white shadow transition-transform ${checked ? "translate-x-4" : ""}`}
      />
    </button>
  );
}

/* ---------------------------------------------------------------------------
   Toasts
--------------------------------------------------------------------------- */
type Toast = { id: number; kind: "success" | "error" | "info"; message: string };
const ToastCtx = createContext<{ push: (kind: Toast["kind"], msg: string) => void }>({ push: () => {} });

export function useToast() {
  return useContext(ToastCtx);
}

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((kind: Toast["kind"], message: string) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t.slice(-4), { id, kind, message }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4200);
  }, []);
  return (
    <ToastCtx.Provider value={{ push }}>
      {children}
      <div className="fixed bottom-4 right-4 z-[100] space-y-2 max-w-sm" aria-live="polite">
        {toasts.map((t) => (
          <div
            key={t.id}
            className="card px-4 py-3 text-sm flex items-start gap-2.5"
            style={{
              background: "var(--bg-panel)",
              borderColor: t.kind === "error" ? "var(--danger)" : t.kind === "success" ? "var(--accent)" : "var(--border-strong)",
            }}
          >
            <StatusDot tone={t.kind === "error" ? "error" : t.kind === "success" ? "success" : "info"} />
            <span>{t.message}</span>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

/* ---------------------------------------------------------------------------
   Async section helper: loading / error / empty / data
--------------------------------------------------------------------------- */
export function AsyncSection<T>({
  data, error, loading, onRetry, empty, emptyHint, emptyAction, children,
}: {
  data: T | null | undefined;
  error: string | null;
  loading: boolean;
  onRetry?: () => void;
  empty?: string;
  emptyHint?: string;
  emptyAction?: React.ReactNode;
  children: (data: T) => React.ReactNode;
}) {
  if (loading && !data) return <Skeleton rows={4} />;
  if (error) return <ErrorState message={error} onRetry={onRetry} />;
  if (!data || (Array.isArray(data) && data.length === 0)) {
    return <EmptyState title={empty ?? "Nothing here yet"} hint={emptyHint} action={emptyAction} />;
  }
  return <>{children(data)}</>;
}

export function fmtNum(n: number | undefined | null): string {
  if (n == null) return "—";
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "k";
  return String(n);
}

export function fmtUsd(v: number | null | undefined): string {
  if (v == null) return "—";
  return `$${v.toFixed(v < 1 ? 4 : 2)}`;
}

export function fmtDate(iso: string | null | undefined, withTime = true): string {
  if (!iso) return "—";
  const d = new Date(iso.endsWith("Z") ? iso : iso + "Z");
  return withTime ? d.toLocaleString() : d.toLocaleDateString();
}
