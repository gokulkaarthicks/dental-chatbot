/**
 * One turn in the thread: the text, then any cards, then the rating control.
 *
 * Memoised on its own props so a new turn does not re-render the whole history.
 * Cards are reconciled against the live proposal state at render time -- a card
 * persisted as "pending" in SQLite still renders correctly after the proposal
 * behind it was accepted or expired.
 */

import { memo, useState } from "react";
import { ThumbsDown, ThumbsUp } from "lucide-react";

import type { UIMessage } from "../../hooks/useChat";
import { reconcileTurn } from "../../lib/liveCards";
import type { CardHandlers } from "../cards/CardStack";
import { CardStack } from "../cards/CardStack";

type FeedbackRating = "up" | "down";
type FeedbackHandler = (
  messageId: number,
  rating: FeedbackRating,
  reason?: string,
) => Promise<boolean>;

function formatTime(iso?: string | null): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

/**
 * One turn in the thread.
 *
 * Messages sit flush against the thread rather than in bubbles: the clinic
 * speaks as plain text on the page, the patient's own words sit in a single
 * raised block with a tiny label above it. No tails, no avatars, no per-turn
 * shadows. The label also names the colour cue for anyone using a screen reader.
 *
 * Interactive cards render *outside* the message body because they act on real
 * database rows and need their own affordances.
 */
export const MessageBubble = memo(function MessageBubble({
  message,
  showTime,
  cardHandlers,
  onFeedback,
}: {
  message: UIMessage;
  showTime: boolean;
  cardHandlers?: CardHandlers;
  onFeedback?: FeedbackHandler;
}) {
  const isPatient = message.role === "patient";
  const [feedback, setFeedback] = useState<FeedbackRating | null>(null);
  const [askingReason, setAskingReason] = useState(false);
  const [reason, setReason] = useState("");
  const [sendingFeedback, setSendingFeedback] = useState(false);
  const [feedbackError, setFeedbackError] = useState(false);
  // Only the turn on screen is rateable: rating an old reply out of context is
  // not a signal staff can act on.
  const canRate =
    !isPatient &&
    showTime &&
    message.id !== null &&
    !message.pending &&
    !message.failed &&
    Boolean(onFeedback) &&
    feedback === null;
  // Older rows stored a "You" prefix on patient turns; the label below already
  // names the speaker, so drop it rather than printing it twice.
  const content = isPatient ? message.content.replace(/^\s*you\s*\n+/i, "") : message.content;
  // The cards decide what the turn may claim. A reply that asks the patient to
  // pick a time but carries no grid is shown as history only: there is nothing
  // to pick. A turn that does carry a grid is always shown with its prose,
  // because blanking it would send the patient back to ask again for what the
  // cards already answer. Patient turns are never touched.
  const turn = isPatient
    ? { cards: message.cards ?? [], hide: false }
    : reconcileTurn(content, message.cards ?? []);
  const cards = turn.cards;
  const hasSlots = cards.some((card) => card.type === "slot");
  let lines = content.split(/\n+/).filter((line) => line.trim().length > 0);
  if (turn.hide) {
    lines = [
      "Those times are no longer on screen. Ask me again for current availability and I'll check.",
    ];
  }
  // When structured slot cards carry the times, the assistant's prose is one
  // concise introduction. Older stored turns (and a model that ignored the
  // rule) may still contain a numbered time list; drop those lines here too so
  // the duplicate never appears. Time lists only: ordinary numbered answers on
  // turns without cards are untouched.
  if (hasSlots && lines.length > 1) {
    const timeLine = /\b\d{1,2}(:\d{2})?\s*([ap]\.?m\.?)\b/i;
    const marked = /^\s*(?:[-*•]|\d+[.)])\s+/;
    lines = lines.filter((line) => !(timeLine.test(line) && marked.test(line)));
  }
  const isList = lines.length > 1 && lines.every((line) => /^\s*\d+[.)]\s+/.test(line));

  async function sendRating(rating: FeedbackRating, note?: string) {
    if (!onFeedback || message.id === null || sendingFeedback) return;
    setSendingFeedback(true);
    setFeedbackError(false);
    const ok = await onFeedback(message.id, rating, note);
    setSendingFeedback(false);
    if (ok) {
      setFeedback(rating);
      setAskingReason(false);
    } else {
      setFeedbackError(true);
    }
  }

  return (
    <>
      {(lines.length > 0 || message.failed || message.pending) && (
        <article
          className={`msg ${isPatient ? "msg--patient" : "msg--clinic"}${
            message.pending ? " msg--pending" : ""
          }${message.failed ? " msg--failed" : ""}`}
          aria-label={isPatient ? "Your message" : "Receptionist"}
        >
          {isPatient && <p className="msg__author">You</p>}
          <div className="msg__body">
            {isList ? (
              <ol className="msg__list">
                {lines.map((line) => (
                  <li key={line}>{line.replace(/^\s*\d+[.)]\s+/, "")}</li>
                ))}
              </ol>
            ) : (
              lines.map((line, index) => <p key={`${index}-${line.slice(0, 12)}`}>{line}</p>)
            )}
          </div>
          {(showTime || message.failed || message.pending) && (
            <div className="msg__meta">
              {message.failed && <span className="msg__status">Not sent</span>}
              {message.pending && <span className="msg__status">Sending…</span>}
              {showTime && !message.pending && !message.failed && (
                <time dateTime={message.created_at ?? undefined}>
                  {formatTime(message.created_at)}
                </time>
              )}
            </div>
          )}

          {canRate && !askingReason && (
            <div className="msg__feedback">
              <span className="msg__feedback-label">Helpful?</span>
              <button
                type="button"
                className="msg__feedback-btn"
                onClick={() => void sendRating("up")}
                disabled={sendingFeedback}
                aria-label="This answer was helpful"
                title="Helpful"
              >
                <ThumbsUp size={14} strokeWidth={1.75} aria-hidden="true" />
              </button>
              <button
                type="button"
                className="msg__feedback-btn"
                onClick={() => setAskingReason(true)}
                disabled={sendingFeedback}
                aria-label="This answer was not helpful"
                title="Not helpful"
              >
                <ThumbsDown size={14} strokeWidth={1.75} aria-hidden="true" />
              </button>
            </div>
          )}

          {canRate && askingReason && (
            <form
              className="msg__feedback-form"
              onSubmit={(event) => {
                event.preventDefault();
                void sendRating("down", reason.trim() || undefined);
              }}
            >
              <label htmlFor={`feedback-${message.key}`} className="sr-only">
                What could have been better?
              </label>
              <input
                id={`feedback-${message.key}`}
                className="msg__feedback-input"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                placeholder="What could have been better? (optional)"
                maxLength={500}
                autoFocus
              />
              <button type="submit" className="msg__feedback-send" disabled={sendingFeedback}>
                Send
              </button>
              <button
                type="button"
                className="msg__feedback-skip"
                onClick={() => void sendRating("down")}
                disabled={sendingFeedback}
              >
                Skip
              </button>
            </form>
          )}

          {feedback !== null && (
            <p className="msg__feedback-thanks" role="status">
              Thanks, that helps the front desk.
            </p>
          )}
          {feedbackError && (
            <p className="msg__feedback-thanks" role="alert">
              That did not send. You can try again.
            </p>
          )}
        </article>
      )}
      {cards.length > 0 && cardHandlers && (
        <div className="msg__cards">
          <CardStack cards={cards} handlers={cardHandlers} messageKey={message.key} />
        </div>
      )}
    </>
  );
});

/** Thinking indicator: three dots plus a label, sized like a message line. */
export function TypingIndicator({ label }: { label: string }) {
  return (
    <div className="typing" role="status" aria-live="polite">
      <span className="typing__dots" aria-hidden="true">
        <i />
        <i />
        <i />
      </span>
      <span>{label}</span>
    </div>
  );
}
