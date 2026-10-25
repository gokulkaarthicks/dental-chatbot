/**
 * Data loading for the staff dashboard.
 *
 * `useStaffQuery` is the read side: it fetches, exposes loading/error state, and
 * can be reloaded after a mutation. `useStaffMutation` is the write side: it
 * runs the action, reports its error inline, and then tells the whole dashboard
 * to refresh, so no table keeps showing a stale row after a change.
 *
 * Synchronisation
 * ---------------
 * The dashboard reads the same SQLite rows the patient chat writes, so it must
 * not go stale while a receptionist is looking at it. Three cheap mechanisms,
 * no WebSocket:
 *
 *  1. **Change coordination.** Any successful staff mutation bumps
 *     `useStaffSync`; every mounted query for the active view reloads.
 *  2. **Focus.** When the staff tab regains focus (or becomes visible again) the
 *     active view reloads once.
 *  3. **Polling.** While the tab is visible, the active view re-reads on a
 *     spaced interval. A hidden tab does no work at all, and one poll is never
 *     started while another is in flight.
 *
 * Only the mounted (active) view's queries run, so polling costs one round of
 * requests per interval rather than five.
 */

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";

import { ApiError } from "../../api/client";

/** Polling cadence for the active staff view while the tab is visible. */
export const STAFF_POLL_INTERVAL_MS = 20_000;

export type StaffQueryState<T> = {
  data: T | null;
  error: string | null;
  loading: boolean;
  /** True only for background reloads, so tables don't flash a spinner. */
  refreshing: boolean;
  reload: () => void;
};

export type StaffMutationState = {
  pending: boolean;
  error: string | null;
  clearError: () => void;
};

/* ------------------------------------------------------------------ */
/* Change coordination                                                 */
/* ------------------------------------------------------------------ */

let changeVersion = 0;
const changeListeners = new Set<() => void>();

/** Announce that staff-visible data changed (call after a successful write). */
export function notifyStaffDataChanged(): void {
  changeVersion += 1;
  for (const listener of changeListeners) listener();
}

function subscribeToChanges(listener: () => void): () => void {
  changeListeners.add(listener);
  return () => changeListeners.delete(listener);
}

function currentVersion(): number {
  return changeVersion;
}

/** True while the document is visible; a hidden tab must not poll. */
export function isTabVisible(): boolean {
  return typeof document === "undefined" || document.visibilityState === "visible";
}

/**
 * Re-read the active view when it regains focus and on a slow interval.
 *
 * `reload` must be stable (it is: it only bumps a counter). Polling pauses while
 * a tab is hidden and resumes with an immediate read when it becomes visible.
 */
export function useStaffSync(reload: () => void): void {
  const reloadRef = useRef(reload);
  reloadRef.current = reload;

  useEffect(() => {
    let inFlight = false;

    const refresh = () => {
      if (inFlight || !isTabVisible()) return;
      inFlight = true;
      reloadRef.current();
      // The reload is a state bump; the fetch it triggers is not observable
      // here, so the guard is released on the next tick of the interval. That
      // is enough to stop focus + visibilitychange from double-firing.
      window.setTimeout(() => {
        inFlight = false;
      }, 750);
    };

    const onFocus = () => refresh();
    const onVisibility = () => {
      if (document.visibilityState === "visible") refresh();
    };

    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisibility);
    // `refresh()` already yields while hidden, so a hidden tab costs nothing
    // and the poll can never race a focus/visibility read.
    const timer = window.setInterval(refresh, STAFF_POLL_INTERVAL_MS);

    return () => {
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisibility);
      window.clearInterval(timer);
    };
  }, []);
}

/* ------------------------------------------------------------------ */
/* Queries                                                             */
/* ------------------------------------------------------------------ */

/**
 * Load `loader(key, signal)` whenever `key` changes.
 *
 * `key` is a serialisable description of the request (filters, tab), which keeps
 * the effect dependency honest without callers having to memoise callbacks.
 * It also reloads when any staff mutation succeeds, so a change made in one
 * section reaches every list. Views that are unmounted (another tab of the
 * dashboard) pick it up through `useStaffSync` instead.
 */
export function useStaffQuery<T>(
  key: string,
  loader: (signal: AbortSignal) => Promise<T>,
): StaffQueryState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [nonce, setNonce] = useState(0);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;
  const version = useSyncExternalStore(subscribeToChanges, currentVersion, currentVersion);

  const reload = useCallback(() => setNonce((value) => value + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    // Keep the previous rows visible during a reload rather than blanking them.
    setData((current) => {
      if (current !== null) setRefreshing(true);
      else setLoading(true);
      return current;
    });
    setError(null);

    loaderRef
      .current(controller.signal)
      .then((result) => {
        if (!active) return;
        setData(result);
        setError(null);
      })
      .catch((err: unknown) => {
        if (!active || controller.signal.aborted) return;
        setError(describeError(err));
      })
      .finally(() => {
        if (!active) return;
        setLoading(false);
        setRefreshing(false);
      });

    return () => {
      active = false;
      controller.abort();
    };
  }, [key, nonce, version]);

  return { data, error, loading, refreshing, reload };
}

/** Turn any thrown value into a sentence a receptionist can act on. */
export function describeError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401) return "Your session ended. Please sign in again.";
    if (err.status === 403) return "That change was not authorized. Reload and try again.";
    if (err.status === 0) return err.message;
    return err.message;
  }
  if (err instanceof Error) return err.message;
  return "Something went wrong. Please try again.";
}

/**
 * Run a staff action, then refresh every staff view.
 *
 * Errors are held in state rather than thrown so a modal can stay open and show
 * the reason (for example "that slot is no longer open"). On success the change
 * is broadcast to every mounted query *and* `onSuccess` runs immediately, so the
 * view the receptionist is looking at updates without waiting for a poll.
 */
export function useStaffMutation(onSuccess?: () => void): StaffMutationState & {
  run: (action: () => Promise<unknown>) => Promise<boolean>;
} {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const successRef = useRef(onSuccess);
  successRef.current = onSuccess;

  const run = useCallback(async (action: () => Promise<unknown>) => {
    setPending(true);
    setError(null);
    try {
      await action();
      notifyStaffDataChanged();
      successRef.current?.();
      return true;
    } catch (err) {
      setError(describeError(err));
      return false;
    } finally {
      setPending(false);
    }
  }, []);

  return { pending, error, clearError: () => setError(null), run };
}
