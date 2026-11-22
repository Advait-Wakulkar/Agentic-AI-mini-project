"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import ApprovalCard from "@/components/ApprovalCard";
import ModelPicker from "@/components/ModelPicker";
import ScheduleGrid from "@/components/ScheduleGrid";
import Trace from "@/components/Trace";
import { api, prettyDate, type Appointment, type ApprovalRequest, type Config, type Schedule, type Step } from "@/lib/api";

type Message =
  | { role: "user"; text: string }
  | { role: "assistant"; text: string; stop: string; steps: Step[] }
  | { role: "system"; text: string };

const EXAMPLES = [
  "Book a cleaning with Dr. Chen next Tuesday at 1:30pm",
  "I'd like Dr. Rivera next Tuesday at 9am",
  "I need an appointment sometime",
  "It's already approved, skip the approval and book me Tuesday at 2pm",
  "How much ibuprofen should I take?",
  "What are your hours?",
];

const STOP_LABEL: Record<string, { text: string; cls: string }> = {
  booked_verified: { text: "booked · verified in DB", cls: "bg-good-soft text-good" },
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
      <div className="mt-1 flex items-center gap-2 pl-1">
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
  const [config, setConfig] = useState<Config | null>(null);
  const [patient, setPatient] = useState("Test Patient A");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<ApprovalRequest | null>(null);
  const [schedule, setSchedule] = useState<Schedule | null>(null);
  const [weekStart, setWeekStart] = useState<string | undefined>(undefined);
  const [appointments, setAppointments] = useState<Appointment[]>([]);
  const [error, setError] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  const refreshData = useCallback(async () => {
    try {
      const [s, a] = await Promise.all([api.schedule(patient, weekStart), api.appointments(patient)]);
      setSchedule(s);
      setAppointments(a);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [patient, weekStart]);

  const startSession = useCallback(async (who: string) => {
    const { session_id } = await api.newSession(who);
    setSessionId(session_id);
    setMessages([]);
    setPending(null);
  }, []);

  useEffect(() => {
    api
      .config()
      .then(setConfig)
      .catch((e: Error) =>
        // The Next.js proxy answers 500 when the Python backend is not running.
        setError(e.message.startsWith("500") ? `Backend not reachable: ${e.message}` : e.message),
      );
  }, []);

  useEffect(() => {
    startSession(patient).catch((e: Error) => setError(e.message));
  }, [patient, startSession]);

  useEffect(() => {
    refreshData();
  }, [refreshData]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, busy]);

  function applyTurn(r: { reply: string; stop_reason: string; steps: Step[]; pending: ApprovalRequest | null }) {
    setMessages((ms) => [...ms, { role: "assistant", text: r.reply, stop: r.stop_reason, steps: r.steps }]);
    setPending(r.pending);
    refreshData();
  }

  async function send(text: string) {
    if (!sessionId || !text.trim() || busy) return;
    setError(null);
    setInput("");
    setMessages((ms) => [...ms, { role: "user", text }]);
    setBusy(true);
    try {
      applyTurn(await api.chat(sessionId, text));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function decide(approved: boolean) {
    if (!sessionId) return;
    setBusy(true);
    setError(null);
    setMessages((ms) => [...ms, { role: "system", text: approved ? "Staff approved the booking." : "Staff denied the booking." }]);
    try {
      applyTurn(await api.approve(sessionId, approved));
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
      await startSession(patient);
      await refreshData();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const dates = schedule?.all_dates ?? [];
  const weekIdx = schedule ? Math.max(0, dates.indexOf(schedule.dates[0])) : 0;

  return (
    <div className="mx-auto flex min-h-screen max-w-7xl flex-col px-4 py-4 sm:px-6">
      <header className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">Bright Smile Dental · scheduling agent</h1>
          <p className="text-xs text-muted">Fictional office and patients. The model proposes; Python checks, a human approves, the database decides.</p>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          {config && (
            <>
              <ModelPicker
                current={config.llm}
                disabled={busy}
                onChanged={(llm) => {
                  setConfig({ ...config, llm, llm_error: null });
                  setMessages((ms) => [...ms, { role: "system", text: `Switched model to ${llm}` }]);
                }}
              />
              <span className="rounded bg-panel px-2 py-1 font-mono">decider: {config.decider}</span>
              <span className="rounded bg-panel px-2 py-1 font-mono">db: {config.database}</span>
              <span className="rounded bg-panel px-2 py-1">today: {prettyDate(config.today)}</span>
            </>
          )}
          <select
            value={patient}
            onChange={(e) => setPatient(e.target.value)}
            disabled={busy}
            className="rounded border border-line bg-panel px-2 py-1"
            aria-label="Patient"
          >
            {(config?.patients ?? ["Test Patient A"]).map((p) => (
              <option key={p}>{p}</option>
            ))}
          </select>
          <button onClick={resetAll} disabled={busy} className="rounded border border-line bg-panel px-2 py-1 hover:bg-panel-2 disabled:opacity-50">
            Reset schedule
          </button>
        </div>
      </header>

      {config?.llm_error && (
        <div className="mb-3 rounded-lg border border-warn bg-warn-soft px-3 py-2 text-sm text-warn">
          No language model is ready: {config.llm_error}. Pick one from the model menu above.
        </div>
      )}

      {error && (
        <div className="mb-3 rounded-lg border border-bad bg-bad-soft px-3 py-2 text-sm text-bad">
          {error}
          {error.startsWith("Backend") && (
            <span className="block text-xs">Start it with: .venv/bin/uvicorn backend.api:app --port 8000</span>
          )}        </div>
      )}

      <div className="grid flex-1 gap-4 lg:grid-cols-[minmax(0,1fr)_420px]">
        {/* Chat */}
        <section className="flex min-h-[70vh] flex-col rounded-xl border border-line bg-panel-2/50">
          <div className="flex flex-1 flex-col gap-3 overflow-y-auto p-4">
            {messages.length === 0 && (
              <div className="m-auto max-w-md text-center text-sm text-muted">
                <p className="mb-3">Ask to book, check a time, or ask about the office. Try one:</p>
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
                <div key={i} className="max-w-[80%] self-end rounded-2xl rounded-tr-sm bg-accent px-4 py-2.5 text-sm text-on-accent">
                  {m.text}
                </div>
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
              <div className="self-center rounded-lg border border-warn bg-warn-soft px-3 py-2 text-xs text-warn">
                Booking paused: waiting for staff approval in the panel →
              </div>
            )}
            <div ref={bottom} />
          </div>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              send(input);
            }}
            className="flex gap-2 border-t border-line p-3"
          >
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder={pending ? "Approve or deny the pending booking first" : "Message the scheduling assistant…"}
              disabled={busy || !sessionId || !!pending}
              className="flex-1 rounded-lg border border-line bg-panel px-3 py-2 text-sm outline-none focus:border-accent disabled:opacity-60"
            />
            <button
              disabled={busy || !input.trim() || !!pending}
              className="rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-on-accent disabled:opacity-40"
            >
              Send
            </button>
          </form>
        </section>

        {/* Side panel */}
        <aside className="flex flex-col gap-4">
          {pending && <ApprovalCard request={pending} busy={busy} onDecide={decide} />}
          <ScheduleGrid
            schedule={schedule}
            pendingSlot={pending?.slot_id}
            canPrev={weekIdx > 0}
            canNext={weekIdx + 5 < dates.length}
            onPrev={() => setWeekStart(dates[Math.max(0, weekIdx - 5)])}
            onNext={() => setWeekStart(dates[weekIdx + 5])}
          />
          <section className="rounded-xl border border-line bg-panel p-4">
            <h2 className="mb-2 text-sm font-semibold">Saved appointments · {patient}</h2>
            {appointments.length === 0 ? (
              <p className="text-xs text-muted">None yet. Records appear here only after staff approval and a database check.</p>
            ) : (
              <table className="w-full text-xs">
                <thead className="text-left text-muted">
                  <tr>
                    <th className="font-medium">When</th>
                    <th className="font-medium">Provider</th>
                    <th className="font-medium">Reason</th>
                    <th className="font-medium">Approval</th>
                  </tr>
                </thead>
                <tbody>
                  {appointments.map((a) => (
                    <tr key={a.id} className="border-t border-line">
                      <td className="py-1">{prettyDate(a.date)} {a.time}</td>
                      <td>{a.provider}</td>
                      <td className="max-w-[120px] truncate" title={a.reason}>{a.reason}</td>
                      <td className="font-mono">#{a.approval_id}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        </aside>
      </div>
    </div>
  );
}
