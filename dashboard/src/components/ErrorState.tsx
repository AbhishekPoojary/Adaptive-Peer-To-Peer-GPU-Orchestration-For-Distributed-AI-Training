import { AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ApiError } from "@/api/client";
import { cn } from "@/lib/utils";

export interface ErrorStateProps {
  error: unknown;
  onRetry?: () => void;
  className?: string;
}

/** Plain-language error, no raw stack traces or HTTP status codes surfaced to
 * the user, with a retry action. Wired to real fetch failures — this is what
 * renders when the orchestrator is unreachable or returns an error. */
export function ErrorState({ error, onRetry, className }: ErrorStateProps) {
  const message =
    error instanceof ApiError
      ? error.message
      : "Couldn't reach the orchestrator. It may be restarting or unreachable from here.";

  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-2 rounded-md border border-bad/30 bg-panel px-6 py-12 text-center",
        className,
      )}
    >
      <AlertTriangle className="mb-1 size-8 text-bad" aria-hidden="true" />
      <p className="text-sm font-medium text-primary">Something went wrong</p>
      <p className="max-w-md text-sm text-secondary">{message}</p>
      {onRetry && (
        <Button variant="secondary" size="sm" className="mt-3" onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  );
}
