import { useState, type FormEvent } from "react";
import { useLocation, useNavigate, type Location } from "react-router-dom";
import {
  useAuthProvidersQuery,
  useGoogleLoginMutation,
  useLoginMutation,
  useRegisterMutation,
} from "@/api/auth";
import { ApiError } from "@/api/client";
import { GoogleSignInButton } from "@/components/GoogleSignInButton";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/** Kept in step with the server's rule (services/users.py). */
const MIN_PASSWORD_LENGTH = 12;

type Mode = "sign-in" | "register";

/**
 * Sign-in and sign-up (ADR-012 and its addenda).
 *
 * Anyone given the dashboard link can create their own account when the
 * deployment allows it (ALLOW_REGISTRATION, reported by /auth/providers). That
 * account is an ordinary one: it trains on its own data and sees nothing of
 * anyone else's. With registration closed the page says to ask an admin.
 *
 * Google sign-in is offered only on an origin registered for it in Google Cloud
 * (GOOGLE_OAUTH_ORIGINS). A quick-tunnel link gets a new random address every
 * run, which can never be registered, so a friend there used to see a Google
 * button that could only fail. While registration is open, a first Google
 * sign-in creates the same ordinary account the form does, so the button is
 * offered in both modes.
 *
 * The password form is always rendered: an orchestrator with no internet must
 * stay usable.
 */
export function Login() {
  const navigate = useNavigate();
  const location = useLocation() as Location<{ from?: string } | null>;
  const loginMutation = useLoginMutation();
  const registerMutation = useRegisterMutation();
  const googleMutation = useGoogleLoginMutation();
  const providers = useAuthProvidersQuery();

  const [mode, setMode] = useState<Mode>("sign-in");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  const registrationOpen = providers.data?.registration === true;
  const registering = mode === "register" && registrationOpen;

  // Falls back to password-only if /auth/providers could not be reached.
  const google = providers.data?.google;
  const origins = google?.origins ?? [];
  const googleHere = origins.length === 0 || origins.includes(window.location.origin);
  const googleEnabled = Boolean(google?.enabled && google.client_id && googleHere);

  const busy = loginMutation.isPending || registerMutation.isPending || googleMutation.isPending;

  function reportError(err: unknown) {
    setFormError(err instanceof ApiError ? err.message : "Something went wrong. Please try again.");
  }

  // Back to whatever the guard intercepted, so a bookmarked job link survives.
  const destination = location.state?.from ?? "/";

  function switchTo(next: Mode) {
    setMode(next);
    setFormError(null);
    setConfirm("");
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setFormError(null);
    if (registering) {
      // Checked here as well as on the server, so the common mistakes are
      // caught before a round trip and said in plain words.
      if (password.length < MIN_PASSWORD_LENGTH) {
        setFormError(`Use at least ${MIN_PASSWORD_LENGTH} characters for your password.`);
        return;
      }
      if (password !== confirm) {
        setFormError("The two passwords don't match.");
        return;
      }
    }
    try {
      if (registering) {
        await registerMutation.mutateAsync({ username, password });
      } else {
        await loginMutation.mutateAsync({ username, password });
      }
      navigate(destination, { replace: true });
    } catch (err) {
      reportError(err);
    }
  }

  async function handleGoogleCredential(credential: string) {
    setFormError(null);
    try {
      await googleMutation.mutateAsync(credential);
      navigate(destination, { replace: true });
    } catch (err) {
      reportError(err);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-canvas px-4 text-ink">
      <div className="w-full max-w-[25rem]">
        <div className="mb-7">
          <svg viewBox="0 0 20 20" className="mb-4 size-6" aria-hidden="true" fill="none">
            <rect x="2" y="9" width="4" height="9" rx="1.4" fill="var(--nosignal)" />
            <rect x="8" y="5" width="4" height="13" rx="1.4" fill="var(--accent)" />
            <rect x="14" y="2" width="4" height="16" rx="1.4" fill="var(--ink)" />
          </svg>
          <h1 className="text-[1.375rem] font-semibold tracking-[-0.02em] text-ink">
            {registering ? "Create your account" : "Sign in to Orchestrator"}
          </h1>
          <p className="mt-1.5 text-[0.8125rem] text-muted">
            {registering
              ? "Train on your own data using machines you don't have to own. Only you can see your data and models."
              : "Train on machines you don't have to own."}
          </p>
        </div>

        <form
          onSubmit={(e) => void handleSubmit(e)}
          className="flex flex-col gap-4 rounded-[var(--radius-panel)] bg-surface p-6 shadow-panel"
        >
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="username">Username</Label>
            <Input
              id="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              autoFocus
              required
              aria-describedby={registering ? "username-hint" : undefined}
            />
            {registering && (
              <p id="username-hint" className="text-xs text-muted">
                3–64 characters: letters, numbers, and . _ -
              </p>
            )}
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="password">Password</Label>
            <Input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete={registering ? "new-password" : "current-password"}
              required
              aria-describedby={registering ? "password-hint" : undefined}
            />
            {registering && (
              <p id="password-hint" className="text-xs text-muted">
                At least {MIN_PASSWORD_LENGTH} characters.
              </p>
            )}
          </div>

          {registering && (
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="confirm-password">Confirm password</Label>
              <Input
                id="confirm-password"
                type="password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                autoComplete="new-password"
                required
              />
            </div>
          )}

          {formError && (
            <div
              role="alert"
              className="rounded-[var(--radius-control)] bg-fault-wash px-3 py-2.5 text-[0.8125rem] text-fault"
            >
              {formError}
            </div>
          )}

          <Button type="submit" disabled={busy}>
            {registering
              ? registerMutation.isPending
                ? "Creating account…"
                : "Create account"
              : loginMutation.isPending
                ? "Signing in…"
                : "Sign in"}
          </Button>

          {googleEnabled && google?.client_id && (
            <>
              <div className="flex items-center gap-3" aria-hidden="true">
                <span className="h-px flex-1 bg-hairline" />
                <span className="text-xs text-faint">or</span>
                <span className="h-px flex-1 bg-hairline" />
              </div>

              <div className="flex flex-col items-center gap-2">
                <GoogleSignInButton
                  clientId={google.client_id}
                  onCredential={(credential) => void handleGoogleCredential(credential)}
                  disabled={busy}
                  signUp={registering}
                />
                {googleMutation.isPending && (
                  <p className="text-xs text-muted">Signing in with Google…</p>
                )}
              </div>
            </>
          )}
        </form>

        <p className="mt-5 text-[0.8125rem] text-muted">
          {registrationOpen ? (
            registering ? (
              <>
                Already have an account?{" "}
                <Button variant="link" className="h-auto p-0 align-baseline" onClick={() => switchTo("sign-in")}>
                  Sign in
                </Button>
              </>
            ) : (
              <>
                No account?{" "}
                <Button variant="link" className="h-auto p-0 align-baseline" onClick={() => switchTo("register")}>
                  Create one
                </Button>
              </>
            )
          ) : (
            "No account? Ask whoever runs this orchestrator to create one for you."
          )}
        </p>
      </div>
    </div>
  );
}
