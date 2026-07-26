import { useEffect, useMemo, useRef, useState } from "react";
import { Check, Copy, RefreshCw } from "lucide-react";
import { hasAdminKeyConfigured, useCreateEnrollmentTokenMutation } from "@/api/auth";
import { ApiError } from "@/api/client";
import { useWatchForNewNodeQuery } from "@/api/nodes";
import type { NodeSummary } from "@/api/types";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { formatBytes, formatTimestamp, isPast } from "@/lib/format";

const ORCHESTRATOR_URL =
  (import.meta.env.VITE_ORCHESTRATOR_URL as string | undefined) || "http://localhost:8090";

function hardwareSummary(node: NodeSummary): string {
  const gpus = node.hardware.gpus;
  if (gpus.length === 0) return `${node.hardware.cpu_model}, CPU only`;
  return gpus.map((g) => `${g.name}, ${formatBytes(g.vram_bytes)}`).join(" · ");
}

export interface AddNodeModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Nodes known to exist the moment the modal is opened — the baseline the
   * live watch diffs against to find the first genuinely new enrollment. */
  existingNodes: NodeSummary[];
}

/**
 * "Add a node": mints a real enrollment token, shows the one-line install
 * command, and honestly waits for that exact node to show up in `GET /nodes`
 * — polling every ~2s and diffing against the set of node ids that existed
 * when the modal opened. This is real detection, never a fake spinner: if
 * nothing connects, it just keeps waiting and says so, with a way to
 * regenerate the token (e.g. if it expires first).
 *
 * The caller mounts this keyed on the open/closed transition (see Overview's
 * and NodesList's `key={addNodeOpen ? "open" : "closed"}`), so every time it
 * opens it is a genuinely fresh component instance — the baseline snapshot
 * and the minted token are simply this instance's initial state, with no
 * "reset when a prop changes" bookkeeping needed.
 */
export function AddNodeModal({ open, onOpenChange, existingNodes }: AddNodeModalProps) {
  const mintMutation = useCreateEnrollmentTokenMutation();
  const [baselineIds] = useState<Set<string>>(
    () => new Set(existingNodes.map((n) => n.id)),
  );
  const [copied, setCopied] = useState(false);
  const [, setTick] = useState(0);
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const token = mintMutation.data;

  // Mint a token exactly once, when this (fresh, per-open) instance mounts.
  useEffect(() => {
    mintMutation.mutate({ created_by: "dashboard-operator" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // A 1s tick to re-evaluate the token's real wall-clock expiry on each
  // render (mirrors UpdatedAgo's pattern) — the clock read itself lives in
  // `isPast`, not inline here, and setState happens only inside the interval
  // callback, never synchronously in the effect body.
  useEffect(() => {
    const id = window.setInterval(() => setTick((t) => t + 1), 1000);
    return () => window.clearInterval(id);
  }, []);

  // Keep watching for as long as the modal is open and a token exists. We
  // deliberately do NOT gate this on "already connected" — that would create
  // a circular dependency (enabled depends on the derived node, which
  // depends on this query's own data). Polling a lightweight GET /nodes every
  // ~2s for the rest of the time the modal happens to stay open costs nothing
  // real; the UI below simply stops *displaying* the waiting state once a
  // match is found.
  const waiting = open && Boolean(token);
  const watchQuery = useWatchForNewNodeQuery(waiting);

  // The first node in a fresh poll whose id isn't in the baseline is the one
  // that just enrolled — derived straight from the query result, not stored
  // state, so there is nothing to keep in sync by hand.
  const connectedNode: NodeSummary | null = useMemo(() => {
    if (!watchQuery.data) return null;
    return watchQuery.data.nodes.find((n) => !baselineIds.has(n.id)) ?? null;
  }, [watchQuery.data, baselineIds]);

  const expired = token ? isPast(token.expires_at) : false;
  const command = token
    ? `curl -sSL ${ORCHESTRATOR_URL}/install.sh | bash -s -- --token ${token.token} --orchestrator ${ORCHESTRATOR_URL}`
    : "";

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      if (copyTimerRef.current) clearTimeout(copyTimerRef.current);
      copyTimerRef.current = setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard API unavailable in this context — the command is still
      // selectable by hand from the code block; nothing else to do.
    }
  }

  function handleRegenerate() {
    mintMutation.mutate({ created_by: "dashboard-operator" });
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Add a node</DialogTitle>
          <DialogDescription>
            Run this on the peer machine whose GPU you want to share. It enrolls
            that machine with the orchestrator and starts heartbeating — nothing
            else on it changes.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          {!hasAdminKeyConfigured() && (
            <div className="rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-sm text-primary">
              This dashboard build has no admin key configured, so it can't mint an
              enrollment token. See <code className="font-data">dashboard/.env.example</code>.
            </div>
          )}

          {mintMutation.isPending && (
            <div className="flex flex-col gap-2">
              <Skeleton className="h-9 w-full" />
              <Skeleton className="h-4 w-2/3" />
            </div>
          )}

          {mintMutation.isError && (
            <div className="rounded-md border border-bad/40 bg-bad/10 px-3 py-2 text-sm text-primary">
              <p>
                {mintMutation.error instanceof ApiError
                  ? mintMutation.error.message
                  : "Couldn't mint an enrollment token. Please try again."}
              </p>
              <Button variant="secondary" size="sm" className="mt-2" onClick={handleRegenerate}>
                Try again
              </Button>
            </div>
          )}

          {token && !connectedNode && (
            <>
              <div className="flex items-center gap-2">
                <code className="flex-1 overflow-x-auto whitespace-pre rounded bg-elevated px-2.5 py-2 font-data text-xs text-primary">
                  {command}
                </code>
                <Button
                  type="button"
                  variant="secondary"
                  size="icon"
                  onClick={() => void handleCopy()}
                  aria-label="Copy install command"
                >
                  {copied ? (
                    <Check className="size-4 text-good" aria-hidden="true" />
                  ) : (
                    <Copy className="size-4" aria-hidden="true" />
                  )}
                </Button>
              </div>

              <p className="text-xs text-tertiary">
                Targets Linux/macOS. On Windows, install{" "}
                <a
                  href="https://learn.microsoft.com/windows/wsl/install"
                  target="_blank"
                  rel="noreferrer"
                  className="text-accent hover:underline"
                >
                  WSL2
                </a>{" "}
                first and run this exact command inside it.
              </p>

              <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-hairline bg-panel px-3 py-2 text-xs">
                <span className={expired ? "text-warn" : "text-secondary"}>
                  {expired
                    ? "Token expired — regenerate to get a fresh one."
                    : `Token expires ${formatTimestamp(token.expires_at)}`}
                </span>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={handleRegenerate}
                  disabled={mintMutation.isPending}
                >
                  <RefreshCw className="size-3.5" aria-hidden="true" />
                  Regenerate token
                </Button>
              </div>

              <div className="flex items-center gap-2 rounded-md border border-hairline bg-panel px-3 py-2 text-sm text-secondary">
                <RefreshCw
                  className="size-3.5 shrink-0 animate-spin text-tertiary motion-reduce:animate-none"
                  aria-hidden="true"
                />
                Waiting for the node to connect… (checking every ~2s)
              </div>
            </>
          )}

          {connectedNode && (
            <div className="flex items-center gap-2 rounded-md border border-good/40 bg-good/10 px-3 py-2 text-sm text-primary">
              <Check className="size-4 shrink-0 text-good" aria-hidden="true" />
              <span>
                <strong className="font-data">{connectedNode.name}</strong> connected —{" "}
                {hardwareSummary(connectedNode)}
              </span>
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            {connectedNode ? "Done" : "Close"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
