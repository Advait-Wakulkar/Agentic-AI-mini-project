"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import Login from "@/components/Login";
import ModelPicker from "@/components/ModelPicker";
import ScheduleGrid from "@/components/ScheduleGrid";
import Trace from "@/components/Trace";
import { useAuth } from "@/components/useAuth";
import { api, prettyDate, type ApprovalRequest, type Config, type Schedule, type Step, type TurnResult } from "@/lib/api";
import { supabase } from "@/lib/supabase";

type Message =
  | { role: "user"; text: string }
  | { role: "assistant"; text: string; stop: string; steps: Step[]; agent?: string }
  | { role: "system"; text: string };

type MyAppointment = { id: number; date: string; time: string; provider: string; reason: string };

const EXAMPLES = [
  "Book a cleaning with Dr. Chen next Tuesday at 1:30pm",
  "I'd like Dr. Rivera next Tuesday at 9am",
  "I need an appointment sometime",
  "What appointments do I have?",
  "Cancel my appointment",
  "Move my appointment to Wednesday at 10am",
  "What are your hours?",
  "How much ibuprofen should I take?",
];

const STOP_LABEL: Record<string, { text: string; cls: string }> = {
  booked_verified: { text: "booked · verified in DB", cls: "bg-good-soft text-good" },
  cancelled_verified: { text: "cancelled · verified in DB", cls: "bg-good-soft text-good" },
  rescheduled_verified: { text: "moved · verified in DB", cls: "bg-good-soft text-good" },
  replied: { text: "replied", cls: "bg-panel-2 text-muted" },
  awaiting_approval: { text: "waiting for staff", cls: "bg-warn-soft text-warn" },
  front_desk: { text: "routed to front desk", cls: "bg-panel-2 text-muted" },
  reply_corrected: { text: "reply corrected by guard", cls: "bg-bad-soft text-bad" },
  too_many_invalid: { text: "stopped: invalid proposals", cls: "bg-bad-soft text-bad" },
  max_steps: { text: "stopped: step limit", cls: "bg-bad-soft text-bad" },
  llm_error: { text: "model unavailable", cls: "bg-bad-soft text-bad" },
};

function AssistantBubble({ m }: { m: Extract<Message, { role: "assistant" }> }) {
  const [open, setOpen] = useState(false);
  const stop = STOP_LABEL[m.stop] ?? { text: m.stop, cls: "bg-panel-2 text-muted" };
  const toolCalls = m.steps.filter((s) => s.kind === "MODEL_PROPOSED").length;
  return (
    <div className="max-w-[85%] self-start">
      <div className="rounded-2xl rounded-tl-sm border border-line bg-panel px-4 py-2.5 text-sm leading-relaxed whitespace-pre-wrap">{m.text}</div>
      <div className="mt-1 flex flex-wrap items-center gap-2 pl-1">
        {m.agent && <span className="rounded bg-accent-soft px-1.5 py-0.5 text-[10px] font-semibold text-accent">{m.agent} agent</span>}
        <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${stop.cls}`}>{stop.text}</span>
        <button onClick={() => setOpen((o) => !o)} className="text-[11px] text-accent hover:underline">
          {open ? "Hide" : "Show"} agent trace ({toolCalls} model {toolCalls === 1 ? "decision" : "decisions"}, {m.steps.length} steps)
        </button>
      </div>
      {open && <Trace steps={m.steps} />}
    </div>
  );
}

export default function Home() {
  const auth = useAuth();
  const [config, setConfig] = useState<Config | null>(null);
  const [demoPatient, setDemoPatient] = useState("Test Patient A");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<ApprovalRequest | null>(null);
  const [schedule, setSchedule] = useState<Schedule | null>(null);
  const [weekStart, setWeekStart] = useState<string | undefined>(undefined);
  const [mine, setMine] = useState<MyAppointment[]>([]);
  const [mineSource, setMineSource] = useState<"rls" | "api">("api");
  const [error, setError] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  const ready = auth.ready && auth.signedIn;
  const signedInPatient = auth.me?.signed_in ? auth.me.patient : undefined;
  const patient = signedInPatient ?? demoPatient;
  const patientParam = auth.me?.signed_in ? undefined : demoPatient; // signed in: the server knows who you are

  const loadMine = useCallback(async () => {
    if (supabase && auth.me?.signed_in) {
      // Read straight from Supabase with the user's own token: Postgres Row Level Security
      // returns only this patient's rows. Our Python API is not involved.
      const { data, error: e } = await supabase
        .from("appointments")
        .select("id, reason, status, slots(date, start_time, providers(name))")
        .eq("status", "booked");
      if (e) throw new Error(e.message);
      type Row = { id: number; reason: string; slots: { date: string; start_time: string; providers: { name: string } } };
      setMine(((data ?? []) as unknown as Row[])
        .map((r) => ({ id: r.id, reason: r.reason, date: r.slots.date, time: r.slots.start_time, provider: r.slots.providers.name }))
        .sort((a, b) => (a.date + a.time).localeCompare(b.date + b.time)));
      setMineSource("rls");
    } else {
      const rows = await api.appointments(patientParam);
      setMine(rows.map((a) => ({ id: a.id, reason: a.reason, date: a.date, time: a.time, provider: a.provider })));
      setMineSource("api");
    }
  }, [auth.me, patientParam]);

  const refreshData = useCallback(async () => {
    try {
      const [s] = await Promise.all([api.schedule(patientParam, weekStart), loadMine()]);
      setSchedule(s);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [patientParam, weekStart, loadMine]);

  useEffect(() => {
    if (!ready) return;
    api.config().then(setConfig).catch((e: Error) =>
      setError(e.message.startsWith("500") ? `Backend not reachable: ${e.message}` : e.message));
  }, [ready]);

  useEffect(() => {
    if (!ready || !auth.me) return;
    api.newSession(patientParam ?? "")
      .then(({ session_id }) => { setSessionId(session_id); setMessages([]); setPending(null); })
      .catch((e: Error) => setError(e.message));
  }, [ready, auth.me, patientParam]);

  useEffect(() => {
    if (ready && auth.me) refreshData();
  }, [ready, auth.me, refreshData]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, busy]);

  const addTurn = useCallback((r: TurnResult) => {
    setMessages((ms) => [...ms, { role: "assistant", text: r.reply, stop: r.stop_reason, steps: r.steps, agent: r.agent }]);
    setPending(r.pending);
  }, []);

  // While a request waits for staff, poll for the decision; the agent's follow-up reply arrives here.
  useEffect(() => {
    if (!pending || !sessionId) return;
    const t = setInterval(async () => {
      try {
        const ev = await api.events(sessionId);
        if (ev.events.length) {
          setMessages((ms) => [...ms, { role: "system", text: "Staff made a decision." }]);
          ev.events.forEach(addTurn);
          refreshData();
        }
        setPending(ev.pending);
      } catch {
        /* keep polling */
      }
    }, 3000);
    return () => clearInterval(t);
  }, [pending, sessionId, addTurn, refreshData]);

  async function send(text: string) {
    if (!sessionId || !text.trim() || busy || pending) return;
    setError(null);
    setInput("");
    setMessages((ms) => [...ms, { role: "user", text }]);
    setBusy(true);
    try {
      addTurn(await api.chat(sessionId, text));
      refreshData();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function resetAll() {
    setBusy(true);
    try {
      await api.reset();
      setWeekStart(undefined);
      const { session_id } = await api.newSession(patientParam ?? "");
      setSessionId(session_id);
      setMessages([]);
      setPending(null);
      await refreshData();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (!auth.ready) return <div className="p-8 text-sm text-muted">Loading…</div>;
  if (auth.authConfigured && !auth.signedIn) return <Login title="Patient sign-in" />;

  const dates = schedule?.all_dates ?? [];
  const weekIdx = schedule ? Math.max(0, dates.indexOf(schedule.dates[0])) : 0;
  const isStaff = !!auth.me?.is_staff;

  return (
    <div className="mx-auto flex min-h-screen max-w-7xl flex-col px-4 py-4 sm:px-6">
      <header className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">Bright Smile Dental · scheduling agents</h1>
          <p className="text-xs text-muted">Fictional office and patients. Agents propose; Python checks; staff approve; the database decides.</p>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          {config && isStaff && (
            <ModelPicker current={config.llm} disabled={busy}
              onChanged={(llm) => {
                setConfig({ ...config, llm, llm_error: null });
                setMessages((ms) => [...ms, { role: "system", text: `Switched model to ${llm}` }]);
              }} />
          )}
          {config && <span className="rounded bg-panel px-2 py-1 font-mono">db: {config.database}</span>}
          {config && <span className="rounded bg-panel px-2 py-1">today: {prettyDate(config.today)}</span>}
          {auth.me?.signed_in ? (
            <span className="rounded bg-panel px-2 py-1" title={auth.me.email}>signed in as <b>{patient}</b></span>
          ) : (
            <select value={demoPatient} onChange={(e) => setDemoPatient(e.target.value)} disabled={busy}
              className="rounded border border-line bg-panel px-2 py-1" aria-label="Patient (demo mode)">
              {(config?.patients ?? ["Test Patient A"]).map((p) => <option key={p}>{p}</option>)}
            </select>
          )}
          {isStaff && (
            <Link href="/staff" className="rounded border border-line bg-panel px-2 py-1 hover:bg-panel-2">Staff dashboard</Link>
          )}
          {isStaff && (
            <button onClick={resetAll} disabled={busy} className="rounded border border-line bg-panel px-2 py-1 hover:bg-panel-2 disabled:opacity-50">
              Reset schedule
            </button>
          )}
          {auth.authConfigured && (
            <button onClick={auth.signOut} className="rounded border border-line bg-panel px-2 py-1 hover:bg-panel-2">Sign out</button>
          )}
        </div>
      </header>

      {config?.llm_error && (
        <div className="mb-3 rounded-lg border border-warn bg-warn-soft px-3 py-2 text-sm text-warn">
          No language model is ready: {config.llm_error}.
        </div>
      )}
      {(error || auth.error) && (
        <div className="mb-3 rounded-lg border border-bad bg-bad-soft px-3 py-2 text-sm text-bad">
          {error ?? auth.error}
          {error?.startsWith("Backend") && <span className="block text-xs">Start it with: .venv/bin/uvicorn backend.api:app --port 8000</span>}
        </div>
      )}

      <div className="grid flex-1 gap-4 lg:grid-cols-[minmax(0,1fr)_420px]">
        <section className="flex min-h-[70vh] flex-col rounded-xl border border-line bg-panel-2/50">
          <div className="flex flex-1 flex-col gap-3 overflow-y-auto p-4">
            {messages.length === 0 && (
              <div className="m-auto max-w-lg text-center text-sm text-muted">
                <p className="mb-3">Book, cancel or move an appointment, or ask about the office. Try one:</p>
                <div className="flex flex-wrap justify-center gap-2">
                  {EXAMPLES.map((ex) => (
                    <button key={ex} onClick={() => send(ex)} className="rounded-full border border-line bg-panel px-3 py-1 text-xs text-ink hover:border-accent">
                      {ex}
                    </button>
                  ))}
                </div>
              </div>
            )}
            {messages.map((m, i) =>
              m.role === "user" ? (
                <div key={i} className="max-w-[80%] self-end rounded-2xl rounded-tr-sm bg-accent px-4 py-2.5 text-sm text-on-accent">{m.text}</div>
              ) : m.role === "system" ? (
                <div key={i} className="self-center rounded-full bg-warn-soft px-3 py-1 text-xs text-warn">{m.text}</div>
              ) : (
                <AssistantBubble key={i} m={m} />
              ),
            )}
            {busy && (
              <div className="self-start rounded-2xl border border-line bg-panel px-4 py-2.5 text-sm text-muted">
                <span className="animate-pulse">Agent is working… local models can take a little while.</span>
              </div>
            )}
            {pending && !busy && (
              <div className="self-center rounded-lg border border-warn bg-warn-soft px-3 py-2 text-center text-xs text-warn">
                Waiting for a staff member to approve your {pending.action ?? "book"} request. This chat updates automatically.
                {isStaff && <> <Link href="/staff" className="underline">Open the staff dashboard</Link></>}
              </div>
            )}
            <div ref={bottom} />
          </div>
          <form onSubmit={(e) => { e.preventDefault(); send(input); }} className="flex gap-2 border-t border-line p-3">
            <input value={input} onChange={(e) => setInput(e.target.value)}
              placeholder={pending ? "Waiting for staff approval…" : "Message the front desk…"}
              disabled={busy || !sessionId || !!pending}
              className="flex-1 rounded-lg border border-line bg-panel px-3 py-2 text-sm outline-none focus:border-accent disabled:opacity-60" />
            <button disabled={busy || !input.trim() || !!pending}
              className="rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-on-accent disabled:opacity-40">Send</button>
          </form>
        </section>

        <aside className="flex flex-col gap-4">
          <section className="rounded-xl border border-line bg-panel p-4">
            <div className="mb-2 flex items-center justify-between">
              <h2 className="text-sm font-semibold">My appointments · {patient}</h2>
              <span className="rounded bg-panel-2 px-1.5 py-0.5 text-[10px] text-muted"
                title={mineSource === "rls" ? "Read directly from Supabase; Row Level Security returns only your rows" : "Read through the API"}>
                {mineSource === "rls" ? "via Supabase RLS" : "via API"}
              </span>
            </div>
            {mine.length === 0 ? (
              <p className="text-xs text-muted">None yet. Appointments appear here only after staff approval and a database check.</p>
            ) : (
              <ul className="divide-y divide-line text-xs">
                {mine.map((a) => (
                  <li key={a.id} className="flex justify-between gap-2 py-1.5">
                    <span><b>{prettyDate(a.date)} {a.time}</b> · {a.provider}</span>
                    <span className="truncate text-muted" title={a.reason}>#{a.id}</span>
                  </li>
                ))}
              </ul>
            )}
          </section>
          <ScheduleGrid schedule={schedule} pendingSlot={pending?.slot_id}
            canPrev={weekIdx > 0} canNext={weekIdx + 5 < dates.length}
            onPrev={() => setWeekStart(dates[Math.max(0, weekIdx - 5)])}
            onNext={() => setWeekStart(dates[weekIdx + 5])} />
        </aside>
      </div>
    </div>
  );
}
