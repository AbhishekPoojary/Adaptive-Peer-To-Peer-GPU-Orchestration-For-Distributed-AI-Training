import { useEffect, useRef, useState } from "react";
import { ArrowDown, RefreshCw } from "lucide-react";
import type { TrainingLogLine } from "@/api/types";
import { Skeleton } from "@/components/ui/skeleton";
import { formatClockTime } from "@/lib/format";
import { cn } from "@/lib/utils";

export interface LogViewerProps {
  lines: TrainingLogLine[];
  isLoading: boolean;
  isReconnecting: boolean;
  className?: string;
}

/** Distance (px) from the true bottom that still counts as "following". */
const FOLLOW_THRESHOLD_PX = 32;

/**
 * Live streaming log transcript. Auto-scrolls to the newest line as they
 * arrive, but the instant the reader scrolls up to read history it stops
 * following (no fighting the reader for scroll position) and offers a
 * "jump to latest" affordance to resume. A quiet "reconnecting" note appears
 * on a failed poll without ever clearing what's already been read.
 */
export function LogViewer({ lines, isLoading, isReconnecting, className }: LogViewerProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [following, setFollowing] = useState(true);

  useEffect(() => {
    const el = containerRef.current;
    if (!el || !following) return;
    el.scrollTop = el.scrollHeight;
  }, [lines, following]);

  function handleScroll() {
    const el = containerRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    setFollowing(distanceFromBottom < FOLLOW_THRESHOLD_PX);
  }

  function jumpToLatest() {
    const el = containerRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    setFollowing(true);
  }

  return (
    <div className={cn("relative rounded-md border border-hairline bg-base/60", className)}>
      {isReconnecting && (
        <div className="absolute right-2 top-2 z-10 flex items-center gap-1.5 rounded border border-warn/40 bg-panel/95 px-2 py-1 text-xs text-warn">
          <RefreshCw className="size-3 animate-spin motion-reduce:animate-none" aria-hidden="true" />
          Reconnecting…
        </div>
      )}
      <div
        ref={containerRef}
        onScroll={handleScroll}
        tabIndex={0}
        aria-label="Live training log transcript"
        className="h-72 overflow-y-auto p-3 font-data text-xs leading-relaxed outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-inset"
      >
        {isLoading ? (
          <div className="flex flex-col gap-2">
            <Skeleton className="h-3 w-full" />
            <Skeleton className="h-3 w-5/6" />
            <Skeleton className="h-3 w-2/3" />
          </div>
        ) : lines.length === 0 ? (
          <p className="text-secondary">
            No logs yet — waiting for the trainer container to start producing output.
          </p>
        ) : (
          lines.map((l) => (
            <div key={l.id} className="whitespace-pre-wrap break-words">
              <span className="text-tertiary">{formatClockTime(l.ts)} </span>
              {l.stream === "stderr" && (
                <span className="mr-1 text-warn" aria-hidden="true">
                  [err]
                </span>
              )}
              <span className={l.stream === "stderr" ? "text-warn" : "text-secondary"}>
                {l.line}
              </span>
            </div>
          ))
        )}
      </div>
      {!following && lines.length > 0 && (
        <button
          type="button"
          onClick={jumpToLatest}
          className="absolute bottom-3 right-3 flex items-center gap-1.5 rounded-md border border-hairline bg-elevated px-2.5 py-1.5 text-xs font-medium text-primary shadow-lg outline-none hover:bg-elevated/80 focus-visible:ring-2 focus-visible:ring-accent"
        >
          <ArrowDown className="size-3.5" aria-hidden="true" />
          Jump to latest
        </button>
      )}
    </div>
  );
}
