import { Button } from "../Button";
import { CardShell, formatSlotDay, formatSlotTime } from "./CardShell";
import type {
  AppointmentsCard,
  CancellationConfirmationCard,
  ConfirmationCard,
  FamilyOptionCard,
  HandoffCard,
} from "../../lib/cards";

/**
 * A booked appointment. Only ever rendered from a backend confirmation, so its
 * presence in the thread means the row is committed.
 */
export function ConfirmationCardView({ card }: { card: ConfirmationCard }) {
  return (
    <CardShell tone="confirmed" eyebrow="Confirmed" title={card.title}>
      <ul className="confirmed__list">
        {card.appointments.map((appointment) => (
          <li key={appointment.starts_at + (appointment.patient_name ?? "")}>
            <p className="confirmed__when">
              {formatSlotDay(appointment.starts_at)} · {formatSlotTime(appointment.starts_at)} –{" "}
              {formatSlotTime(appointment.ends_at)}
            </p>
            <p className="confirmed__meta">
              {appointment.appointment_type}
              {appointment.patient_name ? ` · ${appointment.patient_name}` : ""}
              {appointment.provider ? ` · ${appointment.provider}` : ""}
            </p>
          </li>
        ))}
      </ul>
      <p className="cardx__hint">You&apos;ll see this in “Your appointments” any time you ask.</p>
    </CardShell>
  );
}

export function CancellationConfirmationCardView({
  card,
}: {
  card: CancellationConfirmationCard;
}) {
  return (
    <CardShell tone="confirmed" eyebrow="Cancelled" title={card.title}>
      <p className="confirmed__when">
        {card.cancelled?.starts_at
          ? `${formatSlotDay(card.cancelled.starts_at)} · ${formatSlotTime(card.cancelled.starts_at)}`
          : "Appointment released"}
      </p>
      {card.cancelled?.reason && <p className="confirmed__meta">Reason: {card.cancelled.reason}</p>}
      <p className="cardx__hint">Nothing else on this appointment was changed.</p>
    </CardShell>
  );
}

/** The patient's real appointments, with their upcoming visits first. */
export function AppointmentsCardView({ card }: { card: AppointmentsCard }) {
  const upcoming = card.appointments.filter((appointment) => appointment.is_upcoming);
  return (
    <CardShell tone="neutral" eyebrow="On the books" title={card.title}>
      {upcoming.length === 0 ? (
        <p className="cardx__empty">No upcoming appointments right now.</p>
      ) : (
        <ul className="appointments">
          {upcoming.map((appointment) => (
            <li key={appointment.appointment_id} className="appointments__row">
              <p className="confirmed__when">
                {formatSlotDay(appointment.starts_at)} · {formatSlotTime(appointment.starts_at)}
              </p>
              <p className="confirmed__meta">
                {appointment.appointment_type}
                {appointment.provider ? ` · ${appointment.provider}` : ""}
              </p>
            </li>
          ))}
        </ul>
      )}
    </CardShell>
  );
}

/**
 * A family option discovered by search. The hold itself is requested in
 * conversation so the assistant can capture per-member details first.
 */
export function FamilyOptionCardView({
  card,
  onRequest,
  disabled,
}: {
  card: FamilyOptionCard;
  onRequest: (card: FamilyOptionCard) => void;
  disabled: boolean;
}) {
  return (
    <CardShell
      tone="slots"
      eyebrow="Family option"
      title={card.back_to_back ? "Back-to-back times" : "Times on the same day"}
      footer={
        <Button variant="primary" onClick={() => onRequest(card)} disabled={disabled}>
          Hold this option
        </Button>
      }
    >
      <ul className="family">
        {card.slots.map((slot) => (
          <li key={slot.slot_id} className="family__row">
            <span className="family__who">{slot.patient_first_name ?? slot.appointment_type}</span>
            <span className="family__when">
              {formatSlotTime(slot.starts_at)} · {slot.appointment_type} ({slot.duration_minutes} min)
            </span>
            <span className="family__dentist">{slot.provider}</span>
          </li>
        ))}
      </ul>
    </CardShell>
  );
}

/**
 * Front-desk handoff status.
 *
 * Wording is derived from the stored status, and never claims a person has read
 * or replied — the system cannot verify that.
 */
export function HandoffCardView({ card }: { card: HandoffCard }) {
  const urgent = card.entries.some((entry) => entry.severity === "urgent" || entry.severity === "high");
  return (
    <CardShell tone={urgent ? "urgent" : "neutral"} eyebrow="Front desk follow-up" title={card.title}>
      <ul className="handoff">
        {card.entries.map((entry, index) => (
          <li key={`${entry.type}-${entry.created_at ?? index}`} className="handoff__row">
            <span className={`handoff__status handoff__status--${entry.status}`}>
              {entry.summary}
            </span>
            <span className="handoff__kind">{entry.kind.replace(/_/g, " ")}</span>
            <p className="handoff__message">{entry.message}</p>
            {entry.report_count > 1 && (
              <p className="cardx__hint">Updated {entry.report_count} times.</p>
            )}
          </li>
        ))}
      </ul>
    </CardShell>
  );
}
