/**
 * Typed API client.
 *
 * All requests go to same-origin `/api/...` and are proxied to the backend by
 * Vite (dev) or nginx (containers). Responses never contain secrets; the
 * DeepSeek key lives only in the backend process.
 */

import type { ChatCard, HandoffEntry, ProposalCard, SlotCard } from "../lib/cards";

export type HealthResponse = {
  status: "ok" | "degraded";
  service: string;
  version: string;
  timestamp: string;
  database: "ok" | "unavailable";
  schema_version: number | null;
  reset_generation: string | null;
  seeded: boolean;
  clinic_now: string | null;
};

export type BusinessHours = { open: string | null; close: string | null };

export type ClinicResponse = {
  name: string;
  legal_note: string;
  address: {
    line1: string;
    line2: string;
    city: string;
    state: string;
    postal_code: string;
  };
  phone: string;
  email: string;
  timezone: string;
  timezone_note?: string;
  slot_minutes: number;
  business_hours: Record<string, BusinessHours>;
  closed_days: string[];
  parking_note: string;
  fictional: boolean;
  policies?: { topic: string; question: string; answer: string }[];
  insurance?: {
    accepted_plans: { name: string; note: string }[];
    self_pay_label: string;
    note: string;
  };
  self_pay_label?: string;
};

export type DemoCounts = {
  providers: number;
  patients: number;
  family_groups: number;
  appointments: number;
  upcoming_appointments: number;
  today_appointments: number;
  availability_slots: number;
  available_slots: number;
  occupied_slots: number;
  staff_users: number;
  conversations: number;
  messages: number;
  pending_proposals: number;
  pending_requests: number;
  audit_records: number;
};

export type DemoStatusResponse = {
  generated_at: string;
  reset_generation: string;
  reset_count: number;
  last_reset_at: string | null;
  last_seeded_at: string | null;
  seeded: boolean;
  database_empty: boolean;
  clinic_today: string;
  clinic_today_label: string;
  clinic_now: string;
  clinic_now_offset: string;
  horizon_days: number;
  slot_minutes: number;
  counts: DemoCounts;
  fixtures: {
    fixture_set: string;
    fixture_version: number;
    counts: Record<string, number>;
  };
  next_available_slot: string | null;
};

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

const API_BASE = "/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      headers: { Accept: "application/json" },
      ...init,
    });
  } catch {
    throw new ApiError("Cannot reach the clinic service. Is the backend running?", 0);
  }

  if (!response.ok) {
    let detail = `Request failed with status ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body?.detail) detail = body.detail;
    } catch {
      // Non-JSON error body; keep the status message.
    }
    throw new ApiError(detail, response.status);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

/* ------------------------------------------------------------------ */
/* Patient chat                                                        */
/* ------------------------------------------------------------------ */

export type ChatRole = "patient" | "assistant";

export type ChatMessage = {
  id: number | null;
  role: ChatRole;
  content: string;
  sequence?: number | null;
  created_at?: string | null;
  cards?: ChatCard[];
};

export type ChatSessionState = {
  verified: boolean;
  registration_complete: boolean;
  assistant_configured: boolean;
};

export type ChatConversationSummary = {
  conversation_id: string;
  title: string;
  preview: string;
  started_at: string;
  last_message_at: string | null;
  status: string;
  message_count: number;
};

export type ChatSessionResponse = {
  session_token: string;
  session: ChatSessionState;
  reset_generation: string;
  /** The conversation opened with the session; a session always has one. */
  conversation_id: string;
  greeting: string;
  suggestions: string[];
  clinic_name: string;
};

export type ChatConversationResponse = {
  conversation_id: string;
  conversation: Omit<ChatConversationSummary, "conversation_id">;
  /** Full stored transcript, oldest first. Cards travel on their own turn. */
  messages: ChatMessage[];
  suggestions: string[];
  session: ChatSessionState;
  /**
   * Proposals still pending in SQLite for this conversation, as actionable
   * cards. Present on restore so a refresh can re-enable a hold without a
   * second round-trip. Older servers omit it.
   */
  pending_proposals?: ProposalCard[];
  /** The newest grid's cards, if the server can rebuild them. */
  card_states?: ChatCard[];
};

export type ChatConversationListResponse = {
  conversations: ChatConversationSummary[];
  session: ChatSessionState;
};

export type ChatSendMessageResponse = {
  conversation_id: string;
  reply: string;
  messages: ChatMessage[];
  session: ChatSessionState;
  degraded: boolean;
  cards?: ChatCard[];
};

export type ChatAction = {
  type: "family_option_click";
  option_identity: string;
  request_id?: string | null;
};

type ChatOptions = { signal?: AbortSignal; action?: ChatAction };

function chatHeaders(token: string, json = false): Record<string, string> {
  const headers: Record<string, string> = { "X-Patient-Session": token };
  if (json) headers["Content-Type"] = "application/json";
  return headers;
}

export const chatApi = {
  /**
   * Start a session. With `resumeConversationId` the server tries to hand back
   * that thread instead of opening a new one, but only when the freshly issued
   * session is verified as the patient who owned it — so a browser that still
   * holds a valid thread after losing its token can pick it up again.
   */
  startSession: (signal?: AbortSignal, options: { resumeConversationId?: string } = {}) => {
    const body = options.resumeConversationId
      ? JSON.stringify({ resume_conversation_id: options.resumeConversationId })
      : undefined;
    return request<ChatSessionResponse>("/chat/sessions", {
      method: "POST",
      signal,
      ...(body
        ? { headers: { "Content-Type": "application/json" }, body }
        : {}),
    });
  },

  listConversations: (token: string, signal?: AbortSignal) =>
    request<ChatConversationListResponse>("/chat/conversations", {
      headers: chatHeaders(token),
      signal,
    }),

  startConversation: (token: string, signal?: AbortSignal) =>
    request<ChatConversationResponse>("/chat/conversations", {
      method: "POST",
      headers: chatHeaders(token),
      signal,
    }),

  getConversation: (token: string, conversationId: string, signal?: AbortSignal) =>
    request<ChatConversationResponse>(`/chat/conversations/${encodeURIComponent(conversationId)}`, {
      headers: chatHeaders(token),
      signal,
    }),

  sendMessage: (
    token: string,
    conversationId: string,
    message: string,
    options: ChatOptions = {},
  ) =>
    request<ChatSendMessageResponse>(
      `/chat/conversations/${encodeURIComponent(conversationId)}/messages`,
      {
        method: "POST",
        headers: chatHeaders(token, true),
        body: JSON.stringify({ message, ...(options.action ? { action: options.action } : {}) }),
        signal: options.signal,
      },
    ),
};

export const api = {
  health: (signal?: AbortSignal) => request<HealthResponse>("/health", { signal }),
  clinic: (signal?: AbortSignal) => request<ClinicResponse>("/clinic", { signal }),
  demoStatus: (signal?: AbortSignal) => request<DemoStatusResponse>("/demo/status", { signal }),
};

/* ------------------------------------------------------------------ */
/* Scheduling actions                                                  */
/* ------------------------------------------------------------------ */

export type SchedulingActionResponse = {
  ok: boolean;
  error: string | null;
  message: string | null;
  proposal_id: string | null;
  kind?: string | null;
  intent?: string | null;
  target_appointment_id?: number | null;
  appointment_unchanged?: boolean | null;
  confirmed?: boolean | null;
  replayed?: boolean | null;
  released_appointment_ids?: number[];
  cards: ChatCard[];
  proposal: ProposalCard | null;
  /**
   * The action failed and can never succeed as it stands: its visit already
   * moved, or booking it would duplicate one. Nothing changed in the database,
   * and the card must stop offering buttons.
   */
  proposal_stale?: boolean;
  /** A booking card already waiting, when the hold was refused because of it. */
  pending_proposal_id?: string | null;
  next_step?: string | null;
};

export type PendingProposalsResponse = {
  /** Full proposal cards that are still pending in SQLite, actions included. */
  proposals: ProposalCard[];
  handoff: HandoffEntry[];
  conversation_id?: string | null;
};

/** Availability re-check for one rendered grid: what is still offered now. */
export type AvailabilityRefreshResponse = {
  ok: boolean;
  proposal_id: string | null;
  cards: SlotCard[];
  /** Slot ids that are no longer bookable (taken, superseded, or invalid). */
  rejected: string[];
  message: string | null;
  /**
   * The conversation's still-pending proposals, re-derived from SQLite. A client
   * that receives this can treat any other proposal card as history.
   */
  proposals?: ProposalCard[];
};

function idempotencyKey(prefix: string): string {
  // Stable enough for a demo and unique per action; the server also dedupes on
  // its own proposal/confirmation state.
  const random =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}-${random}`;
}

export const schedulingApi = {
  /** Hold a clicked slot. Never books: the reply is an Awaiting Confirmation card. */
  selectSlot: (
    token: string,
    input: {
      slot_ids: string[];
      appointment_type: string;
      conversation_id: string;
      /** The card's own meaning, so the server continues the right flow. */
      intent?: "new_booking" | "reschedule" | null;
      target_appointment_id?: number | null;
      /** The card was just searched: it books a new visit, even mid-move. */
      require_open?: boolean;
      /** The card belongs to an explicitly requested separate visit. */
      additionalVisit?: boolean;
      summary?: string;
      createKey?: string;
    },
  ) =>
    request<SchedulingActionResponse>("/scheduling/slots/select", {
      method: "POST",
      headers: chatHeaders(token, true),
      body: JSON.stringify({
        slot_ids: input.slot_ids,
        appointment_type: input.appointment_type,
        conversation_id: input.conversation_id,
        intent: input.intent ?? undefined,
        target_appointment_id: input.target_appointment_id ?? undefined,
        require_open: input.require_open ?? false,
        additional_visit: input.additionalVisit ?? false,
        summary: input.summary,
        create_key: input.createKey ?? idempotencyKey("create"),
      }),
    }),

  confirm: (token: string, proposalId: string, conversationId: string, confirmKey?: string) =>
    request<SchedulingActionResponse>(
      `/scheduling/proposals/${encodeURIComponent(proposalId)}/confirm`,
      {
        method: "POST",
        headers: chatHeaders(token, true),
        body: JSON.stringify({
          conversation_id: conversationId,
          confirm_key: confirmKey ?? idempotencyKey("confirm"),
        }),
      },
    ),

  decline: (token: string, proposalId: string, conversationId: string) =>
    request<SchedulingActionResponse>(
      `/scheduling/proposals/${encodeURIComponent(proposalId)}/decline`,
      {
        method: "POST",
        headers: chatHeaders(token, true),
        body: JSON.stringify({ conversation_id: conversationId }),
      },
    ),

  pending: (token: string, conversationId: string) =>
    request<PendingProposalsResponse>(
      `/scheduling/pending?conversation_id=${encodeURIComponent(conversationId)}`,
      { headers: chatHeaders(token) },
    ),

  /**
   * Re-check one rendered grid against SQLite, unchanged otherwise. The server
   * re-derives every id, so the reply is the authoritative set of times that can
   * still be held right now.
   */
  availability: (
    token: string,
    input: {
      conversation_id: string;
      appointment_type: string;
      slot_ids: string[];
      patient_first_name?: string;
      /** The grid's meaning, so a replacement grid stays a replacement grid. */
      intent?: "new_booking" | "reschedule" | null;
      target_appointment_id?: number | null;
      /** Keeps an additional-visit grid live when the thread already has a visit. */
      additional_visit?: boolean;
    },
  ) =>
    request<AvailabilityRefreshResponse>("/scheduling/availability", {
      method: "POST",
      headers: chatHeaders(token, true),
      body: JSON.stringify(input),
    }),

  expireStale: (token: string, conversationId: string) =>
    request<SchedulingActionResponse>(
      `/scheduling/proposals/expire?conversation_id=${encodeURIComponent(conversationId)}`,
      { method: "POST", headers: chatHeaders(token) },
    ),
};
