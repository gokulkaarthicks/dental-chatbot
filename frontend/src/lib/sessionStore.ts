/**
 * Browser-side patient session storage: one session per browser tab.
 *
 * The conversation itself is persisted server-side in SQLite; this module only
 * keeps the *pointers* the browser needs to ask for it again:
 *
 *   - the opaque patient session token (the only credential),
 *   - the active conversation id,
 *   - the reset generation the token was issued under.
 *
 * Why the tab, and only the tab
 * -----------------------------
 * Everything lives in ``sessionStorage``. A refresh (Cmd+R) keeps the same
 * tab, so the thread is restored exactly as it was; a second tab, a duplicated
 * tab, or a reopened tab gets a clean session of its own. Nothing is shared
 * between tabs, so one patient's chat can never appear in another tab's
 * history, and "New chat" cannot pull a different tab into the new thread.
 *
 * ``localStorage`` is deliberately *not* used: it is shared by every tab on
 * the origin and outlives the tab that wrote it, which is exactly the
 * cross-tab history leak this module avoids.
 *
 * Nothing stored is trusted: the token is always re-resolved by the backend,
 * and the reset generation is compared on every boot so a demo reset
 * invalidates the stored session instead of resurrecting it.
 *
 * The token is never rendered, logged, or placed in a URL. Any XSS on this
 * origin could read it; that is acceptable for a local demonstration build, and
 * a production deployment would move it to an httpOnly, SameSite=Strict cookie.
 */

const TOKEN_KEY = "dental-demo:patient-session";
const CONVERSATION_KEY = "dental-demo:active-conversation";
const GENERATION_KEY = "dental-demo:reset-generation";
/** Set when a reset invalidated stored state, so the UI can say why. */
const RESET_NOTICE_KEY = "dental-demo:reset-notice";

function get(area: Storage | null, key: string): string | null {
  try {
    return area?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

function set(area: Storage | null, key: string, value: string): void {
  try {
    area?.setItem(key, value);
  } catch {
    // Private mode or storage disabled: the thread simply won't survive reload.
  }
}

function remove(area: Storage | null, key: string): void {
  try {
    area?.removeItem(key);
  } catch {
    // ignore
  }
}

function tabStore(): Storage | null {
  return typeof window === "undefined" ? null : window.sessionStorage;
}

function read(key: string): string | null {
  return get(tabStore(), key);
}

export const sessionStore = {
  /* ---- token + active conversation ---- */

  token(): string | null {
    return read(TOKEN_KEY);
  },
  activeConversation(): string | null {
    return read(CONVERSATION_KEY);
  },
  /**
   * Remember a session and the thread it is currently in. Called when a session
   * is created and after every restore/new-conversation, so the two pointers
   * can never disagree.
   */
  rememberSession(token: string, conversationId: string): void {
    set(tabStore(), TOKEN_KEY, token);
    set(tabStore(), CONVERSATION_KEY, conversationId);
  },
  setToken(token: string): void {
    set(tabStore(), TOKEN_KEY, token);
  },
  setActiveConversation(id: string): void {
    set(tabStore(), CONVERSATION_KEY, id);
  },
  /** Drop a conversation pointer that no longer resolves (e.g. stale id). */
  clearActiveConversation(): void {
    remove(tabStore(), CONVERSATION_KEY);
  },
  /**
   * Forget the session and the open thread. The device-wide generation is kept
   * so the next boot can still tell the patient why.
   */
  clear(): void {
    remove(tabStore(), TOKEN_KEY);
    remove(tabStore(), CONVERSATION_KEY);
  },

  /* ---- demo reset generation ---- */

  generation(): string | null {
    return read(GENERATION_KEY);
  },
  setGeneration(generation: string): void {
    set(tabStore(), GENERATION_KEY, generation);
  },
  clearGeneration(): void {
    remove(tabStore(), GENERATION_KEY);
  },

  /* ---- "the demo was reset" flag ---- */

  resetNotice(): string | null {
    return read(RESET_NOTICE_KEY);
  },
  setResetNotice(text: string): void {
    set(tabStore(), RESET_NOTICE_KEY, text);
  },
  clearResetNotice(): void {
    remove(tabStore(), RESET_NOTICE_KEY);
  },

  /** Every demo-scoped key, for a hard reset from the overview page. */
  clearAll(): void {
    sessionStore.clear();
    remove(tabStore(), GENERATION_KEY);
    remove(tabStore(), RESET_NOTICE_KEY);
  },
};
