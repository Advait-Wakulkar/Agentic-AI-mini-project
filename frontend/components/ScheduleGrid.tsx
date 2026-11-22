"use client";

import { prettyDate, type Schedule, type Slot } from "@/lib/api";

const STATUS_CLS: Record<Slot["status"] | "pending", string> = {
  open: "bg-good-soft text-good border-good/30",
  booked: "bg-panel-2 text-muted/60 border-transparent line-through",
  mine: "bg-accent text-white border-accent dark:text-black",
  pending: "bg-warn-soft text-warn border-warn animate-pulse",
};

function initials(provider: string) {
  return provider.replace("Dr. ", "").slice(0, 1);
}

export default function ScheduleGrid({
  schedule,
  pendingSlot,
  onPrev,
  onNext,
  canPrev,
  canNext,
}: {
  schedule: Schedule | null;
  pendingSlot?: string;
  onPrev: () => void;
  onNext: () => void;
  canPrev: boolean;
  canNext: boolean;
}) {
  if (!schedule) return <div className="h-64 animate-pulse rounded-xl bg-panel" />;
  const byKey = new Map(schedule.slots.map((s) => [`${s.date}|${s.time}|${s.provider}`, s]));

  return (
    <section className="rounded-xl border border-line bg-panel p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold">Schedule</h2>
        <div className="flex gap-1">
          <button onClick={onPrev} disabled={!canPrev} className="rounded border border-line px-2 text-sm disabled:opacity-30" aria-label="Previous week">
            ‹
          </button>
          <button onClick={onNext} disabled={!canNext} className="rounded border border-line px-2 text-sm disabled:opacity-30" aria-label="Next week">
            ›
          </button>
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full border-separate border-spacing-0.5 text-[11px]">
          <thead>
            <tr>
              <th />
              {schedule.dates.map((d) => (
                <th key={d} className="pb-1 font-medium text-muted">
                  {prettyDate(d)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {schedule.times.map((t) => (
              <tr key={t} className={t === "13:00" ? "border-t" : ""}>
                <td className="pr-1 text-right font-mono text-muted">{t}</td>
                {schedule.dates.map((d) => (
                  <td key={d}>
                    <div className="flex justify-center gap-0.5">
                      {schedule.providers.map((p) => {
                        const s = byKey.get(`${d}|${t}|${p}`);
                        if (!s) return null;
                        const status = s.slot_id === pendingSlot ? "pending" : s.status;
                        return (
                          <span
                            key={p}
                            title={`${prettyDate(d)} ${t} · ${p} · ${status}`}
                            className={`inline-flex h-5 w-5 items-center justify-center rounded border text-[10px] font-semibold ${STATUS_CLS[status]}`}
                          >
                            {initials(p)}
                          </span>
                        );
                      })}
                    </div>
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="mt-3 flex flex-wrap gap-3 text-[11px] text-muted">
        <span>R = Dr. Rivera · C = Dr. Chen · O = Dr. Okafor</span>
        <span className="inline-flex items-center gap-1"><i className={`h-3 w-3 rounded border ${STATUS_CLS.open}`} />open</span>
        <span className="inline-flex items-center gap-1"><i className={`h-3 w-3 rounded border ${STATUS_CLS.booked}`} />booked</span>
        <span className="inline-flex items-center gap-1"><i className={`h-3 w-3 rounded border ${STATUS_CLS.mine}`} />yours</span>
        <span className="inline-flex items-center gap-1"><i className={`h-3 w-3 rounded border ${STATUS_CLS.pending}`} />awaiting approval</span>
      </div>
    </section>
  );
}
