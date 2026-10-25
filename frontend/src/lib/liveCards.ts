/**
 * A turn's cards, read as the thread should show them.
 *
 * The assistant's prose is written by a model and stored as text; the cards are
 * rebuilt by the server from SQLite. They can drift apart: an intro can promise
 * "the soonest openings are Monday, October 26" above a grid dated Saturday,
 * and a reply that says "here are the times" can be persisted without its grid
 * at all. This module makes the cards the display truth:
 *
 *  - a day named in the prose that the grid does not contain is not shown, so
 *    the prose never describes times that are not on screen;
 *  - a reply that asks the patient to pick a time but carries no grid is not
 *    shown as an offer, because there is nothing to pick;
 *  - every date in prose comes from the backend, so the calendar day is read
 *    from the ISO string itself rather than from a timezone-shifted Date.
 */

import type { ChatCard, SlotCard } from "./cards";

/**
 * Phrasing that asks the patient to choose a *time*.
 *
 * It must reference a selectable time, not merely offer a choice between
 * options. "Which one did you have in mind?" (which Tuesday?) and "Which one
 * would you prefer?" are ordinary questions and must keep their text.
 *
 * The noun has to be what is actually being chosen, so the time word is bound
 * to the "which" phrase rather than searched for anywhere in the sentence:
 * "Which dentist did you see last time?" is a question about a dentist, and
 * "...last time" is an adverb of frequency, not a slot.
 */
const ASKS_TO_PICK =
  /\bwhich\b[^?.!]{0,20}?\b(?:time|slot|appointment|morning|afternoon|evening|day|date)\b|\bwhich (works?|fits)\b|\bworks for you\b|\bdoes that work\b/i;

/**
 * The clause the patient is being asked to choose within.
 *
 * A reply can hold several sentences; only the one carrying the question may
 * hide the turn. Sentence-wide matching would let "Which plan do you have? I've
 * left the times below." count as an offer of times.
 */
function asksToPickATime(text: string): boolean {
  return text
    .split(/(?<=[?.!])\s+|\n+/)
    .map((clause) => clause.trim())
    .filter((clause) => clause.length > 0)
    .some((clause) => ASKS_TO_PICK.test(clause));
}

/**
 * How one turn should be displayed.
 *
 * `hide` marks a reply that offers times its cards do not carry, so prose that
 * points at a grid which is not on screen is never shown as an offer. A grid,
 * once rendered, always keeps its rows: only the server retires a time.
 */
export function reconcileTurn(
  content: string,
  cards: ChatCard[],
): { cards: ChatCard[]; hide: boolean } {
  const slots = cards.filter((card): card is SlotCard => card.type === "slot");

  if (slots.length === 0) {
    // A prose-only turn that points at a grid which is not there. Only turns
    // that ask the patient to choose a *time* are affected; ordinary answers
    // and questions between options ("which Tuesday?") keep their text.
    return {
      cards,
      hide: asksToPickATime(content),
    };
  }

  // The slots decide: a turn carrying a grid is shown with its grid, whatever
  // its prose says. Blanking it into "ask me again" used to hide a reply the
  // cards already made clear, and sent the patient back to ask again.
  return { cards, hide: false };
}
