import { useEffect, useState, type ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { api } from "@/api/client";
import {
  getToken,
  getUser,
  setSession,
  subscribe,
  tokenExpiresAt,
  type SessionUser,
} from "@/api/session";

/** Renew this long before the token expires, and check this often. */
const RENEW_BEFORE_MS = 5 * 60_000;
const CHECK_EVERY_MS = 60_000;

/**
 * Keep the session alive while the dashboard is open.
 *
 * A user token lives 15 minutes and used to be the whole session, so people
 * were sent back to the sign-in page mid-use. Shortly before it expires this
 * swaps it for a fresh one (POST /auth/refresh). Checked on a timer, and when
 * the tab becomes visible again, since background tabs poll nothing. If the
 * swap is refused (signed in over 12 hours, account disabled, laptop asleep
 * past expiry) the 401 handler in api/client.ts ends the session as before.
 */
function useSessionRenewal(active: boolean): void {
  useEffect(() => {
    if (!active) return;
    let renewing = false;
    const check = async () => {
      const expiresAt = tokenExpiresAt();
      if (renewing || expiresAt === null || expiresAt - Date.now() > RENEW_BEFORE_MS) {
        return;
      }
      renewing = true;
      try {
        const { data } = await api.POST("/auth/refresh");
        // Only if still signed in as the same person: a sign-out or a new
        // sign-in during the request must not be overwritten.
        if (data && getUser()?.id === data.user.id) {
          setSession(data.access_token, data.user as SessionUser);
        }
      } finally {
        renewing = false;
      }
    };
    const onVisible = () => {
      if (document.visibilityState === "visible") void check();
    };
    void check();
    const timer = window.setInterval(() => void check(), CHECK_EVERY_MS);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [active]);
}

/**
 * Route guard: redirect to /login when there is no live session (ADR-012).
 *
 * This is a *convenience*, not the security boundary. Every protected route's
 * data comes from an orchestrator endpoint that independently requires a valid
 * user token, so bypassing this component in devtools yields empty pages and
 * 401s, not access. It exists so an expired session shows a sign-in form
 * instead of a wall of error states.
 *
 * Subscribes to session changes so the 401 middleware in api/client.ts — which
 * fires when a token expires mid-session — redirects immediately rather than
 * on the next navigation.
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const location = useLocation();
  const [hasToken, setHasToken] = useState(() => getToken() !== null);

  useEffect(() => subscribe(() => setHasToken(getToken() !== null)), []);
  useSessionRenewal(hasToken);

  if (!hasToken) {
    return (
      <Navigate
        to="/login"
        replace
        // Remember where they were headed so signing in resumes it, rather
        // than dumping every user on the overview page.
        state={{ from: location.pathname + location.search }}
      />
    );
  }
  return <>{children}</>;
}
