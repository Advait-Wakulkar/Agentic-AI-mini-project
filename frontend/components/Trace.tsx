"use client";

import type { Step } from "@/lib/api";

// Shows the agent loop for one turn: what the model proposed, what Python checked,
// what the tool returned, and why the loop stopped.

const KIND_STYLE: Record<string, { label: string; cls: string }> = {
  DECIDER: { label: "Decider", cls: "bg-panel-2 text-muted" },
  ROUTER: { label: "Router", cls: "bg-accent-soft text-accent" },
  HANDOFF: { label: "Hand-off", cls: "bg-accent-soft text-accent" },
  MODEL_PROPOSED: { label: "Model proposed", cls: "bg-accent-soft text-accent" },
  VALIDATION: { label: "Validation", cls: "bg-good-soft text-good" },
  TOOL_OBSERVATION: { label: "Observation", cls: "bg-panel-2 text-muted" },
  APPROVAL_REQUESTED: { label: "Approval requested", cls: "bg-warn-soft text-warn" },
  HUMAN_DECISION: { label: "Human decision", cls: "bg-warn-soft text-warn" },
  VERIFY: { label: "Verify in DB", cls: "bg-good-soft text-good" },
  OUTPUT_GUARD: { label: "Output guard", cls: "bg-good-soft text-good" },
  LLM_ERROR: { label: "LLM error", cls: "bg-bad-soft text-bad" },
  STOP: { label: "Stop", cls: "bg-panel-2 text-ink" },
};

function Json({ value }: { value: unknown }) {
  const text = JSON.stringify(value, null, 2);
  const short = text.length > 160;
  if (!short) return <pre className="mt-1 whitespace-pre-wrap break-all font-mono text-[11px] text-muted">{text}</pre>;
  return (
    <details className="mt-1">
      <summary className="cursor-pointer font-mono text-[11px] text-muted">{JSON.stringify(value).slice(0, 140)}…</summary>
      <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-all rounded bg-panel-2 p-2 font-mono text-[11px] text-muted">
        {text}
      </pre>
    </details>
  );
}

function StepBody({ s }: { s: Step }) {
  switch (s.kind) {
    case "DECIDER": {
      const d = s.decision as Record<string, unknown> & { route: { action: string; notes?: string[] } };
      return (
        <div className="text-xs text-muted">
          <span className="font-mono">{String(s.decider)}</span> · intent <b className="text-ink">{String(d.intent)}</b> · confidence{" "}
          {String(d.confidence)} · skip-approval {Number(d.skip_approval).toFixed(2)} · medical {Number(d.medical_advice).toFixed(2)} ·
          urgency {String(d.urgency)} → <b className="text-ink">{d.route.action}</b>
          {d.route.notes?.map((n) => (
            <div key={n} className="mt-0.5 italic">note: {n}</div>
          ))}
        </div>
      );
    }
    case "ROUTER":
      return <div className="text-xs">→ <b>{String(s.to_agent)} agent</b> <span className="text-muted">({String(s.reason)})</span></div>;
    case "HANDOFF":
      return <div className="text-xs"><b>{String(s.from_agent)}</b> agent handed off to <b>{String(s.to_agent)}</b> agent</div>;
    case "MODEL_PROPOSED": {
      const p = s.proposal as { action?: string; argument?: Record<string, unknown> } | null;
      if (!p) return <div className="font-mono text-xs text-bad">unparseable: {String(s.raw).slice(0, 200)}</div>;
      return (
        <div className="font-mono text-xs">
          <b>{p.action}</b>
          <span className="text-muted">({p.argument ? JSON.stringify(p.argument) : ""})</span>
        </div>
      );
    }
    case "VALIDATION":
      return s.passed ? (
        <div className="text-xs text-good">passed</div>
      ) : (
        <ul className="list-disc pl-4 text-xs text-bad">
          {(s.errors as string[]).map((e) => (
            <li key={e}>{e}</li>
          ))}
        </ul>
      );
    case "TOOL_OBSERVATION":
      return <Json value={s.observation} />;
    case "APPROVAL_REQUESTED": {
      const r = s.request as Record<string, string>;
      return (
        <div className="text-xs">
          <b>{r.action ?? "book"}</b> · {r.date} {r.time} · {r.provider} · queued as request #{String(s.request_id)}
        </div>
      );
    }
    case "HUMAN_DECISION":
      return (
        <div className="text-xs">
          {s.approved ? "approved" : "denied"} via <span className="font-mono">{String(s.channel)}</span> (approval #{String(s.approval_id)})
        </div>
      );
    case "VERIFY":
      return (
        <div className={`text-xs ${s.verified ? "text-good" : "text-bad"}`}>
          re-read database → verified = {String(s.verified)}
        </div>
      );
    case "OUTPUT_GUARD":
      return s.passed ? (
        <div className="text-xs text-good">reply is consistent with the database</div>
      ) : (
        <div className="text-xs text-bad">reply not supported by the database → {String(s.action ?? "sent back to the model")}</div>
      );
    case "LLM_ERROR":
      return <div className="text-xs text-bad">{String(s.error)}</div>;
    case "STOP":
      return <div className="font-mono text-xs">{String(s.stop_reason)}</div>;
    default:
      return <Json value={s} />;
  }
}

export default function Trace({ steps }: { steps: Step[] }) {
  return (
    <ol className="mt-2 space-y-1.5 border-l-2 border-line pl-3">
      {steps.map((s) => {
        const style = KIND_STYLE[s.kind] ?? { label: s.kind, cls: "bg-panel-2 text-muted" };
        const rejected = s.kind === "VALIDATION" && !s.passed;
        const failed = (s.kind === "VERIFY" && !s.verified) || (s.kind === "OUTPUT_GUARD" && !s.passed);
        const cls = rejected || failed ? "bg-bad-soft text-bad" : style.cls;
        return (
          <li key={`${s.step}-${s.kind}`}>
            <span className={`inline-block rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${cls}`}>
              {rejected ? "Rejected" : style.label}
            </span>
            <StepBody s={s} />
          </li>
        );
      })}
    </ol>
  );
}
