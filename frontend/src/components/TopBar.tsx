import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { wsApi } from "../lib/api";
import { getThemeSetting, setTheme, type ThemeSetting } from "../lib/theme";
import { StatusDot } from "./ui";

const THEME_ORDER: ThemeSetting[] = ["system", "light", "dark"];
const THEME_ICON: Record<ThemeSetting, string> = { system: "◐", light: "○", dark: "●" };
const THEME_LABEL: Record<ThemeSetting, string> = { system: "System", light: "Light", dark: "Dark" };

/** HUD status bar: always-visible autopilot readout with inline control.
    STOP lives on the Command Center; the topbar exposes PAUSE/RESUME
    for one-click supervision, plus the theme switch (system/light/dark). */
export default function TopBar({ onOpenPalette }: { onOpenPalette: () => void }) {
  const nav = useNavigate();
  const [st, setSt] = useState<{ state: string; cycles_completed: number; current_cycle?: any } | null>(null);
  const [alerts, setAlerts] = useState(0);
  const [mode, setMode] = useState("");
  const [busy, setBusy] = useState(false);
  const [theme, setThemeState] = useState<ThemeSetting>(() => getThemeSetting());
  const [tgLinked, setTgLinked] = useState<boolean | null>(null);

  useEffect(() => {
    const load = async () => {
      try { setSt(await wsApi.get("/autopilot/status")); } catch {}
      try {
        const acts = await wsApi.get("/activity/recent?limit=40");
        setAlerts((acts.items ?? []).filter((e: any) => e.level === "error" || e.kind === "review.required" || e.kind === "safety.autopause").length);
      } catch {}
    };
    load();
    fetch("/api/v1/system/readiness").then((r) => r.json()).then((m) => setMode(m.status === "ready" ? "READY" : "BLOCKED")).catch(() => {});
    wsApi.get("/telegram/status").then((s: any) => setTgLinked(Boolean(s.linked))).catch(() => setTgLinked(null));
    const t = setInterval(load, 6000);
    return () => clearInterval(t);
  }, []);

  async function act(a: string) {
    setBusy(true);
    try {
      if (a === "start") await wsApi.post("/autopilot/start", { mode: "CONTINUOUS" });
      else await wsApi.post(`/autopilot/${a}`);
      const s = await wsApi.get("/autopilot/status");
      setSt(s);
    } catch {} finally { setBusy(false); }
  }

  function cycleTheme() {
    const next = THEME_ORDER[(THEME_ORDER.indexOf(theme) + 1) % THEME_ORDER.length];
    setTheme(next);
    setThemeState(next);
  }

  const running = st?.state === "RUNNING" || st?.state === "STARTING";
  const tone = running ? "success" : st?.state === "PAUSED" ? "warning" : st?.state === "FAILED" ? "error" : "neutral";
  const stage = st?.current_cycle?.stage;

  return (
    <div className="sticky top-0 z-20 flex items-center gap-2 px-4 md:px-6 h-11 border-b"
         style={{ borderColor: "var(--border)", background: "var(--bg-panel)" }}>
      {/* Autopilot readout — always visible */}
      <button
        onClick={() => nav("/autopilot")}
        className="flex items-center gap-2 px-2.5 py-1.5 text-[11px] font-semibold tracking-wide border rounded-lg transition-colors hover:bg-[var(--accent-dim)]"
        style={{ borderColor: "var(--border-strong)" }}
        aria-label={`Autopilot ${st?.state ?? "idle"}, cycle ${st?.cycles_completed ?? 0}`}
      >
        <StatusDot tone={tone as any} pulse={running} />
        <span>AUTOPILOT</span>
        <span style={{ color: running ? "var(--accent)" : "var(--text-muted)" }}>{st?.state ?? "IDLE"}</span>
        <span style={{ color: "var(--text-faint)" }}>#{String(st?.cycles_completed ?? 0).padStart(3, "0")}</span>
        {stage && running && (
          <span className="hidden md:inline" style={{ color: "var(--accent)" }}>
            · {stage}
            <span className="cursor-blink">_</span>
          </span>
        )}
      </button>

      {running && (
        <button className="btn-outline !py-1 !px-2.5 text-xs" onClick={() => act("pause")} disabled={busy}>
          Pause
        </button>
      )}
      {st?.state === "PAUSED" && (
        <button className="btn-primary !py-1 !px-2.5 text-xs" onClick={() => act("resume")} disabled={busy}>
          Resume
        </button>
      )}

      {/* Readiness indicator */}
      {mode && (
        <button onClick={() => nav("/health")}
                className="badge hidden sm:inline-flex"
                style={mode === "READY"
                  ? { background: "var(--accent-dim)", color: "var(--accent)", border: "1px solid var(--accent)" }
                  : { background: "var(--warn-dim)", color: "var(--warn)", border: "1px solid var(--warn)" }}>
          {mode}
        </button>
      )}

      {/* Telegram remote-control indicator */}
      <button onClick={() => nav("/integrations")}
              className="hidden md:grid place-items-center w-8 h-8 rounded-lg text-[13px] transition-colors hover:bg-[var(--accent-dim)]"
              style={{ color: tgLinked ? "var(--accent)" : "var(--text-faint)" }}
              aria-label={tgLinked ? "Telegram connected — open Integrations" : "Telegram not connected — open Integrations"}
              title={tgLinked ? "Telegram: connected" : "Telegram: set up remote control"}>
        <span aria-hidden>✈</span>
      </button>

      <div className="flex-1" />

      {/* Command palette trigger */}
      <button
        onClick={onOpenPalette}
        className="hidden sm:flex items-center gap-2 px-3 py-1.5 text-[12px] border rounded-lg transition-colors hover:border-[var(--accent)]"
        style={{ borderColor: "var(--border-strong)", color: "var(--text-muted)" }}
        aria-label="Open command palette (Ctrl+K)"
      >
        <span>Search / commands</span>
        <kbd className="text-[10px] px-1.5 rounded-md border" style={{ borderColor: "var(--border)" }}>⌘K</kbd>
      </button>
      <button onClick={onOpenPalette} className="sm:hidden btn-outline !px-2 !py-1" aria-label="Search">⌕</button>

      {/* Notifications */}
      <button
        onClick={() => nav("/?panel=activity")}
        className="relative grid place-items-center w-8 h-8 rounded-[3px] hover:bg-[var(--accent-dim)] text-[13px]"
        aria-label={`Notifications${alerts ? `, ${alerts} need attention` : ""}`}
      >
        <span aria-hidden>◈</span>
        {alerts > 0 && (
          <span role="status" aria-atomic="true"
                className="absolute -top-0.5 -right-0.5 min-w-[16px] h-4 px-1 grid place-items-center rounded-full text-[9px] font-bold font-mono"
                style={{ background: "var(--danger)", color: "#140404" }}>
            {alerts > 9 ? "9+" : alerts}
          </span>
        )}
      </button>

      {/* Theme switch: system → light → dark */}
      <button
        onClick={cycleTheme}
        className="grid place-items-center w-8 h-8 rounded-lg hover:bg-[var(--accent-dim)] text-[13px] transition-colors"
        style={{ color: "var(--text-muted)" }}
        aria-label={`Theme: ${THEME_LABEL[theme]}. Click to change.`}
        title={`Theme: ${THEME_LABEL[theme]}`}
      >
        <span aria-hidden>{THEME_ICON[theme]}</span>
      </button>
    </div>
  );
}
