import { useEffect, useMemo, useRef, useState } from "react";
import { Check, Copy, RefreshCw } from "lucide-react";
import { useCreateEnrollmentTokenMutation } from "@/api/auth";
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

/**
 * Where a *peer* should reach this orchestrator.
 *
 * `VITE_ORCHESTRATOR_URL` is the dev proxy target — it is almost always
 * `http://localhost:8090`, which is correct for this machine and wrong for every
 * other one. A peer that runs a command containing "localhost" dials itself and
 * fails with connection refused, which is exactly the failure this guess exists
 * to avoid.
 *
 * So: keep the configured *port*, but take the *host* the admin actually used to
 * reach this dashboard. Browsing from 192.168.1.5:5173 therefore suggests
 * http://192.168.1.5:8090 rather than localhost. It is a guess, which is why the
 * field it fills is editable and warns when it is still localhost.
 */
function guessPeerFacingUrl(): string {
  const configured =
    (import.meta.env.VITE_ORCHESTRATOR_URL as string | undefined) ||
    "http://localhost:8090";
  try {
    const api = new URL(configured);
    // A configured public address (demo.ps1 -Public sets the orchestrator's
    // own HTTPS tunnel) is exactly what peers should dial: keep it as is.
    // Only a loopback default needs this machine's reachable hostname swapped
    // in -- and swapping a tunnel's hostname into http://…:8090 produced an
    // address that did not exist.
    if (!isLoopback(api.origin)) return api.origin;
    const here = window.location.hostname;
    if (here && here !== api.hostname) {
      api.hostname = here;
      return api.origin;
    }
    return api.origin;
  } catch {
    return configured;
  }
}

/** True for an address no other machine can reach. */
function isLoopback(url: string): boolean {
  try {
    const host = new URL(url).hostname;
    return host === "localhost" || host === "127.0.0.1" || host === "::1";
  } catch {
    return false;
  }
}

type PeerOs = "windows" | "macos" | "linux";

/** Best guess at the *admin's* OS, used only to pick the default tab. */
function detectOs(): PeerOs {
  const ua = navigator.userAgent;
  if (/Win/i.test(ua)) return "windows";
  if (/Mac/i.test(ua)) return "macos";
  return "linux";
}

const OS_LABEL: Record<PeerOs, string> = {
  windows: "Windows",
  macos: "macOS",
  linux: "Linux",
};

/**
 * The command a peer runs.
 *
 * Deliberately no `--orchestrator` flag. The orchestrator substitutes the address
 * the script was fetched from into the script it serves
 * (`orchestrator/api/installer.py:_public_base_url`), so the address is already
 * correct — and passing the flag explicitly is what used to *override* that
 * correct value with a broken one.
 */
function installCommand(os: PeerOs, baseUrl: string, token: string): string {
  const url = baseUrl.replace(/\/+$/, "");
  if (os === "windows") {
    return `$env:ORCH_TOKEN='${token}'; irm ${url}/install.ps1 | iex`;
  }
  return `curl -sSL ${url}/install.sh | bash -s -- --token ${token}`;
}

/** Plain-language steps an admin can paste to a non-technical volunteer. */
function friendInstructions(os: PeerOs, baseUrl: string, token: string): string {
  const cmd = installCommand(os, baseUrl, token);
  const openTerminal =
    os === "windows"
      ? 'Press the Windows key, type "PowerShell", and press Enter.\n   A blue window opens. That is normal.'
      : os === "macos"
        ? 'Press Command + Space, type "Terminal", and press Enter.'
        : "Press Ctrl + Alt + T to open a terminal.";
  const paste =
    os === "windows"
      ? "right-click inside the window to paste"
      : "press Ctrl + Shift + V to paste";

  return [
    "You are helping run AI training on your computer. It takes about 2 minutes.",
    "",
    `1. ${openTerminal}`,
    "",
    `2. Copy the line below, ${paste}, then press Enter:`,
    "",
    `   ${cmd}`,
    "",
    "3. If it asks a yes/no question, type y and press Enter.",
    "",
    '4. When it says "enrolled", you are done. Leave the window open.',
    "",
    "Your computer only reports its own status (how busy it is). Nothing is read",
    "from your files. This invitation stops working after 1 hour.",
  ].join("\n");
}

function hardwareSummary(node: NodeSummary): string {
  const gpus = node.hardware.gpus;
  if (gpus.length === 0) return `${node.hardware.cpu_model}, CPU only`;
  return gpus.map((g) => `${g.name}, ${formatBytes(g.vram_bytes)}`).join(" · ");
}

export interface EnrollPanelProps {
  /** Nodes known to exist when the panel mounts — the baseline the live watch
   * diffs against to find the first genuinely new enrollment. */
  existingNodes: NodeSummary[];
  /** Whether to keep watching for the machine to connect. */
  active: boolean;
}

/**
 * The join command and the live "waiting for it to connect" state, shared by
 * the "Add a computer" dialog and the Lend page.
 *
 * Mints a real enrollment token, shows the one-line install command, and
 * honestly waits for a new node to show up in `GET /nodes` — polling every ~2s
 * and diffing against the set of node ids that existed when it mounted. This is
 * real detection, never a fake spinner: if nothing connects, it keeps waiting
 * and says so, with a way to regenerate the token (e.g. if it expires first).
 *
 * The caller mounts it fresh each time (a `key` on open/close, or only once
 * the node list has loaded), so the baseline snapshot and the minted token are
 * simply this instance's initial state.
 */
export function EnrollPanel({ existingNodes, active }: EnrollPanelProps) {
  const mintMutation = useCreateEnrollmentTokenMutation();
  const [baselineIds] = useState<Set<string>>(
    () => new Set(existingNodes.map((n) => n.id)),
  );
  const [copied, setCopied] = useState(false);
  const [copiedSteps, setCopiedSteps] = useState(false);
  const [, setTick] = useState(0);
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  // Editable, because only a human knows which address a peer can actually
  // reach — LAN, Tailscale, or a tunnel. The guess is a starting point, and on
  // a public deployment it is already right, so the field stays tucked away
  // unless the guess is one no other machine could use.
  const [baseUrl, setBaseUrl] = useState<string>(() => guessPeerFacingUrl());
  const [editAddress, setEditAddress] = useState(false);
  const [os, setOs] = useState<PeerOs>(() => detectOs());

  const token = mintMutation.data;

  // Mint a token exactly once, when this (fresh) instance mounts. The server
  // stamps it with the signed-in user; the label only matters for the admin key.
  useEffect(() => {
    mintMutation.mutate({ created_by: "dashboard" });
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

  // Keep watching while the panel is active and a token exists. Not gated on
  // "already connected": that would be circular (enabled depends on the
  // derived node, which depends on this query's own data), and a light
  // GET /nodes every ~2s costs nothing real.
  const waiting = active && Boolean(token);
  const watchQuery = useWatchForNewNodeQuery(waiting);

  // The first node in a fresh poll whose id isn't in the baseline is the one
  // that just enrolled — derived straight from the query result.
  const connectedNode: NodeSummary | null = useMemo(() => {
    if (!watchQuery.data) return null;
    return watchQuery.data.nodes.find((n) => !baselineIds.has(n.id)) ?? null;
  }, [watchQuery.data, baselineIds]);

  const expired = token ? isPast(token.expires_at) : false;
  const command = token ? installCommand(os, baseUrl, token.token) : "";
  const unreachable = isLoopback(baseUrl);

  async function copyText(text: string, mark: (v: boolean) => void) {
    try {
      await navigator.clipboard.writeText(text);
      mark(true);
      if (copyTimerRef.current) clearTimeout(copyTimerRef.current);
      copyTimerRef.current = setTimeout(() => mark(false), 2000);
    } catch {
      // Clipboard API unavailable in this context — the command is still
      // selectable by hand from the code block; nothing else to do.
    }
  }

  function handleRegenerate() {
    mintMutation.mutate({ created_by: "dashboard" });
  }

  return (
    <div className="flex flex-col gap-3">
      {mintMutation.isPending && (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-9 w-full" />
          <Skeleton className="h-4 w-2/3" />
        </div>
      )}

      {mintMutation.isError && (
        <div className="rounded-[var(--radius-control)] bg-fault-wash px-3 py-2 text-sm text-primary">
          <p>
            {mintMutation.error instanceof ApiError
              ? mintMutation.error.message
              : "Couldn't create a join command. Please try again."}
          </p>
          <Button variant="secondary" size="sm" className="mt-2" onClick={handleRegenerate}>
            Try again
          </Button>
        </div>
      )}

      {token && !connectedNode && (
        <>
          <div className="flex gap-1" role="tablist" aria-label="Operating system">
            {(["windows", "macos", "linux"] as PeerOs[]).map((value) => (
              <button
                key={value}
                type="button"
                role="tab"
                aria-selected={os === value}
                onClick={() => setOs(value)}
                className={
                  os === value
                    ? "rounded-[var(--radius-control)] bg-elevated px-2.5 py-1 text-xs font-medium text-accent border border-hairline"
                    : "rounded-[var(--radius-control)] px-2.5 py-1 text-xs text-secondary hover:bg-elevated"
                }
              >
                {OS_LABEL[value]}
              </button>
            ))}
          </div>

          <div className="flex items-center gap-2">
            <code className="flex-1 overflow-x-auto whitespace-pre rounded bg-elevated px-2.5 py-2 font-data text-xs text-primary">
              {command}
            </code>
            <Button
              type="button"
              variant="secondary"
              size="icon"
              onClick={() => void copyText(command, setCopied)}
              aria-label="Copy join command"
            >
              {copied ? (
                <Check className="size-4 text-good" aria-hidden="true" />
              ) : (
                <Copy className="size-4" aria-hidden="true" />
              )}
            </Button>
          </div>

          <p className="text-xs text-tertiary">
            {os === "windows"
              ? "Paste it into PowerShell (right-click pastes) and press Enter. Python is installed for you if it's missing."
              : "Paste it into Terminal and press Enter. Needs Python 3.11–3.13, and Docker Desktop running."}{" "}
            Leave the window open while your computer is lending.
          </p>

          {(unreachable || editAddress) && (
            <div className="flex flex-col gap-1.5">
              <label htmlFor="peer-address" className="text-xs font-medium text-secondary">
                Address this machine is reachable at
              </label>
              <input
                id="peer-address"
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                spellCheck={false}
                className="w-full rounded-[var(--radius-control)] border border-hairline bg-elevated px-2.5 py-1.5 font-data text-xs text-primary outline-none"
              />
              {unreachable ? (
                <p className="rounded-[var(--radius-control)] bg-fault-wash px-2.5 py-1.5 text-xs text-primary">
                  <strong>Another computer cannot reach this address.</strong>{" "}
                  On their machine <code className="font-data">localhost</code>{" "}
                  means <em>their</em> machine. Replace it with your LAN address
                  (e.g. <code className="font-data">http://192.168.1.5:8090</code>)
                  or your Tailscale address.
                </p>
              ) : (
                <p className="text-xs text-tertiary">
                  The computer must be able to open this address.
                </p>
              )}
            </div>
          )}

          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="button"
              variant="secondary"
              size="sm"
              onClick={() =>
                void copyText(friendInstructions(os, baseUrl, token.token), setCopiedSteps)
              }
            >
              {copiedSteps ? "Copied — paste it to them" : "Copy step-by-step instructions"}
            </Button>
            {!unreachable && !editAddress && (
              <Button type="button" variant="ghost" size="sm" onClick={() => setEditAddress(true)}>
                Change address
              </Button>
            )}
          </div>

          <div className="flex flex-wrap items-center justify-between gap-2 rounded-[var(--radius-control)] bg-sunken px-3 py-2.5 text-xs">
            <span className={expired ? "text-warn" : "text-secondary"}>
              {expired
                ? "This command has expired — make a new one."
                : `Works once, until ${formatTimestamp(token.expires_at)}`}
            </span>
            <Button
              variant="ghost"
              size="sm"
              onClick={handleRegenerate}
              disabled={mintMutation.isPending}
            >
              <RefreshCw className="size-3.5" aria-hidden="true" />
              New command
            </Button>
          </div>

          <div className="flex items-center gap-2 rounded-[var(--radius-control)] bg-sunken px-3 py-2.5 text-[0.8125rem] text-muted">
            <RefreshCw
              className="size-3.5 shrink-0 animate-spin text-tertiary motion-reduce:animate-none"
              aria-hidden="true"
            />
            Waiting for the computer to connect… (checking every ~2s)
          </div>
        </>
      )}

      {connectedNode && (
        <div className="flex items-center gap-2 rounded-[var(--radius-control)] bg-ok-wash px-3 py-2 text-sm text-primary">
          <Check className="size-4 shrink-0 text-good" aria-hidden="true" />
          <span>
            <strong className="font-data">{connectedNode.name}</strong> connected —{" "}
            {hardwareSummary(connectedNode)}. Leave its window open while it lends.
          </span>
        </div>
      )}
    </div>
  );
}

export interface AddNodeModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  existingNodes: NodeSummary[];
}

/**
 * "Add a computer": the join command in a dialog, for the Machines and
 * Overview pages. The caller mounts it keyed on the open/closed transition, so
 * every open is a fresh panel with a fresh token and baseline.
 */
export function AddNodeModal({ open, onOpenChange, existingNodes }: AddNodeModalProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Add a computer</DialogTitle>
          <DialogDescription>
            Run this on the computer you want to lend. It joins the group and
            starts taking training jobs — nothing else on it changes. Each
            command works for one computer.
          </DialogDescription>
        </DialogHeader>

        <EnrollPanel existingNodes={existingNodes} active={open} />

        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
