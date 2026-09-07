import React, { useEffect, useRef } from "react";

/* Lightweight, dependency-free SVG charts tuned for the YMONEY design system.
   Every chart answers one question; tooltips via <title>; color never the
   only signal (labels included). */

export function AreaChart({ data, height = 140, label = "value", format = (v: number) => String(v) }: {
  data: { x: string; y: number }[];
  height?: number;
  label?: string;
  format?: (v: number) => string;
}) {
  const w = 600;
  const pad = 6;
  if (!data.length || data.every((d) => d.y === 0)) {
    return (
      <div className="text-[12px] py-8 text-center" style={{ color: "var(--text-muted)" }}>
        No data yet — charts appear as metrics are collected.
      </div>
    );
  }
  const max = Math.max(...data.map((d) => d.y), 1);
  const stepX = data.length > 1 ? (w - pad * 2) / (data.length - 1) : 0;
  const pts = data.map((d, i) => [pad + i * stepX, height - 18 - ((d.y / max) * (height - 30))]);
  const line = pts.map((p, i) => `${i === 0 ? "M" : "L"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
  const area = `${line} L${pts[pts.length - 1][0].toFixed(1)},${height - 16} L${pad},${height - 16} Z`;
  return (
    <svg viewBox={`0 0 ${w} ${height}`} width="100%" height={height} role="img" aria-label={`${label} chart`}>
      <defs>
        <linearGradient id="areaFill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--accent)" stopOpacity="0.22" />
          <stop offset="100%" stopColor="var(--accent)" stopOpacity="0.02" />
        </linearGradient>
      </defs>
      {[0.5, 1].map((f) => (
        <line key={f} x1={pad} x2={w - pad} y1={(height - 18) * f + 2} y2={(height - 18) * f + 2}
              stroke="var(--border)" strokeDasharray="3 4" />
      ))}
      <path d={area} fill="url(#areaFill)" />
      <path d={line} fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
      {data.map((d, i) => (
        <g key={i}>
          <circle cx={pts[i][0]} cy={pts[i][1]} r="2.5" fill="var(--accent)">
            <title>{`${d.x}: ${format(d.y)} ${label}`}</title>
          </circle>
        </g>
      ))}
      {data.length > 1 && (
        <>
          <text x={pad} y={height - 4} fontSize="10" fill="var(--text-muted)">{data[0].x}</text>
          <text x={w - pad} y={height - 4} fontSize="10" fill="var(--text-muted)" textAnchor="end">
            {data[data.length - 1].x}
          </text>
        </>
      )}
    </svg>
  );
}

export function BarChart({ items, format = (v: number) => String(v) }: {
  items: { label: string; value: number; hint?: string }[];
  format?: (v: number) => string;
}) {
  const max = Math.max(...items.map((i) => i.value), 1);
  return (
    <div className="space-y-2.5" role="img" aria-label="bar chart">
      {items.map((it) => (
        <div key={it.label}>
          <div className="flex justify-between text-[12px] mb-1">
            <span>{it.label}{it.hint ? ` · ${it.hint}` : ""}</span>
            <span className="font-mono" style={{ color: "var(--text-muted)" }}>{format(it.value)}</span>
          </div>
          <div className="h-2 rounded-full overflow-hidden" style={{ background: "var(--bg-inset)" }}>
            <div
              className="h-full rounded-full transition-all duration-500"
              style={{
                width: `${(it.value / max) * 100}%`,
                background: "linear-gradient(90deg, var(--accent), var(--accent-bright))",
              }}
            />
          </div>
        </div>
      ))}
    </div>
  );
}

/** Subtle reveal-on-mount wrapper (respects reduced motion). */
export function Reveal({ children, delay = 0 }: { children: React.ReactNode; delay?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      el.style.opacity = "1";
      return;
    }
    el.style.opacity = "0";
    el.style.transform = "translateY(6px)";
    const t = setTimeout(() => {
      el.style.transition = "opacity 300ms ease, transform 300ms ease";
      el.style.opacity = "1";
      el.style.transform = "none";
    }, delay);
    return () => clearTimeout(t);
  }, [delay]);
  return <div ref={ref}>{children}</div>;
}
