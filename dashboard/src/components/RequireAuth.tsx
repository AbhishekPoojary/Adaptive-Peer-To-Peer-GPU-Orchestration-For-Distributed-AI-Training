import { useEffect, useState, type ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { getToken, subscribe } from "@/api/session";

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
