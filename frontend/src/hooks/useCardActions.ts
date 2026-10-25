/**
 * Interactive-card behaviour.
 *
 * Kept separate from `useChat` so the conversation state machine stays readable
 * and so card actions can depend on `send` without a circular reference.
 *
 * The invariant every action here relies on: the server is the source of truth.
 * A click never mutates local state into "booked"; it posts, then renders the
 * cards the server returns. Local mutation happens only for the narrow case of
 * replacing a pending proposal card with its confirmation.
 *
 * Staleness
 * ---------
 * The transcript is history, so cards stay visible. But only the *current*
 * search's slots and the proposals the server still reports as pending are
 * actionable. After any booking, reschedule, or cancellation (and when the tab
 * regains focus) the hook re-reads `/scheduling/pending` and the availability
 * grid that owns the newest slot card, then:
 *
 *  - disables slot options that are taken, superseded by a newer grid, or whose
 *    grid has been refreshed (unless the server still offers them);
 *  - disables proposal cards the server no longer lists as pending;
 *  - forgets a cached availability query whose data has gone stale.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { schedulingApi } from "../api/client";
import type {
  ChatCard,
  FamilyOptionCard,
  ProposalActions,
  ProposalCard,
  SlotCard,
} from "../lib/cards";

export type CardNotice = { text: string; level: "info" | "warning" };

export type UIMessageLike = {
  key: string;
  id: number | null;
  role: "patient" | "assistant";
  content: string;
  cards?: ChatCard[];
};

/** The constraints that produced a grid, so it can be re-run after a change. */
export type CachedAvailability = {
  appointmentType: string;
  patientFirstName?: string | null;
  /** Slot ids the server offered when the grid was rendered. */
  slotIds: string[];
  /**
   * The meaning of the grid. A replacement grid for a pending move must be
   * re-checked with its target (and its visit's own type), so the fresh cards
   * still say "reschedule" and a click continues the move.
   */
  intent?: "new_booking" | "reschedule";
  targetAppointmentId?: number | null;
  /** A second visit the patient explicitly requested, not a stale alternative. */
  additionalVisit?: boolean;
};

/** A rendered grid plus the message that holds it. */
type GridSnapshot = CachedAvailability & { key: string };

/** Marker for "a search ran this turn; the next grid rendered is its answer". */
const SEARCH_PENDING = "*";

type Options = {
  token: () => string | null;
  conversationId: string | null;
  send: (text: string) => Promise<void>;
  setMessages: (updater: (current: UIMessageLike[]) => UIMessageLike[]) => void;
  setNotice: (notice: CardNotice | null) => void;
  /** Optional: lets the card layer pause work while a turn is in flight. */
  isBusy?: () => boolean;
  /** The rendered thread, so a refresh can tell superseded grids from the newest. */
  messages: UIMessageLike[];
  /**
   * The thread was just restored from the server and no turn has run yet.
   *
   * The grid in a restored transcript is the oldest thing on screen and the
   * likeliest to be stale, but the restore itself is not a change to the
   * schedule. Adopting its ids here (instead of in `trackAvailability`) lets
   * the first re-check of that same grid land on the ids it was asked about,
   * rather than on a set the restore had already invalidated.
   */
  restored?: boolean;
};

const EMPTY_SET: ReadonlySet<string> = new Set();
const MIN_REFRESH_MS = 1500;

/** "A", "A and B", "A, B, and C" — names read naturally in a chat request. */
function joinNames(names: string[]): string {
  if (names.length <= 1) return names[0] ?? "";
  if (names.length === 2) return `${names[0]} and ${names[1]}`;
  return `${names.slice(0, -1).join(", ")}, and ${names[names.length - 1]}`;
}

function cardProposalId(card: ChatCard): string | null {
  return "proposal_id" in card ? card.proposal_id : null;
}

function isProposalCard(card: ChatCard): card is ProposalCard {
  return card.type === "proposal" || card.type === "family_proposal";
}

function describeProposal(card: ProposalCard): string {
  const first = card.items[0];
  if (!first?.starts_at) return "that booking";
  const when = new Date(first.starts_at).toLocaleString(undefined, {
    weekday: "short",
    hour: "numeric",
    minute: "2-digit",
  });
  return `${when}${first.patient_first_name ? ` for ${first.patient_first_name}` : ""}`;
}

function newKey(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
}

/** Slot ids offered by the newest grid in the thread, and who it was booked for. */
function newestGrid(messages: UIMessageLike[]): GridSnapshot | null {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const cards = messages[index].cards ?? [];
    const slots = cards.filter((card): card is SlotCard => card.type === "slot");
    if (slots.length === 0) continue;
    // A family grid is one household card, not a list of single times: hold
    // requests name the people in it, so there is nothing to re-check per slot.
    if (cards.some((card) => card.type === "family_option")) return null;
    const moving = slots.find((slot) => slot.intent === "reschedule");
    return {
      key: messages[index].key,
      appointmentType: slots[0].appointment_type_code,
      patientFirstName: slots[0].patient_first_name ?? null,
      slotIds: slots.map((slot) => slot.slot_id),
      intent: moving ? "reschedule" : "new_booking",
      targetAppointmentId: moving?.target_appointment_id ?? null,
      additionalVisit: slots.some((slot) => slot.additional_visit),
    };
  }
  return null;
}

type OfferedState = {
  live: ReadonlySet<string>;
};

export function useCardActions({
  token,
  conversationId,
  send,
  setMessages,
  setNotice,
  isBusy,
  messages,
  restored = false,
}: Options) {
  const [busySlotId, setBusySlotId] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState<ProposalActions | null>(null);
  const [busyProposalId, setBusyProposalId] = useState<string | null>(null);
  // React state does not update until the current event handler yields. A
  // double-click can therefore enter handleProposalAction twice before the
  // button becomes disabled. Keep a synchronous guard as well so one proposal
  // can only have one browser confirmation request in flight.
  const proposalActionsInFlight = useRef<Set<string>>(new Set());
  // The rendered thread, readable from callbacks without a stale closure.
  const messagesRef = useRef<UIMessageLike[]>(messages);
  messagesRef.current = messages;

  // Slot ids the backend refused ("that time is no longer open"): never
  // actionable again in this thread, even though the card stays visible.
  const [rejectedSlots, setRejectedSlots] = useState<ReadonlySet<string>>(EMPTY_SET);
  // Grids whose request has been settled (a booking held or confirmed, or all
  // of it taken): keyed by the keys of the messages holding them.
  const [settledMessages, setSettledMessages] = useState<ReadonlySet<string>>(EMPTY_SET);
  // Proposals the server still reports as pending. `null` means "not known
  // yet", which keeps restored history clickable until the first sync answers.
  const [liveProposalIds, setLiveProposalIds] = useState<ReadonlySet<string> | null>(null);
  // Ids a re-check has refused or superseded. This is a filter, never an
  // allow-list: a time the server has not refused stays clickable, so a
  // refresher that returns nothing can never disable a whole grid.
  const [staleGrid, setStaleGrid] = useState<ReadonlySet<string>>(EMPTY_SET);
  // Snapshot of the pending proposal set, readable inside async callbacks
  // without a stale closure (a confirmation retires what was pending at click).
  const offeredStateRef = useRef<OfferedState>({ live: EMPTY_SET });
  offeredStateRef.current = { live: liveProposalIds ?? EMPTY_SET };

  const availabilityRef = useRef<CachedAvailability | null>(null);
  // The message holding the newest rendered grid, so a re-render is told apart
  // from a new grid without relying on its ids.
  const renderedGridKeys = useRef<string | null>(null);
  // Set when this turn searched the schedule, until the grid it produced has
  // been adopted. A grid with no search behind it is restored history.
  const searchedGridKeys = useRef<string | null>(null);
  // Read inside `trackAvailability` between renders.
  const restoredRef = useRef(restored);
  restoredRef.current = restored;
  const conversationRef = useRef<string | null>(conversationId);
  conversationRef.current = conversationId;
  const busyRef = useRef(isBusy);
  busyRef.current = isBusy;
  const refreshing = useRef(false);
  const lastRefreshAt = useRef(0);
  const pendingRefresh = useRef<ReturnType<typeof setTimeout> | null>(null);

  /**
   * Remember proposal cards that the server says are currently actionable.
   *
   * Slot cards are deliberately ignored here. A rendered slot is an offer,
   * not a rejection; slots are retired only after a failed selection or when
   * `/scheduling/availability` explicitly returns their ids in `rejected`.
   */
  const markLiveProposals = useCallback(
    (cards: ChatCard[]) => {
      if (cards.length === 0) return;
      const proposals = cards.map(cardProposalId).filter((id): id is string => Boolean(id));
      if (proposals.length) {
        setLiveProposalIds((current) => new Set([...(current ?? []), ...proposals]));
      }
    },
    [],
  );

  /**
   * Retire every slot grid in the thread, except an optional one.
   *
   * Called when an action settles the request behind a grid: once one time is
   * held or booked, the other times in that same grid are alternatives that
   * were not chosen, not further appointments. The cards stay as history; they
   * simply stop being clickable, and their footer says why.
   */
  const settleGrids = useCallback(() => {
    setSettledMessages((current) => {
      const next = new Set(current);
      const messages = messagesRef.current;
      messages.forEach((message) => {
        if (message.cards?.some((card) => card.type === "slot")) next.add(message.key);
      });
      return next;
    });
  }, []);

  const appendCardMessage = useCallback(
    (cards: ChatCard[], note?: string) => {
      if (cards.length === 0 && !note) return;
      markLiveProposals(cards);
      // A settled grid arriving in a turn (the server says one is already
      // held or booked) is settled from the moment it is shown. Everything
      // already in the thread is settled by it too: a newer search replaces
      // the older one.
      if (cards.some((card) => card.type === "slot" && card.settled)) {
        settleGrids();
      }
      setMessages((current) => [
        ...current,
        { key: newKey("card"), id: null, role: "assistant", content: note ?? "", cards },
      ]);
    },
    [markLiveProposals, setMessages, settleGrids],
  );

  /** Swap a resolved proposal card for whatever the server returned. */
  const resolveProposalCard = useCallback(
    (proposalId: string, replacement: ChatCard[]) => {
      setMessages((current) =>
        current.map((message) => {
          if (!message.cards?.some((card) => cardProposalId(card) === proposalId)) {
            return message;
          }
          const kept = message.cards.filter((card) => cardProposalId(card) !== proposalId);
          return { ...message, cards: [...kept, ...replacement] };
        }),
      );
    },
    [setMessages],
  );

  /**
   * Re-read what is still actionable: pending proposals (which ids may be
   * confirmed) and, if the thread has a slot grid, current availability for the
   * same request. Debounced and de-duplicated so a burst of mutations, a focus
   * event, and a fresh turn collapse into one round of requests.
   */
  const resync = useCallback(
    (options: { force?: boolean; ignoreProposalIds?: string[] } = {}) => {
      const authToken = token();
      const activeConversation = conversationRef.current;
      if (!authToken || !activeConversation) return;
      if (refreshing.current) return;
      const since = Date.now() - lastRefreshAt.current;
      if (!options.force && since < MIN_REFRESH_MS) {
        if (pendingRefresh.current) return;
        pendingRefresh.current = setTimeout(() => {
          pendingRefresh.current = null;
          void resync({ force: true, ignoreProposalIds: options.ignoreProposalIds });
        }, MIN_REFRESH_MS - since);
        return;
      }

      refreshing.current = true;
      lastRefreshAt.current = Date.now();
      const availability = availabilityRef.current;

      const pendingCall = schedulingApi
        .pending(authToken, activeConversation)
        .catch(() => null);
      const availabilityCall = availability
        ? schedulingApi
            .availability(authToken, {
              conversation_id: activeConversation,
              appointment_type: availability.appointmentType,
              slot_ids: availability.slotIds,
              patient_first_name: availability.patientFirstName ?? undefined,
              // Keep the grid's meaning: a replacement grid re-checked without
              // its target would come back as ordinary booking times.
              intent: availability.intent,
              target_appointment_id: availability.targetAppointmentId ?? undefined,
              additional_visit: availability.additionalVisit ?? false,
            })
            .catch(() => null)
        : Promise.resolve(null);

      // Proposals confirmed while this request was in flight: the server may
      // still report them as pending, but the thread has already resolved them.
      const ignored = new Set(options.ignoreProposalIds ?? []);
      const liveFrom = (cards: ChatCard[]) =>
        new Set(
          cards
            .map((card) => cardProposalId(card))
            .filter((id): id is string => {
              return Boolean(id) && !ignored.has(id as string);
            }),
        );

      void Promise.all([pendingCall, availabilityCall]).then(([pending, fresh]) => {
        refreshing.current = false;
        if (conversationRef.current !== activeConversation) return;

        // A settled grid means the request already has a booking: it must not
        // stay clickable, even if this particular response did not refresh it.
        const settledNow = Boolean(
          fresh?.cards.some((card) => card.type === "slot" && card.settled),
        );
        if (settledNow) settleGrids();

        if (pending) {
          setLiveProposalIds(liveFrom(pending.proposals));
        }

        // Ids this re-check refused are retired for the rest of the thread.
        if (fresh?.rejected.length) {
          setStaleGrid((current) => new Set([...current, ...fresh.rejected]));
        }
        // A grid is only ever narrowed by the ids the server answers for. An id
        // the response did not mention keeps its own verdict, so a refresh that
        // does not cover a grid can never disable that grid.
        if (fresh && !settledNow) {
          const answered = new Set([
            ...fresh.rejected,
            ...fresh.cards.filter((card) => card.type === "slot").map((card) => card.slot_id),
          ]);
          // An id the server answered for but no longer offers is retired too,
          // so it cannot be clicked into a refusal that already happened.
          const gone = [...(availability?.slotIds ?? [])].filter((id) => !answered.has(id));
          if (gone.length) {
            setStaleGrid((current) => new Set([...current, ...gone]));
          }
        }
        if (fresh) {
          // The refresh carries the pending set too, so a direct action (or a
          // restore where /pending was unreachable) still converges on SQLite.
          if (!pending && fresh.proposals) {
            setLiveProposalIds(liveFrom(fresh.proposals));
          }
        }
      });
    },
    [settleGrids, token],
  );

  /** A schedule search just ran: the grid it renders is an offer made now. */
  const noteScheduleSearched = useCallback(() => {
    searchedGridKeys.current = SEARCH_PENDING;
  }, []);

  // The newest grid becomes the grid a refresh re-checks. A grid is recognised
  // by the message that renders it, not by the ids it happens to contain: an
  // agent turn is streamed in place, and two searches can overlap on a time, so
  // ids alone cannot tell "re-rendered" from "a newer search".
  const trackAvailability = useCallback(
    (messages: UIMessageLike[]) => {
      const grid = newestGrid(messages);
      if (!grid) return;
      const keysChanged =
        renderedGridKeys.current === null || grid.key !== renderedGridKeys.current;
      // A grid that appears in this render without a schedule search behind it
      // is history the server restored, not an offer made now. The marker is
      // bound to the grid's message once it is on screen.
      const searched = searchedGridKeys.current === SEARCH_PENDING;
      if (searched) searchedGridKeys.current = grid.key;
      renderedGridKeys.current = grid.key;
      if (!keysChanged) return;

      availabilityRef.current = grid;
      if (restoredRef.current || !searched) {
        // A restore is not a change to the schedule: its ids are what the
        // transcript last knew, so they are cached for the first re-check but
        // not reasserted as an offer the server has just confirmed.
        return;
      }
      // A grid from a fresh search is an offer made now, so nothing carries over
      // from an older grid: a time that was refused in a previous request is a
      // different question here.
      setStaleGrid(EMPTY_SET);
    },
    [],
  );

  /**
   * Whether a slot in the thread may still be clicked.
   *
   * A replacement card standing in for the patient's own current visit is
   * shown but never selectable: it is already their appointment, so "moving"
   * onto it would book a duplicate. The server refuses it too; disabling it
   * here means the patient never gets that far.
   */
  const isSlotEnabled = useCallback(
    (slot: SlotCard, messageKey?: string) => {
      if (settledMessages.has(messageKey ?? "")) return false;
      if (slot.settled) return false;
      if (rejectedSlots.has(slot.slot_id)) return false;
      if (staleGrid.has(slot.slot_id)) return false;
      if (slot.intent === "reschedule" && slot.moving_from) {
        const ownVisit = new Date(slot.moving_from).getTime();
        if (!Number.isNaN(ownVisit)) {
          if (new Date(slot.starts_at).getTime() === ownVisit) return false;
        }
      }
      const grid = availabilityRef.current;
      if (!grid) return true;
      // A grid the server has retired (its request is held or booked) is
      // history: rendering it as context is the point, so never re-enable it.
      if (slot.settled) return false;
      const current = grid.appointmentType === slot.appointment_type_code &&
        grid.slotIds.includes(slot.slot_id);
      if (current) return true;
      // A slot outside the newest grid is live until the server refuses it.
      return true;
    },
    [rejectedSlots, settledMessages, staleGrid],
  );

  /** Whether a proposal card may still be acted on. */
  const isProposalEnabled = useCallback(
    (card: ProposalCard) => {
      if (!card.actions.length) return false;
      if (card.status !== "pending") return false;
      if (liveProposalIds === null) return true;
      return liveProposalIds.has(card.proposal_id);
    },
    [liveProposalIds],
  );

  /**
   * Clicking an available slot. Holds it server-side and shows the Awaiting
   * Confirmation card. The slot grid stays visible but is disabled while the
   * request is in flight, so one click cannot create two proposals.
   */
  const selectSlot = useCallback(
    async (slot: SlotCard) => {
      const authToken = token();
      if (!authToken || !conversationId || busySlotId) return;
      // A settled grid is history: one request books once, so its other times
      // are never clickable again, even before the next refresh answers.
      if (slot.settled) return;
      setBusySlotId(slot.slot_id);
      setNotice(null);
      try {
        // The card decides what the click means: a replacement time continues
        // the pending move, an ordinary slot books a separate visit. Sending
        // the intent back is what stops "pick a time to move it" from becoming
        // a second appointment.
        const result = await schedulingApi.selectSlot(authToken, {
          slot_ids: [slot.slot_id],
          appointment_type: slot.appointment_type_code,
          conversation_id: conversationId,
          intent: slot.intent ?? "new_booking",
          target_appointment_id: slot.target_appointment_id ?? undefined,
          require_open: slot.require_open ?? false,
          additionalVisit: slot.additional_visit ?? false,
        });
        if (!result.ok) {
          // The time is gone: remember the refusal so the card cannot be
          // clicked again, then show current options.
          setRejectedSlots((current) => new Set([...current, slot.slot_id]));
          // The server refused because this request already has a booking card
          // waiting: that card is the thing to act on, and the rest of the grid
          // is settled (its times would only be a second visit).
          if (result.pending_proposal_id) {
            settleGrids();
            const pending = result.pending_proposal_id;
            setLiveProposalIds((current) => {
              const next = new Set(current ?? []);
              next.add(pending);
              return next;
            });
          }
          setNotice({
            text: result.message ?? "That time is no longer available.",
            level: "warning",
          });
        } else if (result.proposal) {
          // Holding a new time settles the request behind every grid in the
          // thread: one request books once, so the other times are alternatives
          // that were not chosen. Holding also supersedes every earlier pending
          // proposal; anything else still pending comes back from the re-sync.
          settleGrids();
          setLiveProposalIds(new Set([result.proposal.proposal_id]));
          setMessages((current) =>
            current.map((message) => {
              if (!message.cards?.length) return message;
              let touched = false;
              const cards = message.cards.map((card) => {
                if (!isProposalCard(card) || card.proposal_id === result.proposal?.proposal_id) {
                  return card;
                }
                if (!card.actions.length) return card;
                touched = true;
                return { ...card, status: "superseded", actions: [] } as ChatCard;
              });
              return touched ? { ...message, cards } : message;
            }),
          );
        }
        appendCardMessage(result.cards, result.ok ? (result.message ?? undefined) : undefined);
        resync({ force: true });
      } catch (error) {
        setNotice({
          text: error instanceof Error ? error.message : "Could not hold that time.",
          level: "warning",
        });
      } finally {
        setBusySlotId(null);
      }
    },
    [appendCardMessage, busySlotId, conversationId, resync, setMessages, setNotice, settleGrids, token],
  );

  const handleProposalAction = useCallback(
    async (action: ProposalActions, card: ProposalCard) => {
      const authToken = token();
      if (!authToken || !conversationId || busyAction) return;

      if (action === "change") {
        // "Change" is a conversation: the assistant searches again, and the
        // superseded proposal's hold is released server-side. On a move card it
        // must ask for *another replacement time*, not a cancellation: the old
        // wording ("release that hold") read as "cancel it" and abandoned the
        // reschedule, leaving the patient to book a second appointment.
        await send(
          card.kind === "reschedule"
            ? `I'd like a different new time for the move from ${describeProposal(card)}. ` +
              "Please keep the move and show me other replacement times."
            : `I'd like different times instead of ${describeProposal(card)}. ` +
              "Please release that hold and show me other options.",
        );
        return;
      }

      if (proposalActionsInFlight.current.has(card.proposal_id)) return;
      proposalActionsInFlight.current.add(card.proposal_id);

      setBusyAction(action);
      setBusyProposalId(card.proposal_id);
      setNotice(null);
      // Proposals this click resolves. A resync that was already in flight when
      // the click landed will still report them as pending, so they are named
      // to the refresh and filtered out of its answer.
      const resolvedIds: string[] = [];
      try {
        const result =
          action === "confirm"
            ? await schedulingApi.confirm(authToken, card.proposal_id, conversationId)
            : await schedulingApi.decline(authToken, card.proposal_id, conversationId);

        if (!result.ok) {
          // A taken slot leaves the proposal pending; explain and keep the card
          // so the patient can retry or pick another time. Anything else (an
          // expired, superseded, or already-changed proposal) is resolved: the
          // card must stop offering buttons.
          setNotice({
            text: result.message ?? "That didn't go through.",
            level: "warning",
          });
          markLiveProposals(result.cards);
          if (result.cards.length > 0) appendCardMessage(result.cards);
          // A conflict (the time was taken, or the booking can never be made)
          // leaves the card pending server-side so the patient can retry or
          // pick again; only a resolved/stale proposal stops being actionable.
          const retryable =
            result.error === "slot_taken" ||
            result.error === "busy" ||
            result.error === "booking_conflict";
          if (result.proposal_stale) {
            resolveProposalCard(card.proposal_id, result.cards);
          }
          if (!retryable && result.error) {
            setLiveProposalIds((current) => {
              if (current === null) return current;
              const next = new Set(current);
              next.delete(card.proposal_id);
              return next;
            });
          }
        } else {
          // Hide the resolved proposal and show the server's confirmation.
          setLiveProposalIds((current) => {
            if (current === null) return current;
            const next = new Set(current);
            next.delete(card.proposal_id);
            return next;
          });
          // A confirmation means a booking exists for this proposal (today or
          // later, since a reschedule or cancellation can also confirm). Any
          // other proposal that was pending at click time is therefore stale:
          // it may describe the very resource that just changed, so retire it
          // now and let the resync below restore whatever the server still
          // reports as pending.
          const { live } = offeredStateRef.current;
          if (live.size > 0) {
            setLiveProposalIds((current) => {
              if (current === null) return current;
              const next = new Set(current);
              live.forEach((id) => next.delete(id));
              return next;
            });
          }
          resolveProposalCard(card.proposal_id, []);
          appendCardMessage(result.cards, result.message ?? undefined);
          // Whatever settled here is a booking, a move, or a cancellation: the
          // grids in this thread no longer offer anything to book.
          settleGrids();
          resolvedIds.push(card.proposal_id);
        }
      } catch (error) {
        setNotice({
          text: error instanceof Error ? error.message : "That didn't go through.",
          level: "warning",
        });
      } finally {
        proposalActionsInFlight.current.delete(card.proposal_id);
        setBusyAction(null);
        setBusyProposalId(null);
        // Booking, rescheduling, or cancelling changes what is open, so refresh
        // availability (and the pending set) either way.
        resync({ force: true, ignoreProposalIds: resolvedIds });
      }
    },
    [
      appendCardMessage,
      busyAction,
      conversationId,
      markLiveProposals,
      resolveProposalCard,
      resync,
      send,
      setNotice,
      settleGrids,
      token,
    ],
  );

  const holdFamilyOption = useCallback(
    async (card: FamilyOptionCard) => {
      // Turns the chip into an ordinary chat request, so the family hold goes
      // through the same assistant path (and the same SQLite writes) as typing
      // it. Prose, not card contents: the hold is a new message, not a re-offer.
      const names = joinNames(
        card.slots.map((slot) => slot.patient_first_name).filter((name): name is string => Boolean(name)),
      );
      await send(
        names
          ? `Please hold that family option for ${names}. The times in the card work.`
          : "Please hold that family option. The times in the card work.",
      );
    },
    [send],
  );

  // Coming back to the tab is when a patient is most likely to be looking at a
  // stale grid, so re-sync then. A hidden tab does no work.
  useEffect(() => {
    if (!conversationId) return;
    const onVisible = () => {
      if (document.visibilityState !== "visible") return;
      if (busyRef.current?.()) return;
      resync({ force: true });
    };
    window.addEventListener("focus", onVisible);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.removeEventListener("focus", onVisible);
      document.removeEventListener("visibilitychange", onVisible);
      if (pendingRefresh.current) clearTimeout(pendingRefresh.current);
      pendingRefresh.current = null;
    };
  }, [conversationId, resync]);

  // A new conversation starts with a clean slate.
  useEffect(() => {
    setRejectedSlots(EMPTY_SET);
    setSettledMessages(EMPTY_SET);
    setStaleGrid(EMPTY_SET);
    setLiveProposalIds(null);
    availabilityRef.current = null;
    renderedGridKeys.current = null;
    searchedGridKeys.current = null;
    lastRefreshAt.current = 0;
  }, [conversationId]);

  return {
    selectSlot,
    handleProposalAction,
    holdFamilyOption,
    busySlotId,
    busyAction,
    busyProposalId,
    isSlotEnabled,
    isProposalEnabled,
    markLiveProposals,
    trackAvailability,
    noteScheduleSearched,
    resync,
  };
}
