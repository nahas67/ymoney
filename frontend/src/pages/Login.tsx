import { useEffect, useState } from "react";
import { api, setRefreshToken } from "../lib/api";

export default function Login({
  onAuthed,
}: {
  onAuthed: (token: string, ws: string) => void;
}) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  // --- Email/password login ---
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true); setError("");

    // Local email/password auth is the only supported sign-in method.
    try {
      const path = mode === "login" ? "/auth/login" : "/auth/register";
      const res = await api<any>("POST", path, { email, password });
      let wsId = res.workspace?.id ?? "";
      if (!wsId && mode === "login") {
        const me = await api<any>("GET", "/auth/me");
        if (me.workspaces?.length) wsId = me.workspaces[0].id;
      }
      if (!wsId) throw new Error("no workspace available");
      // Keep the rotating refresh token so sessions survive token expiry.
      if (res.refresh_token) setRefreshToken(res.refresh_token);
      onAuthed(res.access_token, wsId);
    } catch (err: any) {
      setError(err.message ?? "failed");
    } finally { setBusy(false); }
  }

  return (
    <div className="min-h-screen grid place-items-center px-4">
      <div className="w-full max-w-sm">
        <div className="text-center mb-8">
          <div
            className="inline-grid place-items-center w-12 h-12 rounded-[4px] text-2xl font-black mb-4"
            style={{
              background: "var(--accent)",
              color: "#04110b",
              boxShadow: "0 0 32px -6px var(--accent-glow)",
            }}
            aria-hidden
          >
            Y
          </div>
          <h1 className="text-2xl font-bold tracking-[0.12em]">YMONEY</h1>
          <p className="text-[11px] font-mono uppercase tracking-[0.2em] mt-2" style={{ color: "var(--text-muted)" }}>
            Autonomous content OS
          </p>
        </div>

        <form onSubmit={submit} className="card p-6 space-y-4">
          <div className="flex gap-1 p-1 rounded-[3px] border" style={{ background: "var(--bg-inset)", borderColor: "var(--border)" }} role="tablist">
            {["register", "login"].map((m) => (
              <button key={m} type="button" role="tab" aria-selected={mode === m}
                onClick={() => setMode(m as "login" | "register")}
                className={`flex-1 rounded-[2px] py-1.5 text-[12px] font-mono uppercase tracking-wider transition-colors ${
                  mode === m ? "font-semibold" : ""
                }`}
                style={mode === m
                  ? { background: "var(--accent-dim)", color: "var(--accent)", border: "1px solid var(--accent)" }
                  : { color: "var(--text-muted)", border: "1px solid transparent" }}>
                {m}
              </button>
            ))}
          </div>
          <div>
            <label htmlFor="email" className="block text-[10px] font-mono font-semibold uppercase tracking-[0.14em] mb-1.5" style={{ color: "var(--text-muted)" }}>
              Email
            </label>
            <input id="email" className="input" type="email" required autoComplete="email"
              value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@company.com" />
          </div>
          <div>
            <label htmlFor="password" className="block text-[10px] font-mono font-semibold uppercase tracking-[0.14em] mb-1.5" style={{ color: "var(--text-muted)" }}>
              Password {mode === "register" && <span className="opacity-60 normal-case tracking-normal">(min 10 chars)</span>}
            </label>
            <input id="password" className="input" type="password" required
              minLength={mode === "register" ? 10 : 1}
              autoComplete={mode === "register" ? "new-password" : "current-password"}
              value={password} onChange={(e) => setPassword(e.target.value)} />
          </div>
          {error && <p className="text-sm" style={{ color: "var(--danger)" }} role="alert">{error}</p>}
          <button className="btn-primary w-full !py-2.5" disabled={busy}>
            {busy ? "…" : mode === "login" ? "Sign in" : "Create account & workspace"}
          </button>
        </form>

        <p className="text-center text-[10px] font-mono mt-5 tracking-wider" style={{ color: "var(--text-faint)" }}>
          MOCK MODE · NOTHING TOUCHES REAL PLATFORMS
        </p>
      </div>
    </div>
  );
}
