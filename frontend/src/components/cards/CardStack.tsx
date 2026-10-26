/**
 * One message's cards: coalesced slot grids, then everything else in order.
 *
 * `CardHandlers` is the entire interaction contract. A card never fetches or
 * mutates anything itself -- it calls a handler and renders whatever busy or
 * disabled state the page hands back, so "is this still actionable?" is decided
 * in one place rather than once per card type.
 */

import type { ChatCard, FamilyOptionCard, ProposalActions, ProposalCard, SlotCard } from "../../lib/cards";
import { AppointmentsCardView, CancellationConfirmationCardView, ConfirmationCardView, FamilyOptionCardView, HandoffCardView } from "./StatusCards";
import { ProposalCardView } from "./ProposalCardView";
import { SlotCardView } from "./SlotCardView";

export type CardHandlers = {
  onSelectSlot: (slot: SlotCard) => void;
  onProposalAction: (action: ProposalActions, card: ProposalCard) => void;
  onHoldFamily: (card: FamilyOptionCard) => void;
  /** Slot id of the click currently being held. */
  busySlotId: string | null;
  /** Proposal/action currently in flight, if any. */
  busyAction: ProposalActions | null;
  disabled: boolean;
  /**
   * Whether one slot is still actionable. Older grids, taken times, and slots
   * the server has stopped offering are shown but cannot be clicked.
   */
  isSlotEnabled?: (slot: SlotCard, messageKey?: string) => boolean;
  /**
   * Whether one proposal card is still actionable. Anything the server no longer
   * reports as pending renders as history, never as a button.
   */
  isProposalEnabled?: (card: ProposalCard) => boolean;
  /** Whether this message's offered grids are history after a settled action. */
  isGridSettled?: (messageKey?: string) => boolean;
};

/**
 * Render a turn's cards.
 *
 * Slot cards are coalesced into a single grid: the backend emits one card per
 * slot, but a patient should see one panel of choices. Every other card renders
 * individually and in server order.
 */
export function CardStack({
  cards,
  handlers,
  messageKey,
}: {
  cards: ChatCard[];
  handlers: CardHandlers;
  messageKey?: string;
}) {
  const slots = cards.filter((card): card is SlotCard => card.type === "slot");
  const others = cards.filter((card) => card.type !== "slot");

  return (
    <div className="cards">
      {slots.length > 0 && (
        <SlotCardView
          slots={slots}
          onSelect={handlers.onSelectSlot}
          busySlotId={handlers.busySlotId}
          disabled={handlers.disabled}
          isEnabled={handlers.isSlotEnabled}
          messageKey={messageKey}
        />
      )}

      {others.map((card, index) => {
        const key = cardKey(card, index);
        switch (card.type) {
          case "proposal":
          case "family_proposal":
            return (
              <ProposalCardView
                key={key}
                card={card}
                onAction={handlers.onProposalAction}
                busy={handlers.busyAction}
                disabled={handlers.disabled}
                enabled={handlers.isProposalEnabled ? handlers.isProposalEnabled(card) : true}
              />
            );
          case "confirmation":
            return <ConfirmationCardView key={key} card={card} />;
          case "cancellation_confirmation":
            return <CancellationConfirmationCardView key={key} card={card} />;
          case "appointments":
            return <AppointmentsCardView key={key} card={card} />;
          case "family_option":
            return (
              <FamilyOptionCardView
                key={key}
                card={card}
                onRequest={handlers.onHoldFamily}
                disabled={handlers.disabled || Boolean(card.settled) || Boolean(handlers.isGridSettled?.(messageKey))}
              />
            );
          case "handoff":
            return <HandoffCardView key={key} card={card} />;
          default:
            // Unknown card types are ignored rather than breaking the thread,
            // so an older client keeps working after a backend addition.
            return null;
        }
      })}
    </div>
  );
}

function cardKey(card: ChatCard, index: number): string {
  if ("proposal_id" in card) return `c-${card.proposal_id}-${card.type}`;
  if (card.type === "family_option") return `c-${card.option_id}`;
  return `c-${card.type}-${index}`;
}
