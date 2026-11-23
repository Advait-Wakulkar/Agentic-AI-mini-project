"use client";

import { prettyDate, type ApprovalRequest } from "@/lib/api";

const ACTION_LABEL = { book: "Book", cancel: "Cancel", reschedule: "Reschedule" } as const;

// One request in the staff queue. Only a person clicking here can approve;
// the agents have no endpoint and no button.
export default function ApprovalCard({
  request,
  meta,
  busy,
  onDecide,
}: {
  request: ApprovalRequest;
  meta?: string;
  busy: boolean;
  onDecide: (approved: boolean) => void;
}) {
  const action = request.action ?? "book";
  const when = (d: string, t: string) => `${prettyDate(d, { weekday: "long", month: "long", day: "numeric" })} at ${t}`;
  return (
    <section className="rounded-xl border-2 border-warn bg-warn-soft p-4">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-warn">
          {ACTION_LABEL[action]} request · {request.patient_alias}
        </h2>
        {meta && <span className="rounded bg-panel px-2 py-0.5 text-[10px] text-muted">{meta}</span>}
      </div>
      <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
        {action === "reschedule" && request.from && (
          <>
            <dt className="text-muted">From</dt>
            <dd className="line-through decoration-bad/60">{when(request.from.date, request.from.time)} · {request.from.provider}</dd>
          </>
        )}
        <dt className="text-muted">{action === "cancel" ? "Cancel" : action === "reschedule" ? "To" : "When"}</dt>
        <dd className="font-medium">{when(request.date, request.time)} · {request.provider}</dd>
        <dt className="text-muted">Reason</dt>
        <dd>{request.reason}</dd>
      </dl>
      {request.patient_request && (
        <p className="mt-3 rounded-lg bg-panel p-2 text-xs text-muted">
          Patient wrote: <span className="text-ink">“{request.patient_request}”</span>
        </p>
      )}
      <div className="mt-4 flex gap-2">
        <button disabled={busy} onClick={() => onDecide(true)}
          className="flex-1 rounded-lg bg-accent px-3 py-2 text-sm font-semibold text-on-accent disabled:opacity-50">
          Approve
        </button>
        <button disabled={busy} onClick={() => onDecide(false)}
          className="flex-1 rounded-lg border border-line bg-panel px-3 py-2 text-sm font-semibold text-bad disabled:opacity-50">
          Deny
        </button>
      </div>
    </section>
  );
}
