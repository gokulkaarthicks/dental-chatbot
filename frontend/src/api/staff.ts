/**
 * Staff dashboard API client (Phase 4).
 *
 * Two things differ from the patient client in `./client`:
 *
 * 1. `credentials: "include"` so the HttpOnly session cookie is sent. The app
 *    never reads that cookie -- it cannot -- so XSS cannot steal the session the
 *    way it could a token kept in localStorage.
 * 2. Every mutating request echoes the CSRF token from the readable
 *    `mb_staff_csrf` cookie in `X-Staff-CSRF`. The server pairs it with the
 *    HttpOnly cookie it was issued with.
 *
 * These checks are for a good experience, not for security: the FastAPI
 * dependencies enforce the same rules, so bypassing this file gains nothing.
 */

import { ApiError } from "./client";

const API_BASE = "/api";

export type StaffUser = {
  id: number;
  username: string | null;
  display_name: string;
  email: string;
  role: "receptionist" | "clinic_manager";
};

export type StaffSession = {
  authenticated: boolean;
  user: StaffUser | null;
  csrf_token: string | null;
  expires_at: string | null;
  clinic_now: string;
  demo_notice: string;
};

export type SummaryCounts = {
  appointments_today: number;
  appointments_upcoming: number;
  open_alerts: number;
  urgent_alerts: number;
  pending_requests: number;
  approvals_required: number;
};

export type AppointmentStatus = "booked" | "cancelled" | "completed" | "rescheduled";

export type StaffPatientRef = {
  id: number;
  full_name: string;
  first_name?: string;
  phone: string;
  date_of_birth?: string | null;
  insurance_provider?: string | null;
  insurance_member_id?: string | null;
  notes?: string | null;
};

export type StaffAppointment = {
  id: number;
  starts_at: string;
  ends_at: string;
  day: string;
  day_label: string;
  time_label: string;
  end_time_label: string;
  duration_minutes: number;
  status: AppointmentStatus;
  is_upcoming: boolean;
  is_past: boolean;
  created_by: string;
  reason: string | null;
  cancelled_at: string | null;
  /** Set on the retired original of a move: the id of its replacement. */
  rescheduled_at: string | null;
  rescheduled_to_id: number | null;
  /** Set on the replacement: the id of the original it moved from. */
  rescheduled_from_id: number | null;
  rescheduled_from_starts_at: string | null;
  rescheduled_from_provider_id: number | null;
  rescheduled_from_appointment_type_id: number | null;
  rescheduled_from_duration_minutes: number | null;
  created_at: string | null;
  updated_at: string | null;
  patient: StaffPatientRef | null;
  provider: { id: number; full_name: string; credentials: string } | null;
  appointment_type: {
    id: number;
    code: string;
    name: string;
    duration_minutes: number;
    is_emergency: boolean;
  } | null;
  booking_group_id: number | null;
};

export type ProviderOption = { id: number; full_name: string; credentials: string };
export type AppointmentTypeOption = {
  id: number;
  code: string;
  name: string;
  duration_minutes: number;
  is_emergency: boolean;
};

export type AppointmentListResponse = {
  appointments: StaffAppointment[];
  providers: ProviderOption[];
  appointment_types: AppointmentTypeOption[];
  count: number;
};

export type AppointmentDetailResponse = {
  appointment: StaffAppointment;
  history: AuditEntry[];
};

export type AuditEntry = {
  action: string;
  actor_type: string;
  actor_id: string | null;
  occurred_at: string | null;
  details: string | null;
};

export type SlotOption = {
  starts_at: string;
  ends_at: string;
  time_label: string;
  slots_needed: number;
  provider_id: number;
  provider_name: string;
  stable_slot_id: string;
};

export type SlotsResponse = { day: string; slots: SlotOption[]; count: number };

export type StaffAlertStatus = "open" | "acknowledged" | "resolved";
export type StaffAlertSeverity = "low" | "normal" | "high" | "urgent";

export type StaffAlert = {
  id: number;
  kind: string;
  is_urgent: boolean;
  severity: StaffAlertSeverity;
  message: string;
  status: StaffAlertStatus;
  report_count: number;
  created_at: string | null;
  updated_at: string | null;
  last_reported_at: string | null;
  acknowledged_at: string | null;
  resolved_at: string | null;
  resolution_note: string | null;
  patient: { id: number; full_name: string; phone: string } | null;
  /**
   * Contact details an unverified caller left for a call back. Present only
   * when `patient` is null: the chat cannot verify identity before filing an
   * emergency, so the board must still show who to call.
   */
  caller: { name: string | null; phone: string | null } | null;
  conversation_id: string | null;
};

export type StaffAlertListResponse = { alerts: StaffAlert[]; count: number };
export type StaffAlertDetailResponse = { alert: StaffAlert; activity: AuditEntry[] };

export type RequestStatus = "pending" | "in_progress" | "resolved";
export type RequestType = "callback" | "reschedule" | "clinical_question" | "billing";

export type PendingRequest = {
  id: number;
  request_type: RequestType;
  status: RequestStatus;
  details: string;
  approval_required: boolean;
  approved_at: string | null;
  approved_by: string | null;
  assigned_to: string | null;
  created_at: string | null;
  updated_at: string | null;
  resolved_at: string | null;
  resolution_note: string | null;
  preferred_callback_at: string | null;
  patient: { id: number; full_name: string; phone: string } | null;
  conversation_id: string | null;
};

export type PendingRequestListResponse = { requests: PendingRequest[]; count: number };

export type ConversationFeedbackRow = {
  id: number;
  rating: "up" | "down";
  reason: string | null;
  created_at: string;
  conversation_title: string | null;
  excerpt: string;
};

export type ConversationFeedbackResponse = {
  counts: { up: number; down: number; total: number };
  window_days: number;
  feedback: ConversationFeedbackRow[];
};
export type PendingRequestDetailResponse = { request: PendingRequest; activity: AuditEntry[] };

export type PatientDetailResponse = {
  patient: StaffPatientRef;
  appointments: StaffAppointment[];
};

export const STAFF_CSRF_COOKIE = "mb_staff_csrf";

/** Read the readable CSRF cookie. Returns "" when absent. */
export function readCsrfCookie(): string {
  const target = `${STAFF_CSRF_COOKIE}=`;
  for (const part of document.cookie.split(";")) {
    const trimmed = part.trim();
    if (trimmed.startsWith(target)) {
      return decodeURIComponent(trimmed.slice(target.length));
    }
  }
  return "";
}

const MUTATING = new Set(["POST", "PUT", "PATCH", "DELETE"]);

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (MUTATING.has(method)) {
    headers.set("X-Staff-CSRF", readCsrfCookie());
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      method,
      headers,
      // The HttpOnly session cookie is the only credential; never a header token.
      credentials: "same-origin",
    });
  } catch {
    throw new ApiError("Cannot reach the clinic service. Is the backend running?", 0);
  }

  if (!response.ok) {
    let detail = `Request failed with status ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body?.detail === "string") {
        detail = body.detail;
      }
    } catch {
      // Non-JSON error body; the status message is enough.
    }
    throw new ApiError(detail, response.status);
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === "") continue;
    search.set(key, String(value));
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
}

export type AppointmentQuery = {
  day?: string;
  providerId?: number | null;
  status?: string;
  search?: string;
  upcomingOnly?: boolean;
};

export const staffApi = {
  login: (username: string, password: string) =>
    request<StaffSession>("/staff/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    }),

  me: (signal?: AbortSignal) => request<StaffSession>("/staff/session", { signal }),

  logout: () => request<{ ok: boolean }>("/staff/session", { method: "DELETE" }),

  summary: () => request<{ counts: SummaryCounts; generated_at: string }>("/staff/summary"),

  feedback: (days = 30, signal?: AbortSignal) =>
    request<ConversationFeedbackResponse>(`/staff/feedback${qs({ days })}`, { signal }),

  appointments: (query: AppointmentQuery = {}, signal?: AbortSignal) =>
    request<AppointmentListResponse>(
      `/staff/appointments${qs({
        day: query.day,
        provider_id: query.providerId ?? undefined,
        status: query.status,
        search: query.search,
        upcoming_only: query.upcomingOnly ? true : undefined,
      })}`,
      { signal },
    ),

  appointment: (id: number, signal?: AbortSignal) =>
    request<AppointmentDetailResponse>(`/staff/appointments/${id}`, { signal }),

  slots: (day: string, providerId: number | null, appointmentType: string) =>
    request<SlotsResponse>(
      `/staff/slots${qs({
        day,
        provider_id: providerId ?? undefined,
        appointment_type: appointmentType,
      })}`,
    ),

  createAppointment: (payload: {
    patient_id: number;
    provider_id: number;
    starts_at: string;
    appointment_type: string;
    reason?: string;
  }) =>
    request<{ appointment: StaffAppointment }>("/staff/appointments", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),

  reschedule: (id: number, payload: { starts_at: string; provider_id?: number | null }) =>
    request<{ appointment: StaffAppointment }>(`/staff/appointments/${id}/reschedule`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),

  cancel: (id: number, reason?: string) =>
    request<{ appointment: StaffAppointment }>(`/staff/appointments/${id}/cancel`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: reason ?? null }),
    }),

  complete: (id: number) =>
    request<{ appointment: StaffAppointment }>(`/staff/appointments/${id}/complete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    }),

  alerts: (
    query: { status?: string; severity?: string; kind?: string; search?: string } = {},
    signal?: AbortSignal,
  ) => request<StaffAlertListResponse>(`/staff/alerts${qs(query)}`, { signal }),

  alert: (id: number, signal?: AbortSignal) =>
    request<StaffAlertDetailResponse>(`/staff/alerts/${id}`, { signal }),

  setAlertStatus: (id: number, status: StaffAlertStatus, note?: string) =>
    request<{ alert: StaffAlert }>(`/staff/alerts/${id}/status`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status, note: note ?? null }),
    }),

  requests: (
    query: { status?: string; request_type?: string; search?: string } = {},
    signal?: AbortSignal,
  ) => request<PendingRequestListResponse>(`/staff/requests${qs(query)}`, { signal }),

  request: (id: number, signal?: AbortSignal) =>
    request<PendingRequestDetailResponse>(`/staff/requests/${id}`, { signal }),

  setRequestStatus: (id: number, status: RequestStatus, note?: string) =>
    request<{ request: PendingRequest }>(`/staff/requests/${id}/status`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status, note: note ?? null }),
    }),

  approveRequest: (id: number, note?: string) =>
    request<{ request: PendingRequest }>(`/staff/requests/${id}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note: note ?? null }),
    }),

  searchPatients: (q: string, signal?: AbortSignal) =>
    request<{ patients: StaffPatientRef[] }>(`/staff/patients/search${qs({ q })}`, { signal }),

  patient: (id: number, signal?: AbortSignal) =>
    request<PatientDetailResponse>(`/staff/patients/${id}`, { signal }),
};
