import { useState, type FormEvent } from "react";
import { useLocation, useNavigate, type Location } from "react-router-dom";
import {
  useAuthProvidersQuery,
  useGoogleLoginMutation,
  useLoginMutation,
} from "@/api/auth";
import { ApiError } from "@/api/client";
import { GoogleSignInButton } from "@/components/GoogleSignInButton";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/**
 * Sign-in page (ADR-012, plus its Google addendum).
 *
 * Accounts are created out of band by whoever runs the orchestrator
 * (`python -m scripts.create_user`), so there is no self-registration link
 * here: on this system an account is permission to run containers on other
 * people's laptops, and that is not something a stranger can grant themselves.
 *
 * Google sign-in does not change that. It is a second way to *prove* you are the
 * holder of an account an admin already created — a Google identity matching no
 * account is refused, and the copy below says so plainly rather than leaving
 * someone clicking a button that will never work for them.
 *
 * The password form is always rendered. Google is additive, and an orchestrator
 * with no internet must stay usable.
 */
export function Login() {
  const navigate = useNavigate();
  const location = useLocation() as Location<{ from?: string } | null>;
  const loginMutation = useLoginMutation();
  const googleMutation = useGoogleLoginMutation();
  const providers = useAuthProvidersQuery();

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  // Falls back to password-only if /auth/providers could not be reached: the
  // sign-in page must not become unusable because an optional lookup failed.
  const google = providers.data?.google;
  const googleEnabled = Boolean(google?.enabled && google.client_id);

  function reportError(err: unknown) {
    setFormError(
      err instanceof ApiError ? err.message : "Couldn't sign in. Please try again.",
    );
  }

  // Where to land after a successful sign-in: back to whatever the guard
  // intercepted, so a bookmarked job link survives it.
  const destination = location.state?.from ?? "/";

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setFormError(null);
    try {
      await loginMutation.mutateAsync({ username, password });
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
    <div className="flex min-h-screen items-center justify-center bg-base px-4 text-primary">
      <div className="w-full max-w-sm">
        <div className="mb-6">
          <h1 className="text-lg font-semibold text-primary">GPU Orchestrator</h1>
          <p className="mt-1 text-sm text-secondary">
            Sign in to submit training jobs and watch the fleet.
          </p>
        </div>

        <form
          onSubmit={(e) => void handleSubmit(e)}
          className="flex flex-col gap-4 rounded-lg border border-hairline bg-panel p-5"
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
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="password">Password</Label>
            <Input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              required
            />
          </div>

          {formError && (
            <div
              role="alert"
              className="rounded-md border border-bad/40 bg-bad/10 px-3 py-2 text-sm text-primary"
            >
              {formError}
            </div>
          )}

          <Button
            type="submit"
            disabled={loginMutation.isPending || googleMutation.isPending}
          >
            {loginMutation.isPending ? "Signing in…" : "Sign in"}
          </Button>

          {googleEnabled && google?.client_id && (
            <>
              <div className="flex items-center gap-3" aria-hidden="true">
                <span className="h-px flex-1 bg-hairline" />
                <span className="text-xs text-tertiary">or</span>
                <span className="h-px flex-1 bg-hairline" />
              </div>

              <div className="flex flex-col items-center gap-2">
                <GoogleSignInButton
                  clientId={google.client_id}
                  onCredential={(credential) => void handleGoogleCredential(credential)}
                  disabled={googleMutation.isPending || loginMutation.isPending}
                />
                {googleMutation.isPending && (
                  <p className="text-xs text-secondary">Signing in with Google…</p>
                )}
              </div>
            </>
          )}
        </form>

        <p className="mt-4 text-xs text-tertiary">
          No account? Accounts are created on the orchestrator host with{" "}
          <code className="font-data">python -m scripts.create_user</code>. Ask
          whoever runs the fleet.
          {googleEnabled && (
            <>
              {" "}
              Google sign-in works only once your address has been added to an
              account — it can&apos;t create one.
            </>
          )}
        </p>
      </div>
    </div>
  );
}
