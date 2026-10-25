/**
 * The chat state machine.
 *
 * One hook owns the whole conversation lifecycle so the components stay
 * presentational: bootstrap (session + thread), restore, send with optimistic
 * user message, error recovery, new conversation, and demo-reset invalidation.
 *
 * Persistence contract
 * --------------------
 * The transcript lives in SQLite, never in React state alone. The browser keeps
 * only two pointers (session token, active conversation id) in `sessionStore`,
 * and a mount *always* restores from them instead of creating anything:
 *
 *   1. compare the stored reset generation (a demo reset invalidates the rest);
 *   2. `GET /chat/conversations/{id}` replays the stored messages, in order;
 *   3. re-read pending proposals so a held booking is clickable again;
 *   4. only when there is nothing valid to restore, create a session.
 *
 * "New chat" is the single path that creates a conversation, and it leaves the
 * previous one in SQLite. React 18 StrictMode mounts twice in dev, so the boot
 * is guarded by a module-level promise rather than an effect flag: both mounts
 * await the same restore and the server sees one session, not two.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  api,
  chatApi,
  schedulingApi,
  type ChatMessage,
  type ChatSessionState,
} from "../api/client";
import type { ChatCard } from "../lib/cards";
import { seedClinicName } from "../lib/clinic";
import { sessionStore } from "../lib/sessionStore";
import { useCardActions, type UIMessageLike } from "./useCardActions";
import { useClinicName } from "./useClinicName";

/**
 * The stored greeting is identical every time, so give it a stable key instead
 * of a per-mount one. (React keys only need to be unique within the list.)
 */
function storedKey(message: ChatMessage, index: number): string {
  if (message.id != null) return `m-${message.id}`;
  return `m-restored-${index}`;
}

function restoredToUI(messages: ChatMessage[]): UIMessage[] {
  return messages.map((message, index) => ({
    ...message,
    key: storedKey(message, index),
  }));
}

/**
 * Reconcile a restored thread with what is actionable now.
 *
 * The transcript is history and must stay visible, but a proposal card in it is
 * only a button while the server still reports that proposal as pending. Every
 * proposal card outside `live` loses its actions, so a booked, superseded or
 * expired card can never be clicked into a second booking.
 *
 * A turn that was persisted without its cards (a crash between the reply and
 * the card write) is repaired the same way: if the newest assistant message has
 * no cards while the server reports a pending proposal, the card is re-attached
 * there so the active proposal is not silently lost on refresh.
 */
export function markProposalsResolved(
  messages: UIMessage[],
  live: ChatCard[] | null,
): UIMessage[] {
  if (!live) return messages;
  const pending = new Set(
    live
      .filter((card): card is Extract<ChatCard, { proposal_id: string }> => "proposal_id" in card)
      .map((card) => card.proposal_id),
  );
  return messages.map((message) => {
    if (!message.cards?.length) return message;
    let touched = false;
    const cards = message.cards.map((card) => {
      if (!("proposal_id" in card) || card.type === "confirmation") return card;
      if (pending.has(card.proposal_id)) return card;
      if (card.type !== "proposal" && card.type !== "family_proposal") return card;
      if (card.actions.length === 0 && card.status !== "pending") return card;
      touched = true;
      return { ...card, status: "superseded", actions: [] } as ChatCard;
    });
    return touched ? { ...message, cards } : message;
  });
}

/**
 * Re-attach live proposals that the persisted transcript did not carry.
 *
 * Only the newest assistant turn is considered: appending to an older turn
 * would present the proposal out of context. It is re-attached only while that
 * turn has no live proposal card yet; once the thread carries a live card, a
 * later reply (a follow-up question) must not keep re-offering old proposals.
 */
export function attachLiveProposals(
  messages: UIMessage[],
  live: ChatCard[] | null,
): UIMessage[] {
  if (!live || live.length === 0) return messages;
  const proposals = live.filter((card) => "proposal_id" in card && card.type !== "confirmation");
  if (proposals.length === 0) return messages;
  const liveIds = new Set(
    proposals.map((card) => (card as { proposal_id: string }).proposal_id),
  );

  let target = -1;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (message.role !== "assistant" || message.pending || message.failed) continue;
    target = i;
    break;
  }
  if (target === -1) return messages;

  const message = messages[target];
  const carried = (message.cards ?? []).filter(
    (card): card is Extract<ChatCard, { proposal_id: string }> =>
      "proposal_id" in card && card.type !== "confirmation",
  );
  // Already presented in the newest turn: nothing to repair.
  if (carried.some((card) => liveIds.has(card.proposal_id))) return messages;

  const next = messages.slice();
  next[target] = { ...message, cards: [...(message.cards ?? []), ...proposals] };
  return next;
}

export type ChatStatus = "booting" | "ready" | "error";

export type UIMessage = ChatMessage & {
  /** Client-side id so optimistic messages can be reconciled safely. */
  key: string;
  pending?: boolean;
  failed?: boolean;
  cards?: ChatCard[];
};

/** A banner above the thread. Warnings (failed sends) outrank info notes. */
export type ChatNotice = { text: string; level: "info" | "warning" };

const UNVERIFIED: ChatSessionState = {
  verified: false,
  registration_complete: false,
  assistant_configured: true,
};

const RESET_NOTICE = "The demo data was reset, so we started a fresh chat.";

function isSessionInvalid(error: unknown): boolean {
  // 404: the token is unknown (reset, expired, or deleted).
  // 401: no token was sent at all.
  return error instanceof ApiError && (error.status === 404 || error.status === 401);
}

function greetingMessage(text: string): UIMessage {
  return { key: "greeting", id: null, role: "assistant", content: text };
}

type BootOutcome = { ok: true } | { ok: false; error: string };

/**
 * What one page-load restore settled on, captured outside React state so a
 * second mount can render it before its own first paint, without fetching.
 */
type BootSnapshot = {
  ok: boolean;
  error?: string;
  token: string | null;
  conversationId: string | null;
  messages: UIMessage[];
  session: ChatSessionState;
  suggestions: string[];
};

/**
 * One restore per page load, even though React 18 StrictMode mounts the hook
 * twice in development. The promise is shared, so the second mount waits for
 * the same server round-trips instead of minting a second session, and the
 * snapshot lets it render what the first mount resolved.
 */
let bootPromise: Promise<BootSnapshot> | null = null;

/**
 * The thread the running restore produced: the server payload plus the live
 * proposals it reconciled against. Written as the restore runs, then frozen by
 * `snapshotThread` so a remount renders exactly the same list.
 */
let bootedMessages: UIMessage[] = [];
let bootedSession: ChatSessionState | null = null;
/** Starter chips the restore settled on (hidden by the UI after a message). */
let bootedSuggestions: string[] = [];

/** Reset the guard so the next bootstrap really re-restores from the server. */
function resetBootGuard(): void {
  bootPromise = null;
}

function snapshotThread(result: BootOutcome): BootSnapshot {
  if (!result.ok) {
    return {
      ok: false,
      error: result.error,
      token: null,
      conversationId: null,
      messages: [],
      session: UNVERIFIED,
      suggestions: [],
    };
  }
  return {
    ok: true,
    token: sessionStore.token(),
    conversationId: sessionStore.activeConversation(),
    messages: bootedMessages,
    session: bootedSession ?? UNVERIFIED,
    suggestions: bootedSuggestions,
  };
}

export function useChat() {
  const [status, setStatus] = useState<ChatStatus>("booting");
  const [fatalError, setFatalError] = useState<string | null>(null);
  const [messages, setMessages] = useState<UIMessage[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [session, setSession] = useState<ChatSessionState>(UNVERIFIED);
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [sending, setSending] = useState(false);
  const [notice, setNotice] = useState<ChatNotice | null>(null);
  // Only a seed: the header reads the shared clinic configuration, and this is
  // confirmed by the session payload below.
  const configuredName = useClinicName();

  const tokenRef = useRef<string | null>(null);
  const conversationRef = useRef<string | null>(null);
  const activeRequest = useRef<AbortController | null>(null);
  // Mirrors `sending` for guards inside async callbacks. State alone is stale
  // across awaits, which would allow a double-send to slip through.
  const sendingRef = useRef(false);

  // Forward declaration: `send` reports the new turn's cards to the card layer,
  // which is created further down (it needs `send`). Both live in refs so the
  // callbacks stay stable and do not re-create each other.
  const resyncRef = useRef<(options?: { force?: boolean; ignoreProposalIds?: string[] }) => void>(
    () => undefined,
  );
  const markLiveProposalsRef = useRef<(cards: ChatCard[]) => void>(() => undefined);
  const noteScheduleSearchedRef = useRef<() => void>(() => undefined);

  conversationRef.current = conversationId;

  const beginRequest = useCallback(() => {
    if (sendingRef.current) return false;
    sendingRef.current = true;
    setSending(true);
    return true;
  }, []);

  const endRequest = useCallback(() => {
    sendingRef.current = false;
    setSending(false);
  }, []);

  const note = useCallback((text: string, level: ChatNotice["level"] = "info") => {
    const next: ChatNotice = { text, level };
    setNotice((current) =>
      current && current.level === "warning" && level === "info" ? current : next,
    );
  }, []);

  /**
   * Adopt a conversation payload as the thread on screen *and* as the pointer
   * the next refresh will follow. Every restore and creation path goes through
   * here, so the id shown is always the id stored.
   */
  const applyConversation = useCallback(
    (payload: {
      conversation_id: string;
      messages: ChatMessage[];
      session: ChatSessionState;
    }) => {
      const token = tokenRef.current;
      if (token) sessionStore.rememberSession(token, payload.conversation_id);
      else sessionStore.setActiveConversation(payload.conversation_id);
      const next = restoredToUI(payload.messages);
      setConversationId(payload.conversation_id);
      setMessages(next);
      setSession(payload.session);
      // A restore is still "booting" here; record what the guard should hand a
      // second mount once the reconcile below has run.
      bootedMessages = next;
      bootedSession = payload.session;
    },
    [],
  );

  /**
   * Restore a stored thread.
   *
   * The payload's own cards come first (they were persisted with their turn),
   * then the server's still-pending proposals decide which of those cards stay
   * clickable. A proposal that was live but missing from the transcript is
   * re-attached to the newest turn, so a refresh never loses the hold the
   * patient is looking at.
   */
  const applyRestoredConversation = useCallback(
    async (
      token: string,
      payload: {
        conversation_id: string;
        messages: ChatMessage[];
        session: ChatSessionState;
        pending_proposals?: ChatCard[];
      },
    ) => {
      applyConversation(payload);

      let live = payload.pending_proposals ?? null;
      if (!live) {
        const pending = await schedulingApi
          .pending(token, payload.conversation_id)
          .catch(() => null);
        live = pending?.proposals ?? null;
      }
      if (!live) return;

      // Resolved from the payload itself (not a state updater) so the guard's
      // snapshot is available before React has committed the first paint.
      const reconciled = attachLiveProposals(markProposalsResolved(bootedMessages, live), live);
      bootedMessages = reconciled;
      setMessages(reconciled);
      markLiveProposalsRef.current(live);
    },
    [applyConversation],
  );

  /**
   * Create a new session. Only reached when there is nothing to restore: first
   * visit, a demo reset, or a token the server no longer knows.
   *
   * A stored conversation id is offered as a resume hint, which lets a browser
   * that lost its token (server restart, cleared storage) pick its thread back
   * up — but the backend only hands it over to a session verified as the same
   * patient, so this is never a way around the token.
   */
  const startFreshSession = useCallback(
    async (resetDetected = false) => {
      // The hint is this tab's own pointer, so resuming can only ever pick up
      // a thread this tab was already in (an expired token after a server
      // restart, for example). Another tab's session is never offered.
      const hint = resetDetected ? null : sessionStore.activeConversation();
      const created = await chatApi.startSession(undefined, {
        resumeConversationId: hint ?? undefined,
      });
      const resumed = Boolean(hint) && created.conversation_id === hint;

      // The wire payload for a newly created conversation only carries the
      // greeting; read the stored thread back so the browser shows what SQLite
      // holds (and so the next refresh renders the identical list).
      const detail = await chatApi
        .getConversation(created.session_token, created.conversation_id)
        .catch(() => null);

      // Re-point the store at this conversation *before* applying it, so the
      // pointers and the rendered thread are written together.
      tokenRef.current = created.session_token;
      sessionStore.rememberSession(created.session_token, created.conversation_id);
      sessionStore.setGeneration(created.reset_generation);
      sessionStore.clearResetNotice();
      seedClinicName(created.clinic_name);
      setSession(detail?.session ?? created.session);
      bootedSuggestions = detail?.suggestions ?? created.suggestions;
      setSuggestions(bootedSuggestions);

      if (detail) {
        await applyRestoredConversation(created.session_token, detail);
      } else {
        const greeting = [greetingMessage(created.greeting)];
        bootedMessages = greeting;
        bootedSession = created.session;
        setConversationId(created.conversation_id);
        setMessages(greeting);
      }

      if (resumed) {
        note("Welcome back. Here's where you left off.");
      } else if (hint && !resetDetected) {
        note("Your previous chat is no longer available. Here's a new one.");
      }
      if (resetDetected) note(RESET_NOTICE, "info");
    },
    [applyRestoredConversation, note],
  );

  /** Attempt the stored session + thread. Returns false when it is unusable. */
  const restoreStoredSession = useCallback(async (): Promise<boolean> => {
    const token = sessionStore.token();
    if (!token) return false;

    const storedThread = sessionStore.activeConversation();
    // No pointer means this tab has no thread of its own (a fresh tab, or one
    // whose own thread this tab no longer remembers). Never go looking for the
    // patient's most recent thread: that belongs to another tab.
    if (!storedThread) return false;
    try {
      const detail = await chatApi.getConversation(token, storedThread);
      tokenRef.current = token;
      await applyRestoredConversation(token, detail);
      bootedSuggestions = detail.messages.length <= 1 ? detail.suggestions : [];
      setSuggestions(bootedSuggestions);
      return true;
    } catch (error) {
      if (!isSessionInvalid(error)) throw error;
      // The thread this tab remembered is gone (a demo reset, or a deleted
      // conversation). Start this tab fresh instead of adopting the session's
      // newest thread, which may belong to another tab.
      sessionStore.clear();
      return false;
    }
  }, [applyRestoredConversation]);

  /**
   * Restore the stored session, falling back to a new one when invalid.
   * Returns the outcome so the module-level boot guard can replay it for a
   * second StrictMode mount without a second network round-trip.
   */
  const runBootstrap = useCallback(
    async (): Promise<BootOutcome> => {
    try {
      // A demo reset deletes every session and conversation server-side, so the
      // stored token would 404 anyway; checking the generation first lets us
      // explain why instead of silently starting over.
      let resetDetected = false;
      try {
        const statusPayload = await api.demoStatus();
        const stored = sessionStore.generation();
        if (stored && stored !== statusPayload.reset_generation) {
          sessionStore.clear();
          // Remembered so the explanation survives the reload that detected it.
          sessionStore.setResetNotice(RESET_NOTICE);
          resetDetected = true;
        }
      } catch {
        // Status is best-effort; the session lookup below is authoritative.
      }

      if (!resetDetected && (await restoreStoredSession())) {
        const pendingReset = sessionStore.resetNotice();
        if (pendingReset) {
          note(pendingReset, "info");
          sessionStore.clearResetNotice();
        }
        return { ok: true };
      }

      await startFreshSession(resetDetected);
      return { ok: true };
    } catch (error) {
      const message = error instanceof Error ? error.message : "Could not start the chat.";
      return { ok: false, error: message };
    }
    },
    [note, restoreStoredSession, startFreshSession],
  );

  /**
   * Restore (or create) the session for this page load. The promise is shared
   * across React 18 StrictMode's double mount, so the server sees one restore;
   * the second mount renders the first mount's resolved thread verbatim.
   */
  const bootstrap = useCallback(async () => {
    setStatus("booting");
    setFatalError(null);
    if (!bootPromise) {
      bootedMessages = [];
      bootedSession = null;
      bootedSuggestions = [];
      bootPromise = runBootstrap().then(snapshotThread);
    }
    const snapshot = await bootPromise;
    if (!snapshot.ok) {
      setFatalError(snapshot.error ?? "Could not start the chat.");
      setStatus("error");
      return;
    }
    tokenRef.current = snapshot.token;
    setConversationId(snapshot.conversationId);
    setMessages(snapshot.messages);
    setSession(snapshot.session);
    setSuggestions(snapshot.suggestions);
    setStatus("ready");
  }, [runBootstrap]);

  useEffect(() => {
    void bootstrap();
  }, [bootstrap]);

  const send = useCallback(
    async (rawText: string) => {
      const text = rawText.trim();
      const token = tokenRef.current;
      const thread = conversationRef.current;
      if (!text || !thread || !token) return;
      if (!beginRequest()) return;
      // Any message may lead to a schedule search; if this turn renders a grid,
      // that grid is an offer made now rather than restored history.
      noteScheduleSearchedRef.current();

      setNotice(null);
      setSuggestions([]);

      const optimistic: UIMessage = {
        key: `local-${Date.now()}`,
        id: null,
        role: "patient",
        content: text,
        pending: true,
      };
      setMessages((current) => [...current, optimistic]);

      const controller = new AbortController();
      activeRequest.current = controller;

      try {
        const response = await chatApi.sendMessage(token, thread, text, {
          signal: controller.signal,
        });
        setMessages(restoredToUI(response.messages));
        setSession(response.session);
        // The newest turn's cards are the authoritative actionable set; the
        // re-sync refreshes the availability grid behind them.
        markLiveProposalsRef.current(response.cards ?? []);
        resyncRef.current({ force: true });
        if (response.degraded) {
          setNotice({
            text: "The assistant had trouble with that message. Please try again.",
            level: "warning",
          });
        }
      } catch (error) {
        if (isSessionInvalid(error)) {
          // Session or thread vanished (demo reset, restart). Re-onboard rather
          // than leaving the user stuck on a dead thread. The stored transcript
          // is already server-side; a live session resumes the same id.
          sessionStore.clear();
          resetBootGuard();
          try {
            await startFreshSession(true);
            setStatus("ready");
          } catch {
            setFatalError("Your chat session expired. Please reload the page.");
            setStatus("error");
          }
        } else {
          setMessages((current) =>
            current.map((item) =>
              item.key === optimistic.key ? { ...item, pending: false, failed: true } : item,
            ),
          );
          setNotice({
            text: error instanceof Error ? error.message : "Message failed to send.",
            level: "warning",
          });
        }
      } finally {
        activeRequest.current = null;
        endRequest();
      }
    },
    [beginRequest, endRequest, startFreshSession],
  );

  const retryLast = useCallback(() => {
    const failed = [...messages].reverse().find((item) => item.failed);
    if (!failed) return;
    setMessages((current) => current.filter((item) => item.key !== failed.key));
    void send(failed.content);
  }, [messages, send]);

  /**
   * Start over in this tab. The previous thread stays in SQLite as a record for
   * the front desk (it is never closed or deleted), but this browser stores the
   * new conversation id, so the next refresh restores the *new* thread.
   */
  const newConversation = useCallback(async () => {
    const token = tokenRef.current;
    if (!token) return;
    if (!beginRequest()) return;
    setNotice(null);
    try {
      const created = await chatApi.startConversation(token);
      // The start response and a later restore must produce the same list, so
      // both go through the same mapper (and both read the stored transcript).
      const stored =
        (await chatApi.getConversation(token, created.conversation_id).catch(() => null)) ??
        created;
      applyConversation(stored);
      setSuggestions(stored.suggestions);
    } catch (error) {
      if (isSessionInvalid(error)) {
        sessionStore.clear();
        resetBootGuard();
        await bootstrap();
      } else {
        setNotice({
          text: error instanceof Error ? error.message : "Could not start a new chat.",
          level: "warning",
        });
      }
    } finally {
      endRequest();
    }
  }, [applyConversation, beginRequest, bootstrap, endRequest]);

  const reload = useCallback(async () => {
    resetBootGuard();
    await bootstrap();
  }, [bootstrap]);

  const showSuggestions = useMemo(
    () => messages.filter((item) => item.role === "patient").length === 0,
    [messages],
  );

  // Card actions post to deterministic endpoints; they share `send` so a
  // "Change" request becomes a normal assistant turn.
  const card = useCardActions({
    token: () => tokenRef.current,
    conversationId,
    send,
    setMessages: setMessages as (updater: (current: UIMessageLike[]) => UIMessageLike[]) => void,
    setNotice,
    isBusy: () => sendingRef.current,
    messages,
    // Until a turn runs, the thread on screen is what the server restored, so
    // its slot grid is history to re-check, not an offer this client just made.
    restored: !messages.some((message) => message.role === "patient"),
  });

  // Keep the card layer's view of the newest slot grid current, and lock any
  // proposal card the thread no longer has a live counterpart for.
  const { trackAvailability, markLiveProposals, resync, noteScheduleSearched } = card;
  // Read inside `send` without making it depend on the identity of a callback
  // that is re-created whenever the hook re-renders.
  noteScheduleSearchedRef.current = noteScheduleSearched;
  useEffect(() => {
    resyncRef.current = resync;
    markLiveProposalsRef.current = markLiveProposals;
  }, [markLiveProposals, resync]);
  useEffect(() => {
    trackAvailability(messages);
    markLiveProposals(messages.flatMap((message) => message.cards ?? []));
  }, [markLiveProposals, messages, trackAvailability]);

  return {
    status,
    fatalError,
    messages,
    conversationId,
    session,
    suggestions: showSuggestions ? suggestions : [],
    sending,
    notice,
    clinicName: configuredName,
    send,
    retryLast,
    newConversation,
    reload,
    dismissNotice: () => setNotice(null),
    ...card,
  };
}
