import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { fmtAgo } from "../lib/format";

export { statusTone, lifecycleTone } from "../lib/format";

/* Shared UI primitives — every page composes from these. */

const tones: Record<string, { fg: string; bg: string }> = {
  success: { fg: "var(--accent)", bg: "var(--accent-dim)" },
  warning: { fg: "var(--warn)", bg: "var(--warn-dim)" },
  error: { fg: "var(--danger)", bg: "var(--danger-dim)" },
  info: { fg: "var(--info)", bg: "var(--info-dim)" },
  muted: { fg: "var(--text-muted)", bg: "var(--bg-subtle)" },
};

export function Badge({ tone = "muted", children }: { tone?: keyof typeof tones | string; children: ReactNode }) {
  const t = tones[tone] ?? tones.muted;
  return (
    <span className="badge" style={{ color: t.fg, background: t.bg }}>
      {children}
    </span>
  );
}

export function Card({ children, pad = true, className = "", style }: { children: ReactNode; pad?: boolean; className?: string; style?: any }) {
  return (
    <div className={`card ${className}`} style={{ padding: pad ? 18 : 0, ...style }}>
      {children}
    </div>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3 mb-5">
      <div>
        <h1>{title}</h1>
        {subtitle && <p className="text-[13px] mt-1" style={{ color: "var(--text-muted)" }}>{subtitle}</p>}
      </div>
      {actions && <div className="flex items-center gap-2 flex-wrap">{actions}</div>}
    </div>
  );
}

export function Stat({ label, value, hint, tone }: { label: string; value: ReactNode; hint?: string; tone?: string }) {
  return (
    <Card>
      <div className="panel-label mb-1">{label}</div>
      <div className="text-[22px] font-semibold tracking-tight" style={tone ? { color: tone } : undefined}>{value}</div>
      {hint && <div className="text-[12px] mt-1" style={{ color: "var(--text-muted)" }}>{hint}</div>}
    </Card>
  );
}

export function ScoreBar({ value, max = 100 }: { value: number; max?: number }) {
  const pct = Math.max(0, Math.min(100, (value / max) * 100));
  const color = pct >= 75 ? "var(--accent)" : pct >= 55 ? "var(--warn)" : "var(--danger)";
  return (
    <div className="flex items-center gap-2 min-w-[110px]">
      <div className="flex-1 h-[7px] rounded-full overflow-hidden" style={{ background: "var(--bg-subtle)" }}>
        <div className="h-full rounded-full" style={{ width: `${pct}%`, background: color }} />
      </div>
      <span className="text-[12px] font-semibold font-mono" style={{ color }}>{value.toFixed(0)}</span>
    </div>
  );
}

export function Tabs<T extends string>({ tabs, active, onChange }: { tabs: { key: T; label: string; count?: number }[]; active: T; onChange: (k: T) => void }) {
  return (
    <div className="flex gap-1 flex-wrap mb-4">
      {tabs.map((t) => (
        <button key={t.key} className={`tab ${active === t.key ? "active" : ""}`} onClick={() => onChange(t.key)}>
          {t.label}
          {t.count != null && <span className="ml-1.5 opacity-70 font-mono text-[11px]">{t.count}</span>}
        </button>
      ))}
    </div>
  );
}

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label className="block mb-3">
      <div className="text-[12px] font-medium mb-1.5" style={{ color: "var(--text-muted)" }}>{label}</div>
      {children}
      {hint && <div className="text-[11px] mt-1" style={{ color: "var(--text-faint)" }}>{hint}</div>}
    </label>
  );
}

export function Modal({ open, onClose, title, children, wide }: { open: boolean; onClose: () => void; title: string; children: ReactNode; wide?: boolean }) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ background: "rgba(0,0,0,0.5)" }} onClick={onClose}>
      <div
        className="card w-full overflow-hidden"
        style={{ maxWidth: wide ? 860 : 560, maxHeight: "88vh", display: "flex", flexDirection: "column", padding: 0 }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-5 py-3.5" style={{ borderBottom: "var(--seam)" }}>
          <div className="font-semibold text-[15px]">{title}</div>
          <button className="btn-ghost !px-2.5 !py-1 text-[13px]" onClick={onClose} aria-label="Close">✕</button>
        </div>
        <div className="px-5 py-4 overflow-y-auto">{children}</div>
      </div>
    </div>
  );
}

export function Empty({ title, hint, action }: { title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="text-center py-10 px-4">
      <div className="text-[15px] font-medium">{title}</div>
      {hint && <div className="text-[13px] mt-1.5 max-w-[420px] mx-auto" style={{ color: "var(--text-muted)" }}>{hint}</div>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function Loading({ rows = 3 }: { rows?: number }) {
  return (
    <div className="space-y-2.5 py-2">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="skeleton" style={{ height: 44 }} />
      ))}
    </div>
  );
}

export function ErrorBox({ error, onRetry }: { error: string; onRetry?: () => void }) {
  return (
    <Card style={{ borderColor: "var(--danger)" }}>
      <div className="text-[13.5px] font-medium" style={{ color: "var(--danger)" }}>Couldn't load this view</div>
      <div className="text-[12.5px] mt-1 font-mono break-words" style={{ color: "var(--text-muted)" }}>{error}</div>
      {onRetry && <button className="btn-outline !text-xs mt-3" onClick={onRetry}>Retry</button>}
    </Card>
  );
}

export function Section({ data, loading, error, onRetry, empty, emptyHint, children }: {
  data: any[] | null | undefined;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  empty: string;
  emptyHint?: string;
  children: (list: any[]) => ReactNode;
}) {
  if (loading && !data) return <Loading />;
  if (error && !data) return <ErrorBox error={error} onRetry={onRetry} />;
  const list = data ?? [];
  if (!list.length) return <Card><Empty title={empty} hint={emptyHint} action={<button className="btn-outline !text-xs" onClick={onRetry}>Refresh</button>} /></Card>;
  return <>{children(list)}</>;
}

export function WhyPanel({ why }: { why: any }) {
  if (!why) return null;
  return (
    <div className="rounded-xl p-4 text-[13px] space-y-2.5" style={{ background: "var(--bg-inset)", border: "var(--seam)" }}>
      <div className="flex items-center gap-2 flex-wrap">
        <Badge tone={why.action === "PRODUCE" ? "success" : why.action === "WAIT" ? "warning" : "muted"}>{why.action}</Badge>
        {why.score != null && <span className="font-mono font-semibold">{Number(why.score).toFixed(0)}/100</span>}
        {why.confidence != null && <span style={{ color: "var(--text-muted)" }}>confidence {Math.round(why.confidence * 100)}%</span>}
      </div>
      {(why.reasons ?? []).map((r: string, i: number) => (
        <div key={i}>• {r}</div>
      ))}
      {(why.factors ?? []).length > 0 && (
        <div className="pt-1 space-y-1">
          {why.factors.map((f: any, i: number) => (
            <div key={i} className="flex justify-between gap-3 font-mono text-[12px]">
              <span style={{ color: "var(--text-muted)" }}>{f.name}</span>
              <span>{f.value} <b style={{ color: f.contribution >= 0 ? "var(--accent)" : "var(--danger)" }}>{f.contribution >= 0 ? "+" : ""}{f.contribution}</b></span>
            </div>
          ))}
        </div>
      )}
      {(why.evidence ?? []).map((e: string, i: number) => (
        <div key={`e${i}`} className="text-[12px]" style={{ color: "var(--text-muted)" }}>↳ {e}</div>
      ))}
    </div>
  );
}

export function FeedList({ items, limit = 30 }: { items: { kind: string; message: string; level: string; created_at?: string }[]; limit?: number }) {  const levelColor = (l: string) => (l === "error" ? "var(--danger)" : l === "warning" ? "var(--warn)" : l === "success" ? "var(--accent)" : "var(--text-faint)");
  return (
    <div className="space-y-0 max-h-[420px] overflow-y-auto">
      {items.slice(-limit).reverse().map((e, i) => (
        <div key={i} className="flex gap-2.5 py-2 text-[12.5px]" style={{ borderBottom: "var(--seam)" }}>
          <span style={{ color: levelColor(e.level) }}>●</span>
          <div className="flex-1 min-w-0">
            <div className="break-words">{e.message}</div>
            <div className="font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>{e.kind} · {fmtAgo(e.created_at)}</div>
          </div>
        </div>
      ))}
      {!items.length && <div className="text-[12.5px] py-4 text-center" style={{ color: "var(--text-faint)" }}>No activity yet — press START.</div>}
    </div>
  );
}

/* ---- Toasts: imperative, no provider needed (mount <Toasts/> once in Layout) ---- */

export type ToastTone = "success" | "error" | "warning" | "info";

export function toast(message: string, tone: ToastTone = "info", title?: string) {
  window.dispatchEvent(
    new CustomEvent("ym-toast", {
      detail: { id: Math.random().toString(36).slice(2), message, tone, title },
    })
  );
}

const _toastDot: Record<ToastTone, string> = {
  success: "var(--accent)",
  error: "var(--danger)",
  warning: "var(--warn)",
  info: "var(--info)",
};

export function Toasts() {
  const [items, setItems] = useState<{ id: string; message: string; tone: ToastTone; title?: string }[]>([]);
  const [leaving, setLeaving] = useState<Set<string>>(new Set());
  useEffect(() => {
    const dismiss = (id: string) => {
      setLeaving((s) => new Set(s).add(id));
      setTimeout(() => {
        setItems((l) => l.filter((t) => t.id !== id));
        setLeaving((s) => {
          const n = new Set(s);
          n.delete(id);
          return n;
        });
      }, 180);
    };
    const on = (e: any) => {
      const t = e.detail;
      setItems((l) => [...l.slice(-4), t]);
      setTimeout(() => dismiss(t.id), 4200);
    };
    window.addEventListener("ym-toast", on);
    return () => window.removeEventListener("ym-toast", on);
  }, []);
  if (!items.length) return null;
  return (
    <div className="toasts">
      {items.map((t) => (
        <div key={t.id} className={`toast${leaving.has(t.id) ? " out" : ""}`} role="status">
          <span className="dot" style={{ background: _toastDot[t.tone] }} />
          <div className="min-w-0">
            {t.title && <div className="font-semibold text-[13px]">{t.title}</div>}
            <div className="break-words" style={{ color: "var(--text-muted)" }}>{t.message}</div>
          </div>
        </div>
      ))}
    </div>
  );
}

/* ---- CopyButton: copies text, flashes confirmation ---- */

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [ok, setOk] = useState(false);
  return (
    <button
      className="btn-ghost !text-xs !py-1"
      onClick={() => {
        navigator.clipboard?.writeText(text).then(
          () => {
            setOk(true);
            setTimeout(() => setOk(false), 1400);
          },
          () => toast("Copy failed — select the text manually", "warning")
        );
      }}
    >
      {ok ? "✓ Copied" : label}
    </button>
  );
}

/* ---- ConfirmButton: destructive actions need a second click ---- */

export function ConfirmButton({
  onConfirm,
  children,
  confirmText = "Sure?",
  className = "btn-danger !text-xs",
  disabled,
}: {
  onConfirm: () => void;
  children: ReactNode;
  confirmText?: string;
  className?: string;
  disabled?: boolean;
}) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 3200);
    return () => clearTimeout(t);
  }, [armed]);
  return (
    <button
      className={armed ? "btn-danger !text-xs" : className}
      disabled={disabled}
      onClick={() => {
        if (armed) {
          setArmed(false);
          onConfirm();
        } else setArmed(true);
      }}
    >
      {armed ? confirmText : children}
    </button>
  );
}

/* ---- Accordion: collapsible detail sections ---- */

export function Accordion({
  title,
  badge,
  children,
  defaultOpen = false,
}: {
  title: ReactNode;
  badge?: ReactNode;
  children: ReactNode;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="rounded-xl" style={{ border: "var(--seam)", background: "var(--bg-inset)" }}>
      <button className="w-full flex items-center gap-2 px-4 py-3 text-left text-[13px] font-semibold" onClick={() => setOpen(!open)}>
        <span style={{ color: "var(--text-faint)", transition: "transform 150ms", transform: open ? "rotate(90deg)" : undefined }}>▸</span>
        <span className="flex-1">{title}</span>
        {badge}
      </button>
      {open && <div className="px-4 pb-4">{children}</div>}
    </div>
  );
}

/* ---- SearchInput: filter field with icon + esc-to-clear ---- */

export function SearchInput({
  value,
  onChange,
  placeholder = "Search…",
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <div className="relative">
      <span className="absolute left-3 top-1/2 -translate-y-1/2 text-[13px]" style={{ color: "var(--text-faint)" }}>⌕</span>
      <input
        className="input !pl-8 !pr-8"
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => e.key === "Escape" && onChange("")}
      />
      {value && (
        <button className="absolute right-2.5 top-1/2 -translate-y-1/2 text-[13px]" style={{ color: "var(--text-faint)" }}
          onClick={() => onChange("")} aria-label="Clear search">✕</button>
      )}
    </div>
  );
}

/* ---- Progress: thin instrument bar ---- */

export function Progress({ value, max = 100 }: { value: number; max?: number }) {
  const pct = Math.max(0, Math.min(100, (value / max) * 100));
  return (
    <div className="progress">
      <div style={{ width: `${pct}%` }} />
    </div>
  );
}

/* ---- Avatar: initials medallion ---- */

export function Avatar({ name, size = 32 }: { name: string; size?: number }) {
  const initials = name
    .split(/[\s_@.-]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0]!.toUpperCase())
    .join("");
  return (
    <span className="avatar" style={{ width: size, height: size, fontSize: size * 0.38 }}>
      {initials || "•"}
    </span>
  );
}
