import {
  Ban,
  CheckCircle2,
  Clock,
  HelpCircle,
  Loader2,
  PauseCircle,
  PlayCircle,
  RotateCcw,
  XCircle,
} from "lucide-react";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

type Variant = NonNullable<BadgeProps["variant"]>;

interface StatusSpec {
  label: string;
  variant: Variant;
  icon: typeof CheckCircle2;
}

/**
 * Every real enum value from the backend, mapped exactly once. Status color
 * is never used alone — label + icon always accompany it (hard requirement).
 * An unrecognized value (a future backend enum addition this component
 * hasn't been taught yet) renders honestly as itself in a neutral badge
 * rather than being hidden or guessed at.
 */
const NODE_STATUS: Record<string, StatusSpec> = {
  ONLINE: { label: "Online", variant: "good", icon: CheckCircle2 },
  OFFLINE: { label: "Offline", variant: "bad", icon: XCircle },
  DRAINING: { label: "Draining", variant: "warn", icon: PauseCircle },
};

// orchestrator/models/job.py JobState
const JOB_STATE: Record<string, StatusSpec> = {
  QUEUED: { label: "Queued", variant: "warn", icon: Clock },
  SCHEDULED: { label: "Scheduled", variant: "warn", icon: Clock },
  LEASED: { label: "Leased", variant: "active", icon: Loader2 },
  RUNNING: { label: "Running", variant: "active", icon: PlayCircle },
  COMPLETED: { label: "Completed", variant: "good", icon: CheckCircle2 },
  FAILED: { label: "Failed", variant: "bad", icon: XCircle },
  REASSIGNED: { label: "Reassigned", variant: "warn", icon: RotateCcw },
  CANCELLED: { label: "Cancelled", variant: "neutral", icon: Ban },
};

// orchestrator/models/lease.py LeaseState
const LEASE_STATE: Record<string, StatusSpec> = {
  PENDING: { label: "Pending", variant: "warn", icon: Clock },
  ACTIVE: { label: "Active", variant: "active", icon: PlayCircle },
  EXPIRED: { label: "Expired", variant: "bad", icon: XCircle },
  // Neutral, not "bad": the offer lapsed unclaimed, which costs the node
  // nothing (docs/adr/ADR-003-addendum.md).
  UNCLAIMED: { label: "Unclaimed", variant: "neutral", icon: Clock },
  RELEASED: { label: "Released", variant: "neutral", icon: Ban },
  COMPLETED: { label: "Completed", variant: "good", icon: CheckCircle2 },
  FAILED: { label: "Failed", variant: "bad", icon: XCircle },
};

const TABLES = {
  node: NODE_STATUS,
  job: JOB_STATE,
  lease: LEASE_STATE,
} as const;

export interface StatusPillProps {
  kind: keyof typeof TABLES;
  status: string;
  className?: string;
}

export function StatusPill({ kind, status, className }: StatusPillProps) {
  const spec = TABLES[kind][status] ?? {
    label: status,
    variant: "neutral" as Variant,
    icon: HelpCircle,
  };
  const Icon = spec.icon;
  return (
    <Badge variant={spec.variant} className={cn(className)}>
      <Icon className="size-3" aria-hidden="true" />
      {spec.label}
    </Badge>
  );
}
