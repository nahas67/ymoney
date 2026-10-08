/* Router.
 *
 * Reads the ONE registry in `routes/registry.ts`. A route defined here that is
 * not in the registry -- or vice versa -- fails the route smoke test, which is
 * how "a nav item points at a 404" stops being possible.
 *
 * Work 16.5.2: every product screen is rebuilt, so there are no legacy page
 * imports left. Routes are generated FROM the registry rather than listed by
 * hand, which is what makes the registry authoritative rather than decorative.
 */

import { Suspense, lazy, type ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { AppShell, LegacyBridge } from "./AppShell";
import { SessionProvider, useSession } from "../state/session";
import { Skeleton, EmptyState, Badge } from "../design-system/primitives";
import { LEGACY_ROUTES, NAV_ROUTES, REBUILT_ROUTES, ROUTES } from "../routes/registry";

/* Every feature module default-exports its screen. Default imports keep this
 * file uniform and make a screen that only offers named exports a compile error
 * here rather than a blank page at runtime.
 *
 * Every route is LAZY: one 990KB synchronous bundle is how the app shipped
 * before this rebuild, and the Studio editor in particular pulls the timeline
 * engine, motion panels and intel surfaces that no other route needs. `lazy`
 * splits one chunk per screen; the `<Suspense>` below already wraps `<Routes>`,
 * so a slow chunk shows a skeleton rather than a blank page. Login stays
 * explicitly lazy for the same reason it always was: an unauthenticated visitor
 * must not download the application. */

/* ---- command + plan ---------------------------------------------------- */
const CommandCenter = lazy(() => import("../features/command-center/CommandCenter"));
const Planner = lazy(() => import("../features/planner/Planner"));
const CalendarView = lazy(() => import("../features/planner/CalendarView"));

/* ---- create / studio / campaigns --------------------------------------- */
const Projects = lazy(() => import("../features/projects/Projects"));
const ProjectDetail = lazy(() => import("../features/projects/ProjectDetail"));
const Campaigns = lazy(() => import("../features/campaigns/Campaigns"));
const CampaignDetail = lazy(() => import("../features/campaigns/CampaignDetail"));
const Studio = lazy(() => import("../features/studio/Studio"));
const StudioEditor = lazy(() => import("../features/studio/StudioEditor"));

/* ---- library + brand ---------------------------------------------------- */
const Assets = lazy(() => import("../features/assets/Assets"));
const Brands = lazy(() => import("../features/brands/Brands"));

/* ---- reach -------------------------------------------------------------- */
const Localization = lazy(() => import("../features/localization/Localization"));
const Ugc = lazy(() => import("../features/ugc/Ugc"));
const Distribution = lazy(() => import("../features/distribution/Distribution"));
const Community = lazy(() => import("../features/community/Community"));

/* ---- learn -------------------------------------------------------------- */
const Analytics = lazy(() => import("../features/analytics/Analytics"));
const Experiments = lazy(() => import("../features/experiments/Experiments"));
const Memory = lazy(() => import("../features/memory/Memory"));
const Intelligence = lazy(() => import("../features/intelligence/Intelligence"));

/* ---- automation + operations + settings --------------------------------- */
const Operations = lazy(() => import("../features/operations/Operations"));
const Providers = lazy(() => import("../features/providers/Providers"));
const Settings = lazy(() => import("../features/settings/Settings"));
const Automation = lazy(() => import("../features/automation/Automation"));

/* Login is not a product screen: it stays lazy so an unauthenticated visitor
 * does not download the whole application. */
const Login = lazy(() => import("../pages/Login"));

/**
 * Registry path -> element.
 *
 * Keyed by the registry path, and the routes below are generated from ROUTES, so
 * a route added to the registry without a component here renders the bridge
 * below rather than silently disappearing.
 */
const COMPONENTS: Record<string, ReactNode> = {
  "/": <CommandCenter />,
  "/planner": <Planner />,
  "/calendar": <CalendarView />,
  "/projects": <Projects />,
  "/projects/:contentId": <ProjectDetail />,
  "/studio": <Studio />,
  "/studio/:timelineId": <StudioEditor />,
  "/campaigns": <Campaigns />,
  "/campaigns/:campaignId": <CampaignDetail />,
  "/assets": <Assets />,
  "/brands": <Brands />,
  "/localization": <Localization />,
  "/ugc": <Ugc />,
  "/distribution": <Distribution />,
  "/community": <Community />,
  "/analytics": <Analytics />,
  "/experiments": <Experiments />,
  "/memory": <Memory />,
  "/intelligence": <Intelligence />,
  "/operations": <Operations />,
  "/providers": <Providers />,
  "/automation": <Automation />,
  "/settings": <Settings />,
};

function Bridge({ path }: { path: string }) {
  const route = LEGACY_ROUTES.find((r) => r.path === path);
  return (
    <LegacyBridge title={route?.label ?? path}>
      <EmptyState
        title="Not rebuilt"
        description={`${path} is marked legacy but has no component.`}
      />
    </LegacyBridge>
  );
}

function Authenticated({ children }: { children: ReactNode }) {
  const { authenticated } = useSession();
  if (authenticated) return <>{children}</>;
  return (
    <Suspense fallback={<Skeleton rows={4} />}>
      <Login onAuthed={() => window.location.reload()} />
    </Suspense>
  );
}

export default function App() {
  return (
    <SessionProvider>
      <Authenticated>
        <AppShell>
          <Suspense fallback={<Skeleton rows={6} />}>
            <Routes>
              {ROUTES.map((r) => (
                <Route
                  key={r.path}
                  path={r.path}
                  element={COMPONENTS[r.path] ?? <Bridge path={r.path} />}
                />
              ))}

              {/* Pre-rebuild deep links keep working instead of 404ing. */}
              <Route path="/editor/:timelineId" element={<Navigate to="/studio" replace />} />
              <Route path="/command-center" element={<Navigate to="/" replace />} />

              <Route
                path="*"
                element={
                  <EmptyState
                    title="Page not found"
                    description="That route is not in the route registry."
                    action={<Badge tone="neutral">{NAV_ROUTES.length} routes registered</Badge>}
                  />
                }
              />
            </Routes>
          </Suspense>
        </AppShell>
      </Authenticated>
    </SessionProvider>
  );
}

export { REBUILT_ROUTES, NAV_ROUTES, LEGACY_ROUTES };