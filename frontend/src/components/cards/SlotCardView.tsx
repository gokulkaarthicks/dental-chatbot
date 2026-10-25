import { ChevronRight } from "lucide-react";

import { formatSlotTime, CardShell } from "./CardShell";
import type { SlotCard } from "../../lib/cards";

/**
 * Available appointment times, grouped by day.
 *
 * Each row is a real button carrying the backend's stable slot id. The whole
 * row is clickable — the chevron is an affordance, not the target — so the hit
 * area is the full width of the card at every breakpoint.
 *
 * Every slot shows its time, dentist, and duration. Slots the server no longer
 * offers (a superseded grid, a time taken by someone else, or a slot already
 * held in a newer proposal) stay visible as history but render disabled with a
 * reason, so a stale card can never look bookable.
 */
export function SlotCardView({
  slots,
  onSelect,
  busySlotId,
  disabled,
  isEnabled,
}: {
  slots: SlotCard[];
  onSelect: (slot: SlotCard) => void;
  busySlotId: string | null;
  disabled: boolean;
  isEnabled?: (slot: SlotCard) => boolean;
}) {
  if (slots.length === 0) return null;
  const first = slots[0];
  // Replacement times for a pending move say so, so the patient never reads
  // them as "book another appointment".
  const moving = slots.some((slot) => slot.intent === "reschedule");
  // One booking per request: a grid that offers several times is one request
  // with alternatives, not several appointments. Once one is held or booked the
  // grid is settled, and the footer must not invite a second hold.
  const settled = slots.some((slot) => slot.settled);
  // A held time is not a booking; say which one, so the patient knows whether
  // there is anything left to confirm.
  const settledByHold = settled && slots.every((slot) => (slot.settled_by ?? "hold") === "hold");
  const days = new Map<string, SlotCard[]>();
  for (const slot of slots) {
    const key = slot.starts_at.slice(0, 10);
    days.set(key, [...(days.get(key) ?? []), slot]);
  }
  // A grid where nothing is clickable is history: say so once in the footer
  // rather than repeating "no longer open" on every row.
  const anyLive = slots.some((slot) => (isEnabled ? isEnabled(slot) : true));

  return (
    <CardShell
      tone="slots"
      eyebrow={
        settled
          ? "Earlier options"
          : anyLive
          ? moving
            ? "Times to move to"
            : "Open times"
          : "Earlier options"
      }
      title={first ? `${first.appointment_type} · ${first.duration_minutes} min` : "Open times"}
      footer={
        <p className="cardx__hint">
          {settled
            ? settledByHold
              ? "I'm holding the time you picked, so these other times are no longer bookable for this visit. Confirm the card above to book it, or ask for a separate visit."
              : "This request is already booked, so these other times are no longer bookable. Ask for a new visit if you need another appointment."
            : anyLive
            ? moving
              ? "Picking a time holds it for 2 minutes. Asking for other times gives it straight back. Your current appointment stays booked until you confirm the move."
              : "Picking a time holds it for 2 minutes. Asking for other times gives it straight back, and nothing is booked until you confirm."
            : "These times have been superseded or taken. Ask for current availability and I'll check again."}
        </p>
      }
    >
      <div className="slots">
        {[...days.entries()].map(([day, daySlots]) => (
          <div className="slots__day" key={day}>
            <p className="slots__daylabel">
              {new Date(daySlots[0].starts_at).toLocaleDateString(undefined, {
                weekday: "long",
                month: "short",
                day: "numeric",
              })}
            </p>
            <ul className="slots__list">
              {daySlots.map((slot) => {
                const busy = busySlotId === slot.slot_id;
                const live = isEnabled ? isEnabled(slot) : true;
                const endTime = slot.end_time ?? (slot.ends_at ? formatSlotTime(slot.ends_at) : null);
                return (
                  <li key={slot.slot_id}>
                    <button
                      type="button"
                      className={`slots__button${live ? "" : " slots__button--stale"}`}
                      onClick={() => onSelect(slot)}
                      disabled={!live || disabled || (busySlotId !== null && !busy)}
                      aria-busy={busy}
                    >
                      <span className="slots__time">
                        {formatSlotTime(slot.starts_at)}
                        {endTime ? <span className="slots__end">–{endTime}</span> : null}
                      </span>
                      <span className="slots__dentist">
                        {slot.provider}
                        {slot.provider_credentials ? `, ${slot.provider_credentials}` : ""}
                      </span>
                      <span className="slots__meta">
                        {slot.duration_minutes} min
                      </span>
                      <span className="slots__cta">
                        {busy ? "Holding…" : live ? "Select" : "Unavailable"}
                      </span>
                      <ChevronRight
                        size={14}
                        strokeWidth={1.75}
                        aria-hidden="true"
                        className="slots__chev"
                      />
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </div>
    </CardShell>
  );
}
