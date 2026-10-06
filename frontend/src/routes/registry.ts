/* The ONE route registry.
 *
 * §3 of Work 16.5.1: no duplicated route definitions. Navigation, the router,
 * the command palette, the breadcrumb trail and the route smoke tests all read
 * this array. Adding a route in two places is how a nav item ends up pointing
 * at a 404.
 *
 * Each entry records what the router needs to be honest about state:
 *   `state`      REBUILT | LEGACY_BRIDGE  -- §14. The shell renders a visible
 *                 marker for LEGACY_BRIDGE so nobody mistakes an old screen for
 *                 a rebuilt one.
 *   `permission` the capability required, used to hide nav and to annotate the
 *                 route. RBAC is still enforced SERVER-side; this is affordance
 *                 only, never enforcement.
 */

import type { ReactNode } from "react";

export type RouteState = "REBUILT" | "LEGACY_BRIDGE";
export type RouteGroup =
  | "command"
  | "plan"
  | "build"
  | "library"
  | "reach"
  | "learn"
  | "operate";

export type AppRoute = {
  /** Router path, always absolute. */
  path: string;
  /** Human label. Also the command-palette search key. */
  label: string;
  /** Short label for a collapsed sidebar. */
  short?: string;
  group: RouteGroup;
  /** Capability string, or null when the route needs no specific capability. */
  permission: string | null;
  state: RouteState;
  /** One-line description used by the palette and by empty states. */
  description?: string;
  /** Keywords so the palette finds it by intent, not just by name. */
  keywords?: string[];
  /** Hide from the sidebar (still routable, still in the palette). */
  hidden?: boolean;
};

export const ROUTE_GROUPS: { id: RouteGroup; label: string }[] = [
  { id: "command", label: "Command" },
  { id: "plan", label: "Plan" },
  { id: "build", label: "Build" },
  { id: "library", label: "Library" },
  { id: "reach", label: "Reach" },
  { id: "learn", label: "Learn" },
  { id: "operate", label: "Operate" },
];

export const GROUP_LABEL: Record<RouteGroup, string> = Object.fromEntries(
  ROUTE_GROUPS.map((g) => [g.id, g.label]),
) as Record<RouteGroup, string>;

/**
 * The target information architecture.
 *
 * This phase (16.5.1) rebuilds command/plan/build/library for the core
 * workflows. The reach/learn/operate entries are marked LEGACY_BRIDGE because
 * they are Work 16.5.2 scope -- the shell must be able to TELL the truth about
 * which is which, which is exactly what `state` is for.
 */
export const ROUTES: AppRoute[] = [
  /* ---- command ---------------------------------------------------------- */
  { path: "/", label: "Command Center", short: "Home", group: "command",
    permission: null, state: "REBUILT",
    description: "What YMONEY is doing, what needs attention, what it costs" },

  /* ---- plan ------------------------------------------------------------- */
  { path: "/planner", label: "Planner", group: "plan",
    permission: "content.write", state: "REBUILT",
    description: "Opportunities with evidence, editorial calendar, autonomy",
    keywords: ["ideas", "opportunities", "schedule"] },
  { path: "/calendar", label: "Editorial Calendar", group: "plan",
    permission: "content.write", state: "REBUILT",
    description: "Day, week and month placement with capacity",
    keywords: ["schedule", "reschedule"] },

  /* ---- build ------------------------------------------------------------ */
  { path: "/projects", label: "Projects", group: "build",
    permission: "content.read", state: "REBUILT",
    description: "One workspace per project: research, script, scenes, timeline" },
  { path: "/projects/:contentId", label: "Project", group: "build",
    permission: "content.write", state: "REBUILT", hidden: true,
    description: "A single project across its whole lifecycle" },
  { path: "/studio", label: "Studio", group: "build",
    permission: "content.write", state: "REBUILT",
    description: "Timeline editor with assets, preview, inspector" },
  { path: "/studio/:timelineId", label: "Studio Editor", group: "build",
    permission: "content.write", state: "REBUILT", hidden: true,
    description: "Editing one timeline" },
  { path: "/campaigns", label: "Campaigns", group: "build",
    permission: "content.read", state: "REBUILT",
    description: "Master, derived shorts, platform variants, publications" },
  { path: "/campaigns/:campaignId", label: "Campaign Detail", group: "build",
    permission: "publish.approve", state: "REBUILT", hidden: true,
    description: "One campaign and everything derived from it" },

  /* ---- library ---------------------------------------------------------- */
  { path: "/assets", label: "Assets", group: "library",
    permission: "content.read", state: "REBUILT",
    description: "Video, image, audio, voice, music, generated and source media",
    keywords: ["media", "library", "files"] },
  { path: "/brands", label: "Brands", group: "library",
    permission: "brand.manage", state: "REBUILT",
    description: "BrandDNA, creative rules, voice, effective policy",
    keywords: ["branddna", "identity", "voice"] },

  /* ---- reach ------------------------------------------------------------ */
  { path: "/localization", label: "Localization", group: "reach",
    permission: "content.write", state: "REBUILT",
    description: "Locales, translation state, dubbing, lip-sync, QC",
    keywords: ["dubbing", "lipsync", "translate", "glossary"] },
  { path: "/ugc", label: "UGC", group: "reach",
    permission: "content.write", state: "REBUILT",
    description: "Format, script, talent, voice, timeline, QC, render",
    keywords: ["user generated", "talent", "avatar"] },
  { path: "/distribution", label: "Distribution", group: "reach",
    permission: "publish.approve", state: "REBUILT",
    description: "Accounts, readiness, variants, schedule, LIVE/MOCK/HANDOFF",
    keywords: ["publishing", "platforms", "accounts"] },
  { path: "/community", label: "Community", group: "reach",
    permission: "reviews.approve", state: "REBUILT",
    description: "Inbox, threads, moderation, questions, suggested replies",
    keywords: ["inbox", "comments", "moderation", "replies"] },

  /* ---- learn ------------------------------------------------------------ */
  { path: "/analytics", label: "Analytics", group: "learn",
    permission: "content.read", state: "REBUILT",
    description: "Overview, content, campaign, platform, retention, variants",
    keywords: ["metrics", "retention"] },
  { path: "/experiments", label: "Experiments", group: "learn",
    permission: "content.read", state: "REBUILT",
    description: "Hypothesis, arms, sample, confidence, result, lesson",
    keywords: ["ab", "test", "lessons", "hypothesis"] },
  { path: "/memory", label: "Memory", group: "learn",
    permission: "content.read", state: "REBUILT",
    description: "Provenance, freshness, conflicts, supersession, scope",
    keywords: ["knowledge", "evidence", "global memory"] },
  { path: "/intelligence", label: "Intelligence", group: "learn",
    permission: "operations.view", state: "REBUILT",
    description: "Decision records, routing, evidence, WHY, verifier, cost",
    keywords: ["decisions", "routing", "why"] },

  /* ---- operate ---------------------------------------------------------- */
  { path: "/operations", label: "Operations", group: "operate",
    permission: "operations.view", state: "REBUILT",
    description: "Workers, queues, GPU, renders, SLO, cost incidents, reconciliation",
    keywords: ["workers", "queue", "gpu", "alerts", "slo"] },
  { path: "/providers", label: "Providers", group: "operate",
    permission: "providers.manage", state: "REBUILT",
    description: "Canonical maturity, credentials, health, capabilities",
    keywords: ["integrations", "credentials", "maturity"] },
  { path: "/settings", label: "Settings", group: "operate",
    permission: "content.write", state: "REBUILT",
    description: "Workspace, members, autonomy, budgets, storage, security",
    keywords: ["keys", "webhooks", "configuration", "appearance"] },
];

/* ---- derived lookups ---------------------------------------------------- */

/** Sidebar entries: not hidden, in registry order. */
export const NAV_ROUTES: AppRoute[] = ROUTES.filter((r) => !r.hidden);

export const REBUILT_ROUTES: AppRoute[] = ROUTES.filter((r) => r.state === "REBUILT");
export const LEGACY_ROUTES: AppRoute[] = ROUTES.filter((r) => r.state === "LEGACY_BRIDGE");

/**
 * The registered route that owns a pathname.
 *
 * Matching is longest-registered-path-wins over a param-aware comparison:
 * `:contentId` matches one real segment. An earlier version compared only the
 * first path segment, which let `/projects/:contentId` answer for the bare list
 * path -- a detail screen's route shadowing the list is exactly the bug this
 * ordering exists to prevent.
 */
export function routeAt(pathname: string): AppRoute | undefined {
  const normalised = pathname.length > 1 ? pathname.replace(/\/$/, "") : pathname;
  const matches = (r: AppRoute): boolean => {
    if (r.path === "/") return normalised === "/";
    const pattern = r.path
      .split("/")
      .map((seg) => (seg.startsWith(":") ? "[^/]+" : seg.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")))
      .join("/");
    return new RegExp(`^${pattern}(/|$)`).test(normalised);
  };
  return [...ROUTES].filter(matches).sort((a, b) => b.path.length - a.path.length)[0];
}

/** Breadcrumb trail for a pathname, from the registry rather than by hand. */
export function breadcrumbsFor(pathname: string): { label: string; path?: string }[] {
  const route = routeAt(pathname);
  if (!route) return [{ label: "Command Center", path: "/" }];

  const segments = pathname.split("/").filter(Boolean);
  const crumbs: { label: string; path?: string }[] = [
    { label: "Command Center", path: "/" },
  ];
  if (segments.length === 0) return crumbs;

  // The deepest route that is a literal prefix of this path becomes the parent.
  const parent = [...ROUTES]
    .filter((r) => r.path !== "/" && r.path !== route.path && pathname.startsWith(r.path))
    .sort((a, b) => b.path.length - a.path.length)[0];

  if (parent) crumbs.push({ label: parent.label, path: parent.path });
  crumbs.push({ label: route.hidden ? route.label : route.label });
  return crumbs;
}

/** Palette search over label, description and keywords. */
export function searchRoutes(query: string, routes: AppRoute[] = NAV_ROUTES): AppRoute[] {
  const q = query.trim().toLowerCase();
  if (!q) return routes;
  return routes.filter((r) => {
    const hay = [r.label, r.description ?? "", r.path, ...(r.keywords ?? [])]
      .join(" ")
      .toLowerCase();
    return hay.includes(q);
  });
}

/** Summary used by the smoke test and shown in the shell footer. */
export function routeInventory(): {
  total: number;
  rebuilt: number;
  legacy: number;
  groups: number;
} {
  return {
    total: ROUTES.length,
    rebuilt: REBUILT_ROUTES.length,
    legacy: LEGACY_ROUTES.length,
    groups: ROUTE_GROUPS.length,
  };
}

/** A single nav glyph per route. Text, not emoji -- §3 forbids emoji icons. */
export const ROUTE_GLYPH: Record<string, ReactNode> = {
  "/": "◈",
  "/planner": "◇",
  "/calendar": "▤",
  "/projects": "▣",
  "/studio": "▦",
  "/campaigns": "◉",
  "/assets": "▩",
  "/brands": "◐",
  "/localization": "◎",
  "/ugc": "◑",
  "/distribution": "◇",
  "/community": "◌",
  "/analytics": "◫",
  "/experiments": "◬",
  "/memory": "◇",
  "/intelligence": "◭",
  "/operations": "◮",
  "/providers": "◯",
  "/settings": "◱",
};