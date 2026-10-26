/**
 * The patient front desk: the whole conversation on one page.
 *
 * Layout is a practice-information sidebar plus the thread, with the composer
 * pinned to the bottom. This page owns only layout and the send path; the turn
 * lifecycle lives in `useChat`, card action routing in `useCardActions`, and
 * card rendering in `components/cards`. Nothing here decides scheduling state:
 * every action posts to a deterministic endpoint and waits for the answer.
 */

import { useEffect, useMemo, useRef } from "react";
import {
  CircleDot,
  Clock3,
  MapPin,
  MessageSquarePlus,
  Phone,
  ShieldCheck,
} from "lucide-react";

import type { CardHandlers } from "../components/cards/CardStack";
import { Button } from "../components/Button";
import { Composer } from "../components/chat/Composer";
import { MessageBubble, TypingIndicator } from "../components/chat/MessageBubble";
import { ErrorBlock, LoadingBlock } from "../components/Loading";
import { useChat } from "../hooks/useChat";
import { useClinicInfo } from "../hooks/useClinicName";
import type { ClinicResponse } from "../api/client";

const QUICK_PROMPTS = [
  "I'd like to book a cleaning",
  "I have tooth pain",
  "What insurance do you take?",
  "Where are you and when are you open?",
];

const WEEK = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function formatClock(value: string): string {
  const [hour, minute] = value.split(":").map(Number);
  const suffix = hour >= 12 ? "PM" : "AM";
  return `${hour % 12 || 12}:${String(minute).padStart(2, "0")} ${suffix}`;
}

/** Compact opening-hours line derived from the clinic configuration. */
function hoursLabel(clinic: ClinicResponse | null): string {
  const hours = clinic?.business_hours;
  if (!hours) return "Mon-Sat, 8:00 AM - 6:00 PM";
  const open = Object.entries(hours).filter(([, value]) => value.open && value.close);
  if (!open.length) return "Call for current hours";
  const indexes = open.map(([day]) => WEEK.indexOf(day));
  const contiguous = indexes.every((value, index) => index === 0 || value === indexes[index - 1] + 1);
  const consistent = open.every(
    ([, value]) => value.open === open[0][1].open && value.close === open[0][1].close,
  );
  if (!contiguous || !consistent) return "See current hours";
  const first = open[0][1];
  return `${open[0][0]}-${open[open.length - 1][0]}, ${formatClock(first.open!)} - ${formatClock(first.close!)}`;
}

/**
 * Patient chat page: one thread per browser tab.
 *
 * The tab session (token + open conversation) lives in sessionStorage, so a
 * new tab is a new patient and a reload keeps the thread on screen. There is
 * no conversation list: history is the scrollback above the composer, and
 * "New" archives this thread for the front desk and starts a fresh one.
 *
 * Layout contract: a fixed-height two-column shell. The practice sidebar is
 * desktop chrome; the conversation is the page. Header and composer never
 * scroll; only `.chat__thread` has `overflow-y: auto`.
 */
export function ChatPage() {
  const chat = useChat();
  const clinic = useClinicInfo();
  const threadRef = useRef<HTMLDivElement | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  const lastMessageCount = chat.messages.length;

  const {
    busyAction,
    busySlotId,
    handleProposalAction,
    holdFamilyOption,
    isGridSettled,
    isProposalEnabled,
    isSlotEnabled,
    selectSlot,
    sending,
  } = chat;

  const cardHandlers = useMemo<CardHandlers>(
    () => ({
      onSelectSlot: (slot) => void selectSlot(slot),
      onProposalAction: (action, card) => void handleProposalAction(action, card),
      onHoldFamily: (card) => void holdFamilyOption(card),
      busySlotId,
      busyAction,
      // Any in-flight card action locks every card, which prevents a confirm
      // racing a slot click on the same thread.
      disabled: sending || busySlotId !== null || busyAction !== null,
      // Only the newest grid and still-pending proposals are clickable; older
      // cards stay visible as history.
      isSlotEnabled,
      isProposalEnabled,
      isGridSettled,
    }),
    [
      busyAction,
      busySlotId,
      handleProposalAction,
      holdFamilyOption,
      isGridSettled,
      isProposalEnabled,
      isSlotEnabled,
      selectSlot,
      sending,
    ],
  );

  useEffect(() => {
    // Keep the newest message in view without fighting a user who scrolled up
    // to read: only autoscroll when already near the bottom.
    const container = threadRef.current;
    if (!container) return;
    const nearBottom =
      container.scrollHeight - container.scrollTop - container.clientHeight < 160;
    if (nearBottom) {
      bottomRef.current?.scrollIntoView({ block: "end", behavior: "smooth" });
    }
  }, [lastMessageCount, chat.sending]);

  const busy = chat.sending || chat.status !== "ready";
  const showPrompts = chat.status === "ready" && chat.messages.length <= 1 && !sending;
  const address = clinic?.address;
  const phone = clinic?.phone;
  const insurancePlans =
    clinic?.insurance?.accepted_plans?.map((plan) => plan.name).filter(Boolean) ?? [];
  const insuranceLine = insurancePlans.length
    ? insurancePlans.join(" · ")
    : "Ask about your plan at the front desk";

  return (
    <div className="chat">
      <aside className="practice" aria-label="Practice information">
        <div className="practice__brand">
          <span className="practice__mark" aria-hidden="true">
            <CircleDot size={18} strokeWidth={2} />
          </span>
          <div>
            <p className="practice__eyebrow">Front desk</p>
            <h1 className="practice__name">{chat.clinicName}</h1>
          </div>
        </div>

        <p className="practice__lede">
          Message us like you would call. We can book, move, or cancel a visit and answer
          insurance and pricing questions.
        </p>

        <div className="practice__facts">
          <div className="practice__fact">
            <Clock3 size={15} strokeWidth={1.8} aria-hidden="true" />
            <div>
              <span className="practice__label">Hours</span>
              <span>{hoursLabel(clinic)}</span>
            </div>
          </div>
          {address && (
            <div className="practice__fact">
              <MapPin size={15} strokeWidth={1.8} aria-hidden="true" />
              <div>
                <span className="practice__label">Location</span>
                <span>
                  {address.line1}
                  <br />
                  {address.city}, {address.state} {address.postal_code}
                </span>
              </div>
            </div>
          )}
          {phone && (
            <div className="practice__fact">
              <Phone size={15} strokeWidth={1.8} aria-hidden="true" />
              <div>
                <span className="practice__label">Prefer to call?</span>
                <a href={`tel:${phone.replace(/[^+\d]/g, "")}`}>{phone}</a>
              </div>
            </div>
          )}
        </div>

        <div className="practice__insurance">
          <span className="practice__label">
            <ShieldCheck size={13} strokeWidth={1.8} aria-hidden="true" /> In-network
          </span>
          <p>{insuranceLine}</p>
          <p className="practice__muted">
            No insurance? Ask about self-pay, our membership, or financing.
          </p>
        </div>

        <div className="practice__foot">
          <a href="/staff">Staff dashboard</a>
        </div>
      </aside>

      <div className="chat__main">
        <header className="chat__header">
          <div className="chat__header-side">
            <Button
              variant="ghost"
              size="sm"
              icon={<MessageSquarePlus size={15} strokeWidth={1.75} />}
              onClick={() => void chat.newConversation()}
              disabled={busy}
              title="End this thread and start a new one"
            >
              New chat
            </Button>
          </div>
        </header>

        <main className="chat__thread" ref={threadRef}>
          <div className="chat__column">
            {chat.notice && (
              <div
                className={`chat__notice${
                  chat.notice.level === "warning" ? " chat__notice--warning" : ""
                }`}
                role={chat.notice.level === "warning" ? "alert" : "status"}
              >
                <span>{chat.notice.text}</span>
                <button
                  type="button"
                  className="chat__notice-dismiss"
                  onClick={chat.dismissNotice}
                  aria-label="Dismiss notice"
                >
                  ×
                </button>
              </div>
            )}

            {chat.status === "booting" && <LoadingBlock label="Opening the front desk…" />}

            {chat.status === "error" && (
              <ErrorBlock
                message={chat.fatalError ?? "The chat could not start."}
                onRetry={chat.reload}
              />
            )}

            {chat.status === "ready" && (
              <div className="chat__messages" aria-live="polite" aria-relevant="additions">
                {chat.messages.map((message, index) => (
                  <MessageBubble
                    key={message.key}
                    message={message}
                    showTime={index === chat.messages.length - 1 || Boolean(message.failed)}
                    cardHandlers={cardHandlers}
                    onFeedback={chat.submitFeedback}
                  />
                ))}

                {showPrompts && (
                  <div className="prompts" aria-label="Common requests">
                    {QUICK_PROMPTS.map((prompt) => (
                      <button
                        key={prompt}
                        type="button"
                        className="prompts__chip"
                        onClick={() => void chat.send(prompt)}
                      >
                        {prompt}
                      </button>
                    ))}
                  </div>
                )}

                {chat.sending && (
                  <TypingIndicator label={`${chat.clinicName.split(" ")[0]} is typing`} />
                )}

                {!chat.session.assistant_configured && (
                  <p className="chat__inline-note" role="note">
                    The AI assistant is not configured on this server. You can still leave your
                    details and the front desk will follow up.
                  </p>
                )}

                {chat.messages.some((item) => item.failed) && (
                  <div className="chat__retry">
                    <Button onClick={chat.retryLast}>Retry last message</Button>
                  </div>
                )}
              </div>
            )}
            <div ref={bottomRef} />
          </div>
        </main>

        <div className="chat__composer-wrap">
          <div className="chat__column">
            <Composer
              onSend={(text) => void chat.send(text)}
              sending={chat.sending}
              disabled={chat.status !== "ready" || !chat.conversationId}
              placeholder={
                chat.session.verified
                  ? "Ask about times, insurance, or your visit…"
                  : "Tell us what you need: a cleaning, a question, or a toothache…"
              }
            />
          </div>
        </div>
      </div>
    </div>
  );
}
