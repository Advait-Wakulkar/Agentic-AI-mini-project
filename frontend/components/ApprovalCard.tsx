"use client";

import { prettyDate, type ApprovalRequest } from "@/lib/api";

// The human-in-the-loop step. Only a person clicking here can approve a booking;
// the model has no endpoint and no button.
export default function ApprovalCard({
  request,
  busy,
  onDecide,
}: {
  request: ApprovalRequest;
  busy: boolean;
  onDecide: (approved: boolean) => void;
}) {
  return (
    <section className="rounded-xl border-2 border-warn bg-warn-soft p-4" aria-live="polite">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-warn">Staff approval needed</h2>
        <span className="rounded bg-panel px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-warn">human only</span>
      </div>
      <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
        <dt className="text-muted">Patient</dt>
        <dd>{request.patient_alias}</dd>
        <dt className="text-muted">When</dt>
        <dd className="font-medium">
          {prettyDate(request.date, { weekday: "long", month: "long", day: "numeric" })} at {request.time}
        </dd>
        <dt className="text-muted">Provider</dt>
        <dd>{request.provider}</dd>
        <dt className="text-muted">Reason</dt>
        <dd>{request.reason}</dd>
      </dl>
      {request.patient_request && (
        <p className="mt-3 rounded-lg bg-panel p-2 text-xs text-muted">
          Patient wrote: <span className="text-ink">“{request.patient_request}”</span>
        </p>
      )}
      <div className="mt-4 flex gap-2">
        <button
          disabled={busy}
          onClick={() => onDecide(true)}
          className="flex-1 rounded-lg bg-accent px-3 py-2 text-sm font-semibold text-white disabled:opacity-50 dark:text-black"
        >
          Approve
        </button>
        <button
          disabled={busy}
          onClick={() => onDecide(false)}
          className="flex-1 rounded-lg border border-line bg-panel px-3 py-2 text-sm font-semibold text-bad disabled:opacity-50"
        >
          Deny
        </button>
      </div>
      <p className="mt-2 text-[11px] text-muted">Nothing is saved until you approve. After saving, Python re-reads the database to verify.</p>
    </section>
  );
}
