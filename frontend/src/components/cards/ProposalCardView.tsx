import { Button } from "../Button";
import { CardShell, formatSlotDay, formatSlotTime } from "./CardShell";
import type { ProposalActions, ProposalCard, ProposalItem } from "../../lib/cards";

const KIND_LABEL: Record<ProposalCard["kind"], string> = {
  booking: "Awaiting confirmation",
  // A move is still awaiting confirmation: the original is only retired once
  // the patient confirms, so the card must not imply the time already changed.
  reschedule: "Awaiting confirmation",
  cancellation: "Confirm cancellation",
};

const STATUS_LABEL: Record<string, string> = {
  rejected: "Cancelled",
  superseded: "Superseded",
  expired: "Expired",
  accepted: "Confirmed",
};

function ItemRow({ item }: { item: ProposalItem }) {
  const moving = Boolean(item.original_starts_at) && item.original_starts_at !== item.starts_at;
  return (
    <li className="proposal__item">
      <div className="proposal__who">
        <span className="proposal__name">{item.patient_first_name ?? "You"}</span>
        <span className="proposal__type">{item.appointment_type}</span>
      </div>
      <div className="proposal__when">
        <span className="proposal__new">
          {formatSlotDay(item.starts_at)} · {formatSlotTime(item.starts_at)}
          {item.duration_minutes ? ` (${item.duration_minutes} min)` : ""}
        </span>
        {moving && (
          <span className="proposal__was">
            was {formatSlotDay(item.original_starts_at)} · {formatSlotTime(item.original_starts_at)}
          </span>
        )}
      </div>
      <span className="proposal__dentist">{item.provider}</span>
    </li>
  );
}

/**
 * A persisted proposal. The buttons map 1:1 to backend actions:
 *
 *  - Confirm  -> POST /proposals/{id}/confirm (revalidates, then books)
 *  - Change   -> asks the assistant for alternatives, releasing the hold
 *  - Cancel   -> POST /proposals/{id}/decline, releasing the hold
 *
 * `disabled` is true while any action is in flight so a double click cannot
 * send two confirmations. `enabled` is false when the server no longer reports
 * this proposal as pending (it was booked, superseded, expired, or its visit
 * already changed): the card then stays in the thread as history with no
 * buttons, so an outdated proposal can never look actionable.
 */
export function ProposalCardView({
  card,
  onAction,
  busy,
  disabled,
  enabled = true,
}: {
  card: ProposalCard;
  onAction: (action: ProposalActions, card: ProposalCard) => void;
  busy: ProposalActions | null;
  disabled: boolean;
  enabled?: boolean;
}) {
  const isCancellation = card.kind === "cancellation";
  const isReschedule = card.kind === "reschedule";
  const actionable = enabled && card.status === "pending" && card.actions.length > 0;
  const confirmLabel = isCancellation
    ? "Yes, cancel it"
    : isReschedule
      ? "Move it"
      : "Confirm";
  const eyebrow = actionable
    ? KIND_LABEL[card.kind] ?? "Awaiting confirmation"
    : STATUS_LABEL[card.status] ?? "Earlier proposal";
  const resolvedNote = STATUS_LABEL[card.status]
    ? `This proposal is ${STATUS_LABEL[card.status].toLowerCase()}.`
    : isCancellation
      ? "This cancellation card is no longer open."
      : isReschedule
        ? "This move is no longer open."
        : "This hold is no longer open.";

  return (
    <CardShell
      tone={actionable ? "pending" : "neutral"}
      eyebrow={eyebrow}
      title={card.title}
      footer={
        actionable ? (
          <div className="proposal__actions">
            <Button
              variant="primary"
              onClick={() => onAction("confirm", card)}
              disabled={disabled}
              aria-busy={busy === "confirm"}
            >
              {busy === "confirm"
                ? isReschedule
                  ? "Moving…"
                  : "Booking…"
                : confirmLabel}
            </Button>
            <Button onClick={() => onAction("change", card)} disabled={disabled}>
              Change
            </Button>
            <Button
              variant="ghost"
              className="proposal__decline"
              onClick={() => onAction("cancel", card)}
              disabled={disabled}
            >
              {isCancellation ? "Keep appointment" : isReschedule ? "Keep current time" : "Cancel"}
            </Button>
          </div>
        ) : (
          <p className="cardx__hint">{resolvedNote} Ask me for the current options.</p>
        )
      }
    >
      <ul className="proposal__items">
        {card.items.map((item, index) => (
          <ItemRow
            key={`${card.proposal_id}-${index}`}
            item={
              isCancellation
                ? {
                    ...item,
                    starts_at: item.original_starts_at ?? item.starts_at,
                    original_starts_at: null,
                  }
                : item
            }
          />
        ))}
      </ul>
      {actionable && isCancellation && (
        <p className="proposal__warning">
          This frees the time for someone else. We can usually rebook, but the exact slot may be
          taken.
        </p>
      )}
      {actionable && isReschedule && (
        <p className="proposal__warning">
          Your current appointment stays booked until you confirm this move.
        </p>
      )}
      {card.summary && <p className="proposal__summary">{card.summary}</p>}
      {actionable && card.expires_at && (
        <p className="cardx__hint">Held until {formatSlotTime(card.expires_at)}.</p>
      )}
    </CardShell>
  );
}
