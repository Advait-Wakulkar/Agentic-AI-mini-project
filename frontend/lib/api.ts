// Typed client for the FastAPI backend (proxied at /api by next.config.ts).
import { getAccessToken } from "@/lib/supabase";

export type Step = {
  step: number;
  kind: string;
  [key: string]: unknown;
};

export type ApprovalRequest = {
  action?: "book" | "cancel" | "reschedule";
  appointment_id?: number;
  from?: { slot_id: string; date: string; time: string; provider: string };
  slot_id: string;
  date: string;
  time: string;
  provider: string;
  patient_alias: string;
  reason: string;
  patient_request?: string;
  patient_profile?: {
    name: string | null;
    date_of_birth: string | null;
    phone: string | null;
    email: string | null;
    insurance: string | null;
    insurance_status: string | null;
    new_patient: boolean;
  };
};

export type Notice = {
  id: number;
  appointment_id: number;
  patient_alias: string;
  kind: "confirmation" | "reminder" | "invite";
  channel: "email" | "sms";
  to_address: string;
  subject: string | null;
  body: string;
  has_invite: boolean;
  status: "simulated" | "sent" | "failed";
  sent_by: string;
  created_at: string;
};

export type ReminderStatus = {
  enabled: boolean;
  interval_seconds: number;
  window_hours: number;
  smtp: boolean;
  last_run: string | null;
  last_error: string | null;
  notifications: Notice[];
};

export type Visit ={ date: string; time: string; provider: string; service: string; status: string; note: string | null };

export type PatientHistory = {
  ok: boolean;
  registered: boolean;
  profile?: {
    first_name: string; last_name: string; date_of_birth: string; phone: string; email: string;
    insurance_provider: string; insurance_member_id: string | null; insurance_status: string; patient_since: string;
  };
  upcoming: { appointment_id: number; date: string; time: string; provider: string; reason: string }[];
  past_visits: Visit[];
};

export type TurnResult = {
  reply: string;
  stop_reason: string;
  agent?: string;
  steps: Step[];
  pending: ApprovalRequest | null;
  record_path: string | null;
};

export type Config = {
  llm: string | null;
  llm_error: string | null;
  decider: string;
  database: string;
  auth_enabled: boolean;
  today: string;
  patients: string[];
  providers: string[];
};

export type ProviderStatus = {
  provider: string;
  ready: boolean;
  why_not: string;
  default_model: string;
  local: boolean;
  models: string[];
};

export type Me = { signed_in: boolean; demo: boolean; is_staff: boolean; email?: string; patient?: string };

export type StaffRequest = {
  id: number;
  session_id: string;
  patient_alias: string;
  action: "book" | "cancel" | "reschedule";
  request: ApprovalRequest;
  status: "pending" | "approved" | "denied" | "expired";
  created_at: string;
  decided_at: string | null;
  decided_by: string | null;
  outcome: string | null;
  chat_live: boolean;
};

export type HistoryRow = {
  id: number;
  action: string;
  patient_alias: string;
  date: string | null;
  time: string | null;
  provider: string | null;
  reason: string;
  decision: "approved" | "denied";
  decided_by: string;
  decided_at: string;
  outcome: string | null;
};

export type Slot = {
  slot_id: string;
  date: string;
  time: string;
  provider: string;
  status: "open" | "booked" | "mine";
};

export type Schedule = {
  dates: string[];
  all_dates: string[];
  times: string[];
  providers: string[];
  slots: Slot[];
};

export type Appointment = {
  id: number;
  slot_id: string;
  date: string;
  time: string;
  provider: string;
  reason: string;
  approval_id: number;
  created_at: string;
};

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getAccessToken();
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init?.headers ?? {}),
    },
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      /* not JSON (e.g. backend down) */
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

export const api = {
  config: () => call<Config>("/config"),
  newSession: (patient: string) =>
    call<{ session_id: string; patient: string }>("/session", { method: "POST", body: JSON.stringify({ patient: patient || null }) }),
  patientHistory: (patient?: string) =>
    call<PatientHistory>(`/history${patient ? `?patient=${encodeURIComponent(patient)}` : ""}`),
  chat: (session_id: string, message: string) =>
    call<TurnResult>("/chat", { method: "POST", body: JSON.stringify({ session_id, message }) }),
  me: () => call<Me>("/me"),
  events: (session_id: string) =>
    call<{ pending: ApprovalRequest | null; events: TurnResult[] }>(`/session/${session_id}/events`),
  staffRequests: (status: string) => call<StaffRequest[]>(`/staff/requests?status=${status}`),
  decide: (id: number, approved: boolean) =>
    call<{ id: number; status: string; outcome: string | null }>(`/staff/requests/${id}/decide`, {
      method: "POST",
      body: JSON.stringify({ approved }),
    }),
  history: () => call<HistoryRow[]>("/staff/history"),
  schedule: (patient?: string, start?: string) =>
    call<Schedule>(`/schedule?days=5${patient ? `&patient=${encodeURIComponent(patient)}` : ""}${start ? `&start=${start}` : ""}`),
  appointments: (patient?: string) =>
    call<Appointment[]>(`/appointments${patient ? `?patient=${encodeURIComponent(patient)}` : ""}`),
  models: () => call<{ current: string | null; providers: ProviderStatus[] }>("/models"),
  setModel: (provider: string, model: string) =>
    call<{ llm: string }>("/model", { method: "POST", body: JSON.stringify({ provider, model }) }),
  reset: () => call<{ ok: boolean }>("/reset", { method: "POST" }),
  notifications: (patient?: string) =>
    call<Notice[]>(`/notifications${patient ? `?patient=${encodeURIComponent(patient)}` : ""}`),
  inviteUrl: (id: number, patient?: string) =>
    `/api/notifications/${id}/invite.ics${patient ? `?patient=${encodeURIComponent(patient)}` : ""}`,
  reminderStatus: () => call<ReminderStatus>("/staff/notifications"),
  runReminders: () =>
    call<{ jobs: number; fallbacks: number; results: { kind: string; when: string; sent: Record<string, string> }[] }>(
      "/staff/reminders/run", { method: "POST" }),
};

export function prettyDate(iso: string, opts: Intl.DateTimeFormatOptions = { weekday: "short", month: "short", day: "numeric" }) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString("en-US", opts);
}
