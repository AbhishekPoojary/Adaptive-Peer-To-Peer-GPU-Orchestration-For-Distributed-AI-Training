import { useState, type FormEvent } from "react";
import { useLocation, useNavigate, type Location } from "react-router-dom";
import { useLoginMutation } from "@/api/auth";
import { ApiError } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/**
 * Sign-in page (ADR-012).
 *
 * Accounts are created out of band by whoever runs the orchestrator
 * (`python -m scripts.create_user`), so there is no self-registration link
 * here: on this system an account is permission to run containers on other
 * people's laptops, and that is not something a stranger can grant themselves.
 */
export function Login() {
  const navigate = useNavigate();
  const location = useLocation() as Location<{ from?: string } | null>;
  const loginMutation = useLoginMutation();

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setFormError(null);
    try {
      await loginMutation.mutateAsync({ username, password });
      // Return them to whatever they were trying to reach before the guard
      // intercepted, so a bookmarked job link survives a sign-in.
      navigate(location.state?.from ?? "/", { replace: true });
    } catch (err) {
      setFormError(
        err instanceof ApiError
          ? err.message
          : "Couldn't sign in. Please try again.",
      );
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

          <Button type="submit" disabled={loginMutation.isPending}>
            {loginMutation.isPending ? "Signing in…" : "Sign in"}
          </Button>
        </form>

        <p className="mt-4 text-xs text-tertiary">
          No account? Accounts are created on the orchestrator host with{" "}
          <code className="font-data">python -m scripts.create_user</code>. Ask
          whoever runs the fleet.
        </p>
      </div>
    </div>
  );
}
