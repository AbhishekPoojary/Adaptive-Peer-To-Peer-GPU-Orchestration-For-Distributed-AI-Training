import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { formatRelativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * Honest "updated Xs ago" indicator backed by TanStack Query's real
 * `dataUpdatedAt`. This is REST-polled (no WebSocket in the current
 * backend, ADR-011) — deliberately not a fake "live" dot with nothing behind
 * it. `isFetching` briefly shows a spinning refresh glyph for the in-flight
 * poll itself.
 */
export function UpdatedAgo({
  dataUpdatedAt,
  isFetching,
  className,
}: {
  dataUpdatedAt: number;
  isFetching?: boolean;
  className?: string;
}) {
  const [, setTick] = useState(0);

  useEffect(() => {
    const id = window.setInterval(() => setTick((t) => t + 1), 1000);
    return () => window.clearInterval(id);
  }, []);

  if (!dataUpdatedAt) return null;

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 text-xs text-tertiary font-data",
        className,
      )}
    >
      <RefreshCw
        className={cn("size-3", isFetching && "animate-spin motion-reduce:animate-none")}
        aria-hidden="true"
      />
      Updated {formatRelativeTime(new Date(dataUpdatedAt))}
    </span>
  );
}
