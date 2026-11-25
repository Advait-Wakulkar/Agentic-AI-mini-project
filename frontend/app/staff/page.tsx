"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import ApprovalCard from "@/components/ApprovalCard";
import Login from "@/components/Login";
import { useAuth } from "@/components/useAuth";
import { api, prettyDate, type HistoryRow, type ReminderStatus, type StaffRequest } from "@/lib/api";

const OUTCOME_CLS = (o: string | null, decision: string) =>
  decision === "denied" ? "text-muted" : o === "verified" ? "text-good" : "text-bad";

export default function StaffPage() {
  const auth = useAuth();
  const [queue, setQueue] = useState<StaffRequest[]>([]);
  const [history, setHistory] = useState<HistoryRow[]>([]);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [rem, setRem] = useState<ReminderStatus | null>(null);
  const [running, setRunning] = useState(false);
  const allowed = auth.ready && auth.signedIn && auth.me?.is_staff;

  const refresh = useCallback(async () => {
    try {
      const [q, h, r] = await Promise.all([api.staffRequests("pending"), api.history(), api.reminderStatus()]);
      setQueue(q);
      setHistory(h);
      setRem(r);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    if (!allowed) return;
    refresh();
    const t = setInterval(refresh, 4000); // new requests from any chat appear without reloading
    return () => clearInterval(t);
  }, [allowed, refresh]);

  async function decide(r: StaffRequest, approved: boolean) {
    setBusyId(r.id);
    setNotice(null);
    try {
      const d = await api.decide(r.id, approved);
      setNotice(`Request #${r.id} ${d.status}${d.outcome ? ` · ${d.outcome}` : ""}`);
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusyId(null);
    }
  }

  async function runReminders() {
    setRunning(true);
    setNotice(null);
    try {
      const r = await api.runReminders();
      setNotice(`Reminder agent ran: ${r.jobs} job(s)${r.fallbacks ? `, ${r.fallbacks} used the template fallback` : ""}.`);
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRunning(false);
    }
  }

  if (!auth.ready) return <div className="p-8 text-sm text-muted">Loading…</div>;
  if (auth.authConfigured && !auth.signedIn) return <Login title="Staff sign-in" />;
  if (!auth.me?.is_staff)
    return (
      <div className="p-8 text-sm">
        <p>This dashboard is for staff. Your account ({auth.me?.email}) is not on the staff list.</p>
        <Link href="/" className="text-accent hover:underline">Back to patient chat</Link>
      </div>
    );

  return (
    <div className="mx-auto max-w-6xl px-4 py-4 sm:px-6">
      <header className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">Staff dashboard · approvals</h1>
          <p className="text-xs text-muted">Every book, cancel and reschedule request from every chat. Nothing changes until you decide.</p>
        </div>
        <div className="flex items-center gap-2 text-xs">
          <span className="rounded bg-panel px-2 py-1">{auth.me?.demo ? "demo mode" : auth.me?.email}</span>
          <Link href="/" className="rounded border border-line bg-panel px-2 py-1 hover:bg-panel-2">Patient chat</Link>
          {auth.authConfigured && (
            <button onClick={auth.signOut} className="rounded border border-line bg-panel px-2 py-1 hover:bg-panel-2">Sign out</button>
          )}
        </div>
      </header>

      {error && <div className="mb-3 rounded-lg border border-bad bg-bad-soft px-3 py-2 text-sm text-bad">{error}</div>}
      {notice && <div className="mb-3 rounded-lg border border-line bg-panel px-3 py-2 text-sm">{notice}</div>}

      <h2 className="mb-2 text-sm font-semibold">Pending ({queue.length})</h2>
      {queue.length === 0 ? (
        <p className="mb-6 rounded-xl border border-dashed border-line p-6 text-center text-sm text-muted">No requests waiting.</p>
      ) : (
        <div className="mb-6 grid gap-3 md:grid-cols-2">
          {queue.map((r) => (
            <ApprovalCard key={r.id} request={r.request} busy={busyId === r.id} onDecide={(ok) => decide(r, ok)}
              meta={`#${r.id} · ${r.created_at.slice(11, 16)} · ${r.chat_live ? "chat open" : "chat closed"}`} />
          ))}
        </div>
      )}

      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold">Reminder agent</h2>
        <span className="text-xs text-muted">
          {rem?.enabled ? `runs in the background every ${rem.interval_seconds}s` : "background worker off"} · reminders {rem?.window_hours ?? 48}h ahead ·
          email {rem?.smtp ? "via SMTP" : "simulated outbox"} · SMS simulated
          {rem?.last_run ? ` · last run ${rem.last_run.slice(11, 19)}` : ""}
        </span>
        <button onClick={runReminders} disabled={running}
          className="ml-auto rounded border border-line bg-panel px-2 py-1 text-xs hover:bg-panel-2 disabled:opacity-50">
          {running ? "Running…" : "Run now"}
        </button>
      </div>
      {rem?.last_error && <p className="mb-2 text-xs text-bad">Last background error: {rem.last_error}</p>}
      <div className="mb-6 max-h-72 overflow-auto rounded-xl border border-line bg-panel">
        <table className="w-full text-xs">
          <thead className="sticky top-0 bg-panel-2 text-left text-muted">
            <tr>{["Sent", "Patient", "Kind", "Channel", "To", "Message", "Status", "By"].map((h) => <th key={h} className="px-3 py-2 font-medium">{h}</th>)}</tr>
          </thead>
          <tbody>
            {(rem?.notifications.length ?? 0) === 0 && (
              <tr><td colSpan={8} className="px-3 py-4 text-center text-muted">Nothing sent yet.</td></tr>
            )}
            {rem?.notifications.map((n) => (
              <tr key={n.id} className="border-t border-line align-top">
                <td className="px-3 py-1.5">{n.created_at.replace("T", " ").slice(5, 16)}</td>
                <td className="px-3 py-1.5">{n.patient_alias}</td>
                <td className="px-3 py-1.5 capitalize">{n.kind}</td>
                <td className="px-3 py-1.5">{n.channel}{n.has_invite ? " + .ics" : ""}</td>
                <td className="px-3 py-1.5 font-mono">{n.to_address}</td>
                <td className="max-w-xs truncate px-3 py-1.5" title={n.body}>{n.subject ?? n.body}</td>
                <td className="px-3 py-1.5">{n.status}</td>
                <td className="px-3 py-1.5 font-mono">{n.sent_by}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h2 className="mb-2 text-sm font-semibold">History (audit log)</h2>
      <div className="overflow-x-auto rounded-xl border border-line bg-panel">
        <table className="w-full text-xs">
          <thead className="bg-panel-2 text-left text-muted">
            <tr>
              {["#", "Decided", "Action", "Patient", "Appointment", "Decision", "By", "Outcome (DB re-read)"].map((h) => (
                <th key={h} className="px-3 py-2 font-medium">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {history.length === 0 && (
              <tr><td colSpan={8} className="px-3 py-4 text-center text-muted">No decisions yet.</td></tr>
            )}
            {history.map((h) => (
              <tr key={h.id} className="border-t border-line">
                <td className="px-3 py-1.5 font-mono">{h.id}</td>
                <td className="px-3 py-1.5">{h.decided_at.replace("T", " ").slice(5, 16)}</td>
                <td className="px-3 py-1.5 capitalize">{h.action}</td>
                <td className="px-3 py-1.5">{h.patient_alias}</td>
                <td className="px-3 py-1.5">{h.date ? `${prettyDate(h.date)} ${h.time} · ${h.provider}` : "—"}</td>
                <td className={`px-3 py-1.5 font-semibold ${h.decision === "approved" ? "text-good" : "text-bad"}`}>{h.decision}</td>
                <td className="px-3 py-1.5 font-mono">{h.decided_by}</td>
                <td className={`px-3 py-1.5 ${OUTCOME_CLS(h.outcome, h.decision)}`}>{h.outcome ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
