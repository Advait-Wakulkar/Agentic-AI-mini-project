"use client";

import { useState } from "react";
import { supabase } from "@/lib/supabase";

// Email + password sign-in through Supabase Auth. Use test addresses (e.g. patient1@example.com):
// the app gives every account a fictional patient alias and never asks for real names.
export default function Login({ title }: { title: string }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [mode, setMode] = useState<"in" | "up">("in");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: "error" | "info"; text: string } | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!supabase) return;
    if (!email.includes("@")) return setMessage({ kind: "error", text: "Enter an email address." });
    if (password.length < 6) return setMessage({ kind: "error", text: "Password must be at least 6 characters." });
    setBusy(true);
    setMessage(null);
    const { data, error } =
      mode === "in"
        ? await supabase.auth.signInWithPassword({ email, password })
        : await supabase.auth.signUp({ email, password });
    setBusy(false);
    if (error) return setMessage({ kind: "error", text: error.message });
    if (mode === "up" && !data.session) {
      setMessage({ kind: "info", text: "Account created. Check your email to confirm it, then sign in." });
      setMode("in");
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <form onSubmit={submit} className="w-full max-w-sm rounded-xl border border-line bg-panel p-6">
        <h1 className="text-lg font-semibold">{title}</h1>
        <p className="mt-1 text-xs text-muted">Bright Smile Dental (fictional office). Use a test email; no real details.</p>
        <label className="mt-5 block text-xs font-semibold text-muted" htmlFor="email">Email</label>
        <input id="email" type="email" value={email} onChange={(e) => { setEmail(e.target.value); setMessage(null); }}
          className="mt-1 w-full rounded-lg border border-line bg-panel px-3 py-2 text-sm outline-none focus:border-accent" />
        <label className="mt-3 block text-xs font-semibold text-muted" htmlFor="password">Password</label>
        <input id="password" type="password" value={password} onChange={(e) => { setPassword(e.target.value); setMessage(null); }}
          className="mt-1 w-full rounded-lg border border-line bg-panel px-3 py-2 text-sm outline-none focus:border-accent" />
        {message && (
          <p className={`mt-3 text-xs ${message.kind === "error" ? "text-bad" : "text-accent"}`}>{message.text}</p>
        )}
        <button disabled={busy} className="mt-4 w-full rounded-lg bg-accent px-3 py-2 text-sm font-semibold text-on-accent disabled:opacity-50">
          {busy ? "…" : mode === "in" ? "Sign in" : "Create account"}
        </button>
        <button type="button" onClick={() => { setMode(mode === "in" ? "up" : "in"); setMessage(null); }}
          className="mt-3 w-full text-xs text-accent hover:underline">
          {mode === "in" ? "No account? Create one" : "Have an account? Sign in"}
        </button>
      </form>
    </div>
  );
}
