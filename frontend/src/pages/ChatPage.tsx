import { useEffect, useMemo, useRef } from "react";
import { CircleDot, MessageSquarePlus, X } from "lucide-react";

import type { CardHandlers } from "../components/cards/CardStack";
import { Button } from "../components/Button";
import { Composer } from "../components/chat/Composer";
import { MessageBubble, TypingIndicator } from "../components/chat/MessageBubble";
import { ErrorBlock, LoadingBlock } from "../components/Loading";
import { useChat } from "../hooks/useChat";

const STATUS_LABEL: Record<string, string> = {
  booting: "Connecting",
  ready: "Online",
  error: "Offline",
};

/**
 * Patient chat page — one thread per browser tab.
 *
 * The tab session (token + open conversation) lives in sessionStorage, so a
 * new tab is a new patient and a reload keeps the thread on screen. There is
 * no conversation list: history is the scrollback above the composer, and
 * "New" archives this thread for the front desk and starts a fresh one.
 *
 * Layout contract: a fixed-height flex column. Header and composer never
 * scroll; only `.chat__thread` has `overflow-y: auto`.
 */
export function ChatPage() {
  const chat = useChat();
  const threadRef = useRef<HTMLDivElement | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  const lastMessageCount = chat.messages.length;

  const {
    busyAction,
    busySlotId,
    handleProposalAction,
    holdFamilyOption,
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
    }),
    [
      busyAction,
      busySlotId,
      handleProposalAction,
      holdFamilyOption,
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

  return (
    <div className="chat">
      <header className="chat__header">
        <div className="chat__brand">
          <span className="chat__mark" aria-hidden="true">
            <CircleDot size={16} strokeWidth={2} />
          </span>
          <h1 className="chat__title">{chat.clinicName}</h1>
          <span className={`chat__status chat__status--${chat.status}`}>
            <span className="chat__dot" aria-hidden="true" />
            {STATUS_LABEL[chat.status] ?? "Online"}
          </span>
        </div>
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
                <X size={14} strokeWidth={1.75} />
              </button>
            </div>
          )}

          {chat.status === "booting" && <LoadingBlock label="Opening the front desk…" />}

          {chat.status === "error" && (
            <ErrorBlock message={chat.fatalError ?? "The chat could not start."} onRetry={chat.reload} />
          )}

          {chat.status === "ready" && (
            <div className="chat__messages" aria-live="polite" aria-relevant="additions">
              {chat.messages.map((message, index) => (
                <MessageBubble
                  key={message.key}
                  message={message}
                  showTime={index === chat.messages.length - 1 || Boolean(message.failed)}
                  cardHandlers={cardHandlers}
                />
              ))}

              {chat.sending && <TypingIndicator label={`${chat.clinicName.split(" ")[0]} is typing`} />}

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
  );
}
