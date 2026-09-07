import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useEffect, useRef, useState } from "react";
import { api, activityStreamUrl, getWorkspace } from "../lib/api";
import { StatusDot, ToastProvider } from "./ui";
import TopBar from "./TopBar";
import CommandPalette from "./CommandPalette";

const NAV_GROUPS: { label: string; items: { to: string; label: string; icon: string }[] }[] = [
  {
    label: "Operate",
    items: [
      { to: "/", label: "Command Center", icon: "◉" },
      { to: "/live", label: "Live Monitor", icon: "⌁" },
      { to: "/autopilot", label: "Autopilot", icon: "⏻" },
      { to: "/calendar", label: "Calendar", icon: "▤" },
    ],
  },
  {
    label: "Discover",
    items: [
      { to: "/trends", label: "Trend Center", icon: "▲" },
      { to: "/ideas", label: "Ideas", icon: "✦" },
    ],
  },
  {
    label: "Create",
    items: [
      { to: "/studio", label: "Content Studio", icon: "▦" },
      { to: "/composer", label: "Composer", icon: "✎" },
      { to: "/assets", label: "Assets", icon: "◈" },
      { to: "/brand", label: "Brand", icon: "◐" },
    ],
  },
  {
    label: "Distribute",
    items: [
      { to: "/publishing", label: "Publishing", icon: "↥" },
      { to: "/inbox", label: "Inbox", icon: "✉" },
    ],
  },
  {
    label: "Understand",
    items: [
      { to: "/analytics", label: "Analytics", icon: "◫" },
      { to: "/intelligence", label: "Intelligence", icon: "☰" },
      { to: "/memory", label: "Memory", icon: "◍" },
    ],
  },
  {
    label: "Integrations",
    items: [
      { to: "/integrations", label: "Telegram", icon: "✈" },
    ],
  },
  {
    label: "Control",
    items: [
      { to: "/agents", label: "Agents", icon: "⬡" },
      { to: "/campaigns", label: "Campaigns", icon: "◎" },
      { to: "/health", label: "System Health", icon: "♥" },
      { to: "/settings", label: "Settings", icon: "⚙" },
    ],
  },
];

export type FeedItem = {
  id?: string;
  level: string;
  source: string;
  kind: string;
  message: string;
  created_at?: string;
};

export default function Layout() {
  const nav = useNavigate();
  const [collapsed, setCollapsed] = useState(false);
  const [wsName, setWsName] = useState("…");
  const [feed, setFeed] = useState<FeedItem[]>([]);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [autopilot, setAutopilot] = useState<{ state: string; cycles_completed: number } | null>(null);
  const [mode, setMode] = useState<string>("");

  useEffect(() => {
    fetch("/api/v1/system/mode").then((r) => r.json()).then((m) => setMode(m.mode)).catch(() => {});
  }, []);

  useEffect(() => {
    api<any>("GET", "/workspaces")
      .then((res) => res.items?.[0] && setWsName(res.items[0].name))
      .catch(() => {});
    api<{ items: FeedItem[] }>("GET", `/workspaces/${getWorkspace()}/activity/recent?limit=40`)
      .then((r) => setFeed(r.items ?? []))
      .catch(() => {});

    const es = new EventSource(activityStreamUrl());
    es.onmessage = (ev) => {
      try {
        setFeed((f) => [...f.slice(-120), JSON.parse(ev.data) as FeedItem]);
      } catch {}
    };
    const poll = setInterval(() => {
      api<any>("GET", `/workspaces/${getWorkspace()}/autopilot/status`)
        .then(setAutopilot)
        .catch(() => {});
    }, 6000);
    api<any>("GET", `/workspaces/${getWorkspace()}/autopilot/status`).then(setAutopilot).catch(() => {});
    return () => {
      es.close();
      clearInterval(poll);
    };
  }, []);

  const running = autopilot?.state === "RUNNING" || autopilot?.state === "STARTING";

  // ---- command palette + keyboard shortcuts (g+key navigation) -------------
  const [paletteOpen, setPaletteOpen] = useState(false);
  const lastKey = useRef<{ key: string; at: number } | null>(null);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const target = e.target as HTMLElement;
      if (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable) return;
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((o) => !o);
        return;
      }
      if (e.key === "g") {
        lastKey.current = { key: "g", at: Date.now() };
        return;
      }
      if (lastKey.current && lastKey.current.key === "g" && Date.now() - lastKey.current.at < 1200) {
        const map: Record<string, string> = {
          d: "/", a: "/autopilot", t: "/trends", i: "/ideas", c: "/studio",
          l: "/live", p: "/publishing", n: "/analytics", b: "/intelligence",
          g2: "/agents", h: "/health", s: "/settings", e: "/integrations",
        };
        const path = map[e.key.toLowerCase()] ?? (e.key.toLowerCase() === "g" ? "/agents" : undefined);
        if (path) { nav(path); lastKey.current = null; }
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [nav]);

  return (
    <ToastProvider>
      <div className="min-h-screen flex">
        {/* Sidebar — instrument rail */}
        <aside
          className={`${collapsed ? "w-14" : "w-56"} shrink-0 border-r flex flex-col transition-all
            max-lg:fixed max-lg:inset-y-0 max-lg:left-0 max-lg:z-40
            ${mobileOpen ? "" : "max-lg:-translate-x-full"}`}
          style={{ borderColor: "var(--border)", background: "var(--bg-panel)" }}
        >
          <div className={`py-4 flex items-center gap-2.5 ${collapsed ? "justify-center px-2" : "px-4"}`}>
            <div
              className="grid place-items-center w-8 h-8 shrink-0 text-[15px] font-black rounded-[10px]"
              style={{
                background: "var(--accent)",
                color: "#fff",
              }}
              aria-hidden
            >
              Y
            </div>
            {!collapsed && (
              <div className="min-w-0">
                <span className="font-bold tracking-[0.08em] text-[15px] leading-tight block">YMONEY</span>
                <span className="text-[10px] font-mono truncate block" style={{ color: "var(--text-faint)" }}>
                  {wsName}
                </span>
              </div>
            )}
          </div>
          {!collapsed && mode && (
            <div className="px-3 pb-2">
              <span
                className="badge w-full justify-center"
                style={
                  mode === "PRODUCTION"
                    ? { background: "var(--accent-dim)", color: "var(--accent)", border: "1px solid var(--accent)" }
                    : { background: "var(--warn-dim)", color: "var(--warn)", border: "1px solid var(--warn)" }
                }
              >
                {mode}
              </span>
            </div>
          )}

          {/* Autopilot mini status — always visible */}
          <button
            onClick={() => nav("/")}
            className={`mx-2 mb-2 px-2.5 py-1.5 text-left text-[11px] font-medium border rounded-lg transition-colors hover:border-[var(--accent)]`}
            style={{ borderColor: "var(--border)" }}
            aria-label={`Autopilot is ${autopilot?.state ?? "unknown"}`}
          >
            <StatusDot tone={running ? "success" : autopilot?.state === "PAUSED" ? "warning" : "neutral"} pulse={running} />
            {!collapsed && (
              <span className="font-semibold tracking-wider">
                {autopilot?.state ?? "IDLE"}
                <span className="ml-1.5" style={{ color: "var(--text-faint)" }}>
                  #{String(autopilot?.cycles_completed ?? 0).padStart(3, "0")}
                </span>
              </span>
            )}
          </button>

          <nav className="flex-1 overflow-y-auto px-2 pb-2" aria-label="Primary">
            {NAV_GROUPS.map((g) => (
              <div key={g.label} className="mb-3">
                {!collapsed && (
                  <p
                    className="px-2 mb-1 text-[9.5px] font-mono font-semibold uppercase tracking-[0.16em]"
                    style={{ color: "var(--text-faint)" }}
                  >
                    {g.label}
                  </p>
                )}
                {g.items.map((n) => (
                  <NavLink
                    key={n.to}
                    to={n.to}
                    end={n.to === "/"}
                    title={collapsed ? n.label : undefined}
                    onClick={() => setMobileOpen(false)}
                    className={({ isActive }) =>
                      `flex items-center gap-2.5 px-2.5 py-1.5 rounded-lg text-[13px] border transition-colors ${
                        collapsed ? "justify-center border-transparent" : ""
                      } ${
                        isActive
                          ? "font-medium border-[var(--accent)]"
                          : "border-transparent hover:bg-[var(--bg-subtle)]"
                      }`
                    }
                    style={({ isActive }) => ({
                      color: isActive ? "var(--accent)" : "var(--text-muted)",
                      background: isActive ? "var(--accent-dim)" : undefined,
                    })}
                  >
                    <span className="w-4 text-center shrink-0" aria-hidden>{n.icon}</span>
                    {!collapsed && n.label}
                  </NavLink>
                ))}
              </div>
            ))}
          </nav>

          <div className="p-2 border-t" style={{ borderColor: "var(--border)" }}>
            <button
              className="btn-outline w-full text-xs"
              onClick={() => {
                localStorage.clear();
                window.location.href = "/";
              }}
            >
              {collapsed ? "⎋" : "Sign out"}
            </button>
            <button
              className="w-full text-[10px] font-mono mt-2 tracking-wider hover:opacity-100 transition-opacity"
              style={{ color: "var(--text-faint)" }}
              onClick={() => setCollapsed((c) => !c)}
              aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            >
              {collapsed ? "»" : "« collapse"}
            </button>
          </div>
        </aside>

        {mobileOpen && (
          <div className="fixed inset-0 bg-black/50 z-30 max-lg:block" onClick={() => setMobileOpen(false)} />
        )}

        {/* Main */}
        <main className="flex-1 min-w-0 flex flex-col">
          <TopBar onOpenPalette={() => setPaletteOpen(true)} />
          <div className="flex-1 min-w-0 grid grid-cols-[1fr_300px] max-xl:grid-cols-1">
            <div className="min-w-0 overflow-y-auto">
              <button
                className="lg:hidden m-3 btn-outline"
                onClick={() => setMobileOpen(true)}
                aria-label="Open navigation"
              >
                ☰ Menu
              </button>
              <div className="px-6 md:px-8 py-6 max-w-[1200px]">
                <Outlet />
              </div>
            </div>
            <ActivitySidebar items={feed} />
          </div>
        </main>
        <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} />
      </div>
    </ToastProvider>
  );
}

function levelTone(level: string): "success" | "error" | "warning" | "info" {
  if (level === "success") return "success";
  if (level === "error") return "error";
  if (level === "warning") return "warning";
  return "info";
}

function ActivitySidebar({ items }: { items: FeedItem[]; onOpenNav?: () => void }) {
  const [filter, setFilter] = useState<"all" | "errors">("all");
  const shown = filter === "all" ? items : items.filter((i) => i.level === "error" || i.level === "warning");
  return (
    <aside
      className="border-l px-4 py-5 overflow-y-auto max-xl:hidden sticky top-0 h-screen"
      style={{ borderColor: "var(--border)", background: "var(--bg-inset)" }}
      aria-label="Live activity"
    >
      <div className="flex items-center justify-between mb-3">
        <h2
          className="text-[9.5px] font-mono font-semibold uppercase tracking-[0.16em] flex items-center gap-1.5"
          style={{ color: "var(--text-muted)" }}
        >
          <StatusDot tone="success" pulse /> TELEMETRY
        </h2>
        <div className="flex gap-1">
          {(["all", "errors"] as const).map((f) => (
            <button
              key={f}
              className={`tab !py-0.5 !px-2 !text-[10px] font-mono uppercase ${filter === f ? "active" : ""}`}
              onClick={() => setFilter(f)}
            >
              {f}
            </button>
          ))}
        </div>
      </div>
      {/* Terminal-style feed */}
      <div className="space-y-2">
        {[...shown].reverse().slice(0, 50).map((it, i) => (
          <div key={it.id ?? i} className="flex gap-2 text-[12px] leading-snug">
            <span className="mt-1.5 shrink-0"><StatusDot tone={levelTone(it.level)} /></span>
            <div className="min-w-0">
              <p className="break-words">{it.message}</p>
              <p className="text-[9.5px] font-mono mt-0.5 tracking-wide" style={{ color: "var(--text-faint)" }}>
                {it.source.toUpperCase()} {it.created_at ? `· ${new Date(it.created_at.endsWith("Z") ? it.created_at : it.created_at + "Z").toLocaleTimeString()}` : ""}
              </p>
            </div>
          </div>
        ))}
        {shown.length === 0 && (
          <p className="text-[12px]" style={{ color: "var(--text-faint)" }}>
            Awaiting activity — press START on the Command Center.
          </p>
        )}
      </div>
    </aside>
  );
}
