import { Link } from "react-router-dom";
import type { Lease } from "@/api/types";
import { StatusPill } from "@/components/StatusPill";
import { shortId } from "@/lib/format";
import { cn } from "@/lib/utils";

export interface RankStripProps {
  /** This attempt's leases (already filtered to the job's current lease
   * epoch by the caller) — one per rank. */
  leases: Lease[];
  /** node id -> display name, from the live /nodes list. A node whose name
   * hasn't loaded yet falls back to its short id rather than blocking. */
  nodeNames: Record<string, string>;
  className?: string;
}

/**
 * Which peer holds which rank for this job's current attempt. A world_size=1
 * job is a one-member cohort — rendered as a single chip, not a degenerate
 * "table with one row" that looks broken.
 */
export function RankStrip({ leases, nodeNames, className }: RankStripProps) {
  if (leases.length === 0) {
    return (
      <p className={cn("text-sm text-secondary", className)}>
        No peers assigned yet — this job is still queued.
      </p>
    );
  }

  const sorted = [...leases].sort((a, b) => a.rank - b.rank);

  return (
    <div className={cn("flex flex-wrap gap-2", className)}>
      {sorted.map((lease) => (
        <Link
          key={lease.id}
          to={`/nodes/${lease.node_id}`}
          className="flex items-center gap-2 rounded-md border border-hairline bg-elevated px-3 py-2 text-sm outline-none transition-colors hover:border-accent/50 focus-visible:ring-2 focus-visible:ring-accent"
        >
          <span className="rounded bg-base px-1.5 py-0.5 font-data text-xs text-tertiary">
            rank {lease.rank}
          </span>
          <span className="font-data font-medium text-primary">
            {nodeNames[lease.node_id] ?? shortId(lease.node_id)}
          </span>
          <StatusPill kind="lease" status={lease.state} />
        </Link>
      ))}
    </div>
  );
}
