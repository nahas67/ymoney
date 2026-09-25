import { useState } from "react";
import type { FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api, setAuth, setRefreshToken, setWorkspace } from "../lib/api";
import { Card, Field } from "../components/ui";

export default function Login({ onAuthed }: { onAuthed: () => void }) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const nav = useNavigate();

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const data = mode === "login"
        ? await api<any>("POST", "/auth/login", { email, password })
        : await api<any>("POST", "/auth/register", { email, password, display_name: name });
      setAuth(data.access_token);
      if (data.refresh_token) setRefreshToken(data.refresh_token);
      const wsId = data.workspace?.id ?? data.workspace_id;
      if (wsId) {
        setWorkspace(wsId);
        onAuthed();
        nav("/");
      } else {
        const ws = await api<any>("GET", "/workspaces");
        if (ws.items?.[0]) {
          setWorkspace(ws.items[0].id);
          onAuthed();
          nav("/");
        } else {
          onAuthed();
          nav("/setup");
        }
      }
    } catch (err: any) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="min-h-screen grid place-items-center p-4" style={{ background: "var(--bg)" }}>
      <Card className="w-full max-w-[400px]" style={{ padding: 28, boxShadow: "var(--pop-shadow)" }}>
        <div className="flex items-center gap-2.5 mb-1">
          <span className="grid place-items-center w-10 h-10 rounded-xl font-bold text-white text-[19px]"
            style={{ background: "linear-gradient(135deg, var(--accent-bright), var(--accent-deep))", boxShadow: "0 0 24px -4px var(--accent-glow)" }}>¥</span>
          <div>
            <b className="text-[19px] tracking-tight">YMONEY</b>
            <div className="text-[10.5px] font-medium" style={{ color: "var(--text-faint)" }}>autonomous studio</div>
          </div>
        </div>
        <p className="text-[13px] mb-5" style={{ color: "var(--text-muted)" }}>Autonomous short-form content OS. One niche, every platform.</p>
        <div className="flex gap-1.5 mb-5">
          {(["login", "register"] as const).map((m) => (
            <button key={m} className={`tab ${mode === m ? "active" : ""}`} onClick={() => setMode(m)}>
              {m === "login" ? "Sign in" : "Create account"}
            </button>
          ))}
        </div>
        <form onSubmit={submit}>
          {mode === "register" && (
            <Field label="Display name"><input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="Your name" /></Field>
          )}
          <Field label="Email"><input className="input" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@studio.com" /></Field>
          <Field label="Password"><input className="input" type="password" required minLength={10} value={password} onChange={(e) => setPassword(e.target.value)} placeholder="10+ characters" /></Field>
          {error && <div className="text-[13px] mb-3" style={{ color: "var(--danger)" }}>{error}</div>}
          <button className="btn-primary w-full" disabled={busy}>{busy ? "…" : mode === "login" ? "Sign in" : "Create account"}</button>
        </form>
      </Card>
    </div>
  );
}
