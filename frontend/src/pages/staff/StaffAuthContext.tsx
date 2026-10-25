/**
 * Staff authentication state for the dashboard.
 *
 * The only source of truth is `GET /api/staff/session`: on mount we ask the
 * server who we are rather than trusting anything cached in the browser. That
 * means an expired or revoked session is noticed immediately on a refresh, and
 * a forged browser storage flag could never unlock the UI.
 *
 * The server is still the authority. This context decides *which screen* to
 * show; every API call is independently authorized by FastAPI.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { readCsrfCookie, staffApi, type StaffSession, type StaffUser } from "../../api/staff";

type AuthStatus = "checking" | "anonymous" | "authenticated";

type StaffAuthValue = {
  status: AuthStatus;
  user: StaffUser | null;
  session: StaffSession | null;
  /** Set when the sign-in request itself failed, e.g. wrong password. */
  loginError: string | null;
  loggingIn: boolean;
  login: (username: string, password: string) => Promise<boolean>;
  logout: () => Promise<void>;
  /** Re-check the session, e.g. after the tab regains focus. */
  refresh: () => Promise<void>;
};

const StaffAuthContext = createContext<StaffAuthValue | null>(null);

export function StaffAuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("checking");
  const [session, setSession] = useState<StaffSession | null>(null);
  const [loginError, setLoginError] = useState<string | null>(null);
  const [loggingIn, setLoggingIn] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const result = await staffApi.me();
      if (result.authenticated && result.user) {
        setSession(result);
        setStatus("authenticated");
        return;
      }
      setSession(null);
      setStatus("anonymous");
    } catch {
      // A 401 is the normal "not signed in" answer. Anything else (for example
      // the backend being down) is surfaced by the dashboard's own data loads,
      // which have a retry affordance; the login form stays usable either way.
      setSession(null);
      setStatus("anonymous");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // A session can expire while the tab is open. Re-checking on focus is cheap
  // and avoids a confusing wall of 401s on the next click.
  useEffect(() => {
    const onFocus = () => {
      if (readCsrfCookie()) void refresh();
    };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [refresh]);

  const login = useCallback(async (username: string, password: string) => {
    setLoggingIn(true);
    setLoginError(null);
    try {
      const result = await staffApi.login(username.trim(), password);
      setSession(result);
      setStatus("authenticated");
      return true;
    } catch (err) {
      setLoginError(err instanceof Error ? err.message : "Sign-in failed.");
      return false;
    } finally {
      setLoggingIn(false);
    }
  }, []);

  const logout = useCallback(async () => {
    try {
      await staffApi.logout();
    } catch {
      // Even if the request fails we drop local state; the server-side session
      // will expire on its own, and the cookie is cleared by the response.
    }
    setSession(null);
    setStatus("anonymous");
  }, []);

  const value = useMemo<StaffAuthValue>(
    () => ({
      status,
      user: session?.user ?? null,
      session,
      loginError,
      loggingIn,
      login,
      logout,
      refresh,
    }),
    [status, session, loginError, loggingIn, login, logout, refresh],
  );

  return <StaffAuthContext.Provider value={value}>{children}</StaffAuthContext.Provider>;
}

export function useStaffAuth(): StaffAuthValue {
  const value = useContext(StaffAuthContext);
  if (!value) {
    throw new Error("useStaffAuth must be used inside <StaffAuthProvider>.");
  }
  return value;
}
