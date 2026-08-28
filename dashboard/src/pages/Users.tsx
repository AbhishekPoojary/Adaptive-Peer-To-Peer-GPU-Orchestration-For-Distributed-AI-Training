import { useState, type FormEvent } from "react";
import {
  useCreateUserMutation,
  useUpdateUserMutation,
  useUsersQuery,
  type ManagedUser,
} from "@/api/users";
import { ApiError } from "@/api/client";
import { getUser, isAdmin } from "@/api/session";
import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/hooks/use-toast";
import { formatRelativeTime } from "@/lib/format";

/**
 * People page (ADR-012 addendum 2).
 *
 * Exists so that inviting a classmate no longer requires SSH into the
 * orchestrator host. Everything here is ADMIN-only server-side; the role check
 * below only decides what to draw, and a tampered client can reveal the form
 * but not use it.
 *
 * The most common action — invite someone who will sign in with Google — is one
 * field and a button, because that is the flow that was previously a terminal
 * session and a database URL.
 */
export function Users() {
  const admin = isAdmin();
  const me = getUser();
  const { data, isPending, error, refetch } = useUsersQuery(admin);
  const create = useCreateUserMutation();
  const update = useUpdateUserMutation();

  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<"ADMIN" | "OPERATOR">("OPERATOR");
  const [formError, setFormError] = useState<string | null>(null);

  if (!admin) {
    return (
      <EmptyState
        title="Admins only"
        description="Managing people requires the ADMIN role. Ask whoever runs the fleet."
      />
    );
  }

  async function handleCreate(e: FormEvent) {
    e.preventDefault();
    setFormError(null);
    if (!email.trim() && !password) {
      setFormError(
        "Give an email (for Google sign-in), a password, or both — otherwise there is no way to sign in.",
      );
      return;
    }
    try {
      const created = await create.mutateAsync({
        username: username.trim(),
        role,
        email: email.trim() || undefined,
        password: password || undefined,
      });
      toast({
        title: `Added ${created.username}`,
        description: created.has_password
          ? `Can sign in with a password${created.email ? " or Google" : ""}.`
          : `Signs in with Google as ${created.email}.`,
        variant: "success",
      });
      setUsername("");
      setEmail("");
      setPassword("");
      setRole("OPERATOR");
    } catch (err) {
      setFormError(
        err instanceof ApiError ? err.message : "Couldn't create that account.",
      );
    }
  }

  async function apply(user: ManagedUser, changes: Parameters<typeof update.mutateAsync>[0]["changes"], what: string) {
    try {
      await update.mutateAsync({ id: user.id, changes });
      toast({ title: `${user.username}: ${what}`, variant: "success" });
    } catch (err) {
      toast({
        title: `Couldn't update ${user.username}`,
        description: err instanceof ApiError ? err.message : undefined,
        variant: "destructive",
      });
    }
  }

  async function handleSetEmail(user: ManagedUser) {
    const next = window.prompt(
      `Email for ${user.username} (used by Google sign-in). Leave blank to remove it.`,
      user.email ?? "",
    );
    if (next === null) return;
    const trimmed = next.trim();
    if (trimmed === (user.email ?? "")) return;
    await apply(
      user,
      { email: trimmed || null },
      trimmed ? `Google sign-in set to ${trimmed}` : "email cleared",
    );
  }

  async function handleResetPassword(user: ManagedUser) {
    const next = window.prompt(
      `New password for ${user.username} (at least 12 characters).`,
    );
    if (next === null) return;
    await apply(user, { password: next }, "password reset");
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-xl font-semibold text-primary">People</h1>
        <p className="mt-1 text-sm text-secondary">
          Accounts that can sign in and submit jobs. Adding someone here replaces
          running <code className="font-data">scripts/create_user.py</code> on the
          orchestrator host.
        </p>
      </div>

      <form
        onSubmit={(e) => void handleCreate(e)}
        className="flex flex-col gap-4 rounded-lg border border-hairline bg-panel p-5"
      >
        <div>
          <h2 className="text-sm font-semibold text-primary">Add someone</h2>
          <p className="mt-1 text-xs text-secondary">
            Give an email and they sign in with Google — no password to share.
            Give a password instead for an account that works offline. Both is
            fine too.
          </p>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="new-username">Username</Label>
            <Input
              id="new-username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="priya"
              pattern="[A-Za-z0-9][A-Za-z0-9._-]{2,63}"
              title="Letters, digits, dot, dash, underscore. At least 3 characters."
              required
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="new-role">Role</Label>
            <Select
              value={role}
              onValueChange={(v) => setRole(v as "ADMIN" | "OPERATOR")}
            >
              <SelectTrigger id="new-role">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="OPERATOR">
                  OPERATOR — submit and watch jobs
                </SelectItem>
                <SelectItem value="ADMIN">
                  ADMIN — also add machines, datasets, people
                </SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="new-email">Email for Google sign-in</Label>
            <Input
              id="new-email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="priya@example.com"
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="new-password">Password (optional)</Label>
            <Input
              id="new-password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="at least 12 characters"
              autoComplete="new-password"
            />
          </div>
        </div>

        {formError && (
          <div
            role="alert"
            className="rounded-md border border-bad/40 bg-bad/10 px-3 py-2 text-sm text-primary"
          >
            {formError}
          </div>
        )}

        <div>
          <Button type="submit" disabled={create.isPending}>
            {create.isPending ? "Adding…" : "Add person"}
          </Button>
        </div>
      </form>

      {isPending && <Skeleton className="h-32 w-full" />}
      {error && <ErrorState error={error} onRetry={() => void refetch()} />}

      {data && data.length > 0 && (
        <div className="flex flex-col gap-3">
          {data.map((user) => {
            const disabled = user.disabled_at !== null;
            const isMe = me?.id === user.id;
            return (
              <article
                key={user.id}
                className={`rounded-lg border border-hairline bg-panel p-4 ${
                  disabled ? "opacity-60" : ""
                }`}
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h3 className="text-sm font-semibold text-primary">
                      {user.username}
                      {isMe && (
                        <span className="ml-2 text-xs font-normal text-tertiary">
                          you
                        </span>
                      )}
                    </h3>
                    <p className="mt-0.5 text-xs text-secondary">
                      {user.role}
                      {disabled && " · disabled"}
                      {" · signs in with "}
                      {[
                        user.has_password ? "password" : null,
                        user.email ? "Google" : null,
                      ]
                        .filter(Boolean)
                        .join(" or ") || "nothing"}
                    </p>
                    {user.email && (
                      <p className="mt-0.5 text-xs text-tertiary font-data">
                        {user.email}
                        {!user.google_linked && " (not linked yet)"}
                      </p>
                    )}
                    <p className="mt-0.5 text-xs text-tertiary">
                      {user.last_login_at
                        ? `last signed in ${formatRelativeTime(user.last_login_at)}`
                        : "never signed in"}
                    </p>
                  </div>

                  <div className="flex flex-wrap gap-1.5">
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => void handleSetEmail(user)}
                      disabled={update.isPending}
                    >
                      {user.email ? "Change email" : "Add email"}
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => void handleResetPassword(user)}
                      disabled={update.isPending}
                    >
                      Reset password
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() =>
                        void apply(
                          user,
                          { role: user.role === "ADMIN" ? "OPERATOR" : "ADMIN" },
                          user.role === "ADMIN" ? "now an OPERATOR" : "now an ADMIN",
                        )
                      }
                      disabled={update.isPending}
                    >
                      Make {user.role === "ADMIN" ? "operator" : "admin"}
                    </Button>
                    <Button
                      variant={disabled ? "secondary" : "ghost"}
                      size="sm"
                      onClick={() =>
                        void apply(
                          user,
                          { disabled: !disabled },
                          disabled ? "re-enabled" : "disabled",
                        )
                      }
                      disabled={update.isPending}
                    >
                      {disabled ? "Enable" : "Disable"}
                    </Button>
                  </div>
                </div>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}
