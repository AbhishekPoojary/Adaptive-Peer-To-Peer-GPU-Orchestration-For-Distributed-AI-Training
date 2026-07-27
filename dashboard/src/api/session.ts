/**
 * Browser-side session for the human operator (ADR-012).
 *
 * Replaces the M7 arrangement where `VITE_ADMIN_API_KEY` was inlined into the
 * built JS at build time — a fleet-admin secret readable by anyone who opened
 * devtools. The browser now holds only a short-lived, user-scoped JWT that it
 * obtained by someone actually typing a password.
 *
 * Storage is `sessionStorage`, not an httpOnly cookie. That is a deliberate
 * trade-off recorded in ADR-012 §6: the dashboard is a separate origin from
 * the API, so a cross-origin httpOnly cookie needs `SameSite=None; Secure`,
 * which needs HTTPS, which the Tailscale topology in ADR-010 does not
 * terminate. A bearer token here is readable by XSS but immune to CSRF and
 * actually works over the transport we have. `sessionStorage` rather than
 * `localStorage` so the token dies with the tab instead of persisting on a
 * shared laptop.
 */

const TOKEN_KEY = "gpuorch.access_token";
const USER_KEY = "gpuorch.user";

export interface SessionUser {
  id: string;
  username: string;
  role: string;
  created_at: string;
  last_login_at: string | null;
}

type Listener = () => void;
const listeners = new Set<Listener>();

/** Subscribe to session changes (login, logout, expiry). Returns unsubscribe. */
export function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function notify(): void {
  for (const listener of listeners) listener();
}

export function getToken(): string | null {
  return sessionStorage.getItem(TOKEN_KEY);
}

export function getUser(): SessionUser | null {
  const raw = sessionStorage.getItem(USER_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as SessionUser;
  } catch {
    // A corrupt entry is treated as "logged out" rather than crashing the app
    // shell on boot. The token is the credential that matters; this is display
    // data, and the server re-checks the role on every privileged call anyway.
    return null;
  }
}

export function isAdmin(): boolean {
  return getUser()?.role === "ADMIN";
}

export function setSession(token: string, user: SessionUser): void {
  sessionStorage.setItem(TOKEN_KEY, token);
  sessionStorage.setItem(USER_KEY, JSON.stringify(user));
  notify();
}

export function clearSession(): void {
  sessionStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(USER_KEY);
  notify();
}
