import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, Cpu, Images } from "lucide-react";
import { useAuthProvidersQuery } from "@/api/auth";
import { getUser, isAdmin } from "@/api/session";

/**
 * Where sign-in and sign-up land: one question, two answers.
 *
 * People come here for one of two reasons — to train a model on their own
 * pictures, or to lend their computer so others can — and the two paths share
 * almost nothing. Asking first means a lender never has to find "Machines",
 * and never has to wait for an admin to send them a join command.
 */
export function Welcome() {
  const user = getUser();
  const providers = useAuthProvidersQuery();
  // Until the answer arrives, offer lending: a refusal, if it comes, is
  // explained on the Lend page itself rather than by a card that vanishes.
  const canLend = isAdmin() || providers.data?.lending !== false;

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 py-6">
      <div>
        <h1 className="text-[1.375rem] font-semibold tracking-[-0.02em] text-ink">
          {user ? `Welcome, ${user.username}` : "Welcome"}
        </h1>
        <p className="mt-1 text-[0.9375rem] text-muted">What would you like to do?</p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <Choice
          to="/datasets"
          icon={<Images className="size-5" aria-hidden="true" />}
          title="Train a model"
          body="Upload pictures sorted into folders, one folder per category, and get back a model that tells them apart. Nothing to install."
          action="Start with your pictures"
        />
        {canLend && (
          <Choice
            to="/lend"
            icon={<Cpu className="size-5" aria-hidden="true" />}
            title="Lend my computer"
            body="Let the group train on your computer while it's switched on. Run one command, once — about ten minutes."
            action="Get my command"
          />
        )}
      </div>

      <p className="text-xs text-muted">
        You can do both, and switch any time from the menu at the top.{" "}
        <Link to="/" className="underline underline-offset-2 hover:text-ink">
          Just look around
        </Link>
      </p>
    </div>
  );
}

function Choice({
  to,
  icon,
  title,
  body,
  action,
}: {
  to: string;
  icon: ReactNode;
  title: string;
  body: string;
  action: string;
}) {
  return (
    <Link
      to={to}
      className="group flex flex-col rounded-[var(--radius-panel)] bg-surface p-5 shadow-panel outline-none transition-shadow hover:shadow-lg focus-visible:ring-2 focus-visible:ring-accent"
    >
      <span className="flex size-10 items-center justify-center rounded-full bg-sunken text-accent">
        {icon}
      </span>
      <h2 className="mt-4 text-[1.0625rem] font-semibold tracking-[-0.01em] text-ink">
        {title}
      </h2>
      <p className="mt-1.5 flex-1 text-[0.8125rem] leading-relaxed text-muted">{body}</p>
      <span className="mt-5 inline-flex items-center gap-1.5 text-[0.8125rem] font-medium text-accent">
        {action}
        <ArrowRight
          className="size-3.5 transition-transform group-hover:translate-x-0.5 motion-reduce:transition-none"
          aria-hidden="true"
        />
      </span>
    </Link>
  );
}
