/**
 * Interactive card payloads.
 *
 * The backend builds these from database rows it has just read or written, so
 * every id here refers to something real. The UI never invents a slot: a click
 * posts `slot_id` back and the server re-derives the run from SQLite.
 */

export type SlotCard = {
  type: "slot";
  slot_id: string;
  starts_at: string;
  ends_at: string;
  /** Clinic-local calendar day, e.g. 2026-10-27. */
  day?: string;
  day_label: string;
  /** Start / end wall-clock labels, e.g. "9:00 AM". */
  time?: string;
  end_time?: string;
  time_range: string;
  provider: string;
  provider_credentials: string;
  duration_minutes: number;
  appointment_type: string;
  appointment_type_code: string;
  patient_first_name?: string | null;
  /**
   * What picking this time means. "reschedule" cards are replacement times for
   * the appointment in `target_appointment_id`; everything else is an
   * additional visit. Posted back verbatim so a click continues the move
   * instead of booking a second appointment.
   */
  intent?: "new_booking" | "reschedule" | null;
  target_appointment_id?: number | null;
  /** Set on replacement cards: the time being moved away from. */
  moving_from?: string | null;
  /**
   * Set by the client only: this grid's request already has a booking held or
   * confirmed, so its remaining times are alternatives that were not chosen.
   * Never posted back to the server.
   */
  settled?: boolean;
  /**
   * Why the grid is settled: a visit is booked, or a time is held and one
   * click from booked. Read by the prose to say the right thing; never acted on
   * by the UI.
   */
  settled_by?: "booked" | "hold" | null;
  /** This card came from an explicitly requested additional visit. */
  additional_visit?: boolean;
  /**
   * The server searched the schedule when it produced this turn, so the time is
   * an offer made now and may be re-checked against `/availability`. A card
   * without it (the transcript only kept that it once offered times) is never
   * used to answer a re-check: its availability is unknown, not confirmed.
   */
  require_open?: boolean;
};

export type ProposalItem = {
  patient_first_name: string | null;
  provider: string | null;
  provider_credentials: string | null;
  appointment_type: string | null;
  appointment_type_code: string | null;
  starts_at: string | null;
  duration_minutes: number | null;
  original_starts_at: string | null;
};

export type ProposalActions = "confirm" | "change" | "cancel";

export type ProposalCard = {
  type: "proposal" | "family_proposal";
  proposal_id: string;
  kind: "booking" | "reschedule" | "cancellation";
  /** Why the proposal exists; mirrors `kind` except that it is explicit. */
  intent?: "new_booking" | "reschedule" | "cancellation" | null;
  status: string;
  title: string;
  items: ProposalItem[];
  summary?: string | null;
  actions: ProposalActions[];
  expires_at?: string | null;
  /** The existing appointment a reschedule/cancellation is about. */
  target_appointment_id?: number | null;
};

export type FamilyOptionCard = {
  type: "family_option";
  option_id: string;
  slots: SlotCard[];
  back_to_back: boolean;
};

export type ConfirmedAppointment = {
  patient_name: string | null;
  provider: string | null;
  appointment_type: string | null;
  starts_at: string;
  ends_at: string;
  duration_minutes: number;
};

/** The retired half of a move, as recorded by the confirmation transaction. */
export type MovedFrom = {
  appointment_id: number | null;
  starts_at: string | null;
  ends_at: string | null;
  status: "rescheduled" | "cancelled" | string;
};

export type ConfirmationCard = {
  type: "confirmation";
  proposal_id: string;
  booking_group_id: string | null;
  kind: string;
  title: string;
  appointments: ConfirmedAppointment[];
  moved_from?: MovedFrom | null;
  released_appointment_ids?: number[];
  replayed?: boolean;
};

export type CancellationConfirmationCard = {
  type: "cancellation_confirmation";
  proposal_id: string;
  title: string;
  cancelled: { starts_at?: string; reason?: string | null } | null;
};

export type AppointmentSummary = {
  appointment_id: number;
  starts_at: string;
  ends_at: string;
  day: string;
  time: string;
  duration_minutes: number;
  status: string;
  provider: string | null;
  provider_credentials: string | null;
  appointment_type: string | null;
  appointment_type_code: string | null;
  is_upcoming: boolean;
  /** Set on a retired original; the replacement is the live booking. */
  rescheduled_to_id?: number | null;
  /** Set on a replacement; the original is the history row. */
  rescheduled_from_id?: number | null;
};

export type AppointmentsCard = {
  type: "appointments";
  title: string;
  appointments: AppointmentSummary[];
};

export type HandoffEntry = {
  type: "alert" | "request";
  kind: string;
  severity: string;
  status: string;
  summary: string;
  message: string;
  report_count: number;
  created_at: string | null;
};

export type HandoffCard = {
  type: "handoff";
  title: string;
  entries: HandoffEntry[];
};

export type ChatCard =
  | SlotCard
  | ProposalCard
  | FamilyOptionCard
  | ConfirmationCard
  | CancellationConfirmationCard
  | AppointmentsCard
  | HandoffCard;
