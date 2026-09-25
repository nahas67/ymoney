import { useEffect, useState } from "react";
import { Navigate, Route, Routes, useNavigate } from "react-router-dom";
import { api, setAuth, setWorkspace } from "./lib/api";
import Login from "./pages/Login";
import Layout from "./components/Layout";
import CommandCenter from "./pages/CommandCenter";
import Autopilot from "./pages/Autopilot";
import Trends from "./pages/Trends";
import Studio from "./pages/Studio";
import ContentDetail from "./pages/ContentDetail";
import Editor from "./pages/Editor";
import LongForm from "./pages/LongForm";
import CalendarPage from "./pages/CalendarPage";
import Composer from "./pages/Composer";
import Publishing from "./pages/Publishing";
import Inbox from "./pages/Inbox";
import Analytics from "./pages/Analytics";
import Intelligence from "./pages/Intelligence";
import Memory from "./pages/Memory";
import Agents from "./pages/Agents";
import Approvals from "./pages/Approvals";
import ApiKeys from "./pages/ApiKeys";
import Webhooks from "./pages/Webhooks";
import Templates from "./pages/Templates";
import Campaigns from "./pages/Campaigns";
import Brand from "./pages/Brand";
import Assets from "./pages/Assets";
import SystemHealth from "./pages/SystemHealth";
import Settings from "./pages/Settings";
import Setup from "./pages/Setup";
import Integrations from "./pages/Integrations";
import LiveMonitor from "./pages/LiveMonitor";

function Ideas() {
  const nav = useNavigate();
  useEffect(() => {
    nav("/trends", { replace: true });
  }, [nav]);
  return null;
}

export default function App() {
  const [authed, setAuthed] = useState<boolean | null>(null);

  useEffect(() => {
    if (!localStorage.getItem("ym_token")) {
      setAuthed(false);
      return;
    }
    api("GET", "/auth/me")
      .then(() => setAuthed(true))
      .catch(() => {
        setAuth(null);
        setWorkspace(null);
        setAuthed(false);
      });
  }, []);

  if (authed === null) {
    return <div className="min-h-screen grid place-items-center" style={{ color: "var(--text-muted)" }}>Loading…</div>;
  }

  if (!authed) {
    return <Login onAuthed={() => setAuthed(true)} />;
  }

  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<CommandCenter />} />
        <Route path="autopilot" element={<Autopilot />} />
        <Route path="trends" element={<Trends />} />
        <Route path="ideas" element={<Ideas />} />
        <Route path="studio" element={<Studio />} />
        <Route path="studio/:contentId" element={<ContentDetail />} />
        <Route path="editor/:timelineId" element={<Editor />} />
        <Route path="longform" element={<LongForm />} />
        <Route path="calendar" element={<CalendarPage />} />
        <Route path="approvals" element={<Approvals />} />
        <Route path="composer" element={<Composer />} />
        <Route path="publishing" element={<Publishing />} />
        <Route path="inbox" element={<Inbox />} />
        <Route path="analytics" element={<Analytics />} />
        <Route path="intelligence" element={<Intelligence />} />
        <Route path="memory" element={<Memory />} />
        <Route path="agents" element={<Agents />} />
        <Route path="campaigns" element={<Campaigns />} />
        <Route path="campaigns/:id" element={<Campaigns />} />
        <Route path="brand" element={<Brand />} />
        <Route path="assets" element={<Assets />} />
        <Route path="health" element={<SystemHealth />} />
        <Route path="integrations" element={<Integrations />} />
        <Route path="live" element={<LiveMonitor />} />
        <Route path="setup" element={<Setup />} />
        <Route path="developers/keys" element={<ApiKeys />} />
        <Route path="developers/webhooks" element={<Webhooks />} />
        <Route path="developers/templates" element={<Templates />} />
        <Route path="settings" element={<Settings />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
