import { useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { Plus, ServerOff } from "lucide-react";
import { useNodesQuery, useRemoveNodeMutation } from "@/api/nodes";
import { getUser, isAdmin } from "@/api/session";
import type { NodeSummary } from "@/api/types";
import { AddNodeModal } from "@/components/AddNodeModal";
import { DataTable, type DataTableColumn } from "@/components/DataTable";
import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { StatusPill } from "@/components/StatusPill";
import { UpdatedAgo } from "@/components/UpdatedAgo";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { formatBytes, formatPercent, formatRelativeTime } from "@/lib/format";

function hardwareSummary(node: NodeSummary): string {
  const { cores, ram_bytes, gpus } = node.hardware;
  const gpuPart =
    gpus.length === 0
      ? "CPU only"
      : gpus.map((g) => `${g.name} (${formatBytes(g.vram_bytes)})`).join(", ");
  return `${cores} cores · ${formatBytes(ram_bytes)} RAM · ${gpuPart}`;
}

function telemetrySummary(node: NodeSummary): string {
  const t = node.latest_telemetry;
  if (!t) return "No telemetry yet";
  const gpuUtil = t.gpu && t.gpu.length > 0 ? t.gpu.map((g) => g.util_percent) : null;
  const gpuPart = gpuUtil
    ? `GPU ${formatPercent(gpuUtil.reduce((a, b) => a + b, 0) / gpuUtil.length)}`
    : "GPU —";
  return `CPU ${formatPercent(t.cpu_percent)} · ${gpuPart}`;
}

export function NodesList() {
  const navigate = useNavigate();
  const query = useNodesQuery();
  const [addNodeOpen, setAddNodeOpen] = useState(false);

  const addNodeButton = (
    <Button size="sm" onClick={() => setAddNodeOpen(true)}>
      <Plus className="size-3.5" aria-hidden="true" />
      Add a computer
    </Button>
  );
  const addNodeModal = (
    <AddNodeModal
      key={addNodeOpen ? "open" : "closed"}
      open={addNodeOpen}
      onOpenChange={setAddNodeOpen}
      existingNodes={query.data?.nodes ?? []}
    />
  );

  if (query.isPending) {
    return (
      <PageShell title="Machines" right={addNodeButton}>
        <DataTable
      maxBodyHeight="max-h-[calc(100vh-15rem)]"
          columns={columns}
          rows={[]}
          getRowKey={(n) => n.id}
          isLoading
          skeletonRows={4}
        />
        {addNodeModal}
      </PageShell>
    );
  }

  if (query.isError) {
    return (
      <PageShell title="Machines" right={addNodeButton}>
        <ErrorState error={query.error} onRetry={() => query.refetch()} />
        {addNodeModal}
      </PageShell>
    );
  }

  const nodes = query.data.nodes;

  return (
    <PageShell
      title="Machines"
      right={
        <div className="flex items-center gap-3">
          <UpdatedAgo dataUpdatedAt={query.dataUpdatedAt} isFetching={query.isFetching} />
          {addNodeButton}
        </div>
      }
    >
      {nodes.length === 0 ? (
        <EmptyState
          icon={<ServerOff className="size-8" />}
          title="No computers yet — add one to get started"
          description={
            <div className="flex flex-col gap-2 text-left">
              <p>
                A computer joins by running one command on it — click "Add a
                computer" above to get that command. Anyone signed in can lend
                their own.
              </p>
            </div>
          }
        />
      ) : (
        <DataTable
          columns={columns}
          rows={nodes}
          getRowKey={(n) => n.id}
          onRowClick={(n) => navigate(`/nodes/${n.id}`)}
        />
      )}
      {addNodeModal}
    </PageShell>
  );
}

const columns: DataTableColumn<NodeSummary>[] = [
  {
    key: "name",
    header: "Name",
    render: (n) => <span className="font-data font-medium">{n.name}</span>,
  },
  {
    key: "status",
    header: "Status",
    render: (n) => (
      <div className="flex flex-col gap-1">
        <StatusPill kind="node" status={n.status} />
        {n.heartbeat_stale && (
          <Badge variant="warn" className="w-fit">
            Heartbeat stale
          </Badge>
        )}
      </div>
    ),
  },
  {
    key: "hardware",
    header: "Hardware",
    render: (n) => (
      <span className="font-data text-xs text-secondary">{hardwareSummary(n)}</span>
    ),
  },
  {
    key: "telemetry",
    header: "Latest telemetry",
    render: (n) => (
      <span className="font-data text-xs text-secondary">{telemetrySummary(n)}</span>
    ),
  },
  {
    key: "heartbeat",
    header: "Last heartbeat",
    render: (n) => (
      <span className="font-data text-xs text-secondary">
        {formatRelativeTime(n.last_heartbeat_at)}
      </span>
    ),
  },
  {
    key: "reliability",
    header: "Reliability",
    render: (n) => (
      <span className="font-data text-xs text-secondary">
        {n.lease_success_count} ok / {n.lease_failure_count} failed
      </span>
    ),
  },
  {
    key: "actions",
    header: "",
    // Offline machines only, for admins and for whoever added the machine
    // (the API enforces both, and refuses a node holding live work). An online
    // machine is contributing; removing it is never the cleanup this is for.
    render: (n) =>
      canRemove(n) && n.status !== "ONLINE" ? <RemoveNodeButton node={n} /> : null,
  },
];

/** Admins remove any machine; a lender removes the ones they added. */
function canRemove(node: NodeSummary): boolean {
  const me = getUser();
  return isAdmin() || (me !== null && node.enrolled_by === me.username);
}

export function RemoveNodeButton({ node }: { node: NodeSummary }) {
  const [open, setOpen] = useState(false);
  const remove = useRemoveNodeMutation();

  return (
    // The row opens the node's page on click, and React bubbles events from
    // the dialog's portal -- its backdrop included -- through this tree. One
    // boundary here keeps every click in the control and its dialog local.
    <div onClick={(event) => event.stopPropagation()}>
      <Button
        size="sm"
        variant="ghost"
        onClick={() => {
          remove.reset();
          setOpen(true);
        }}
      >
        Remove
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Remove {node.name}?</DialogTitle>
            <DialogDescription>
              It leaves the Machines list and its agent can no longer connect. Jobs it
              trained keep their record of it. To bring the machine back, add it again
              with a new install command.
            </DialogDescription>
          </DialogHeader>
          {remove.error && (
            <p role="alert" className="text-sm text-fault">
              {remove.error.message}
            </p>
          )}
          <DialogFooter>
            <Button variant="secondary" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={remove.isPending}
              onClick={() =>
                remove.mutate(node.id, { onSuccess: () => setOpen(false) })
              }
            >
              {remove.isPending ? "Removing…" : "Remove machine"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function PageShell({
  title,
  right,
  children,
}: {
  title: string;
  right?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h1 className="text-[1.375rem] font-semibold tracking-[-0.02em] text-ink">{title}</h1>
        {right}
      </div>
      {children}
    </div>
  );
}
