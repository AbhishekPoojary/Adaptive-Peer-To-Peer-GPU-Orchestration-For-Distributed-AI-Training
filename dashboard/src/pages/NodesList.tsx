import { useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { Plus, ServerOff } from "lucide-react";
import { useNodesQuery } from "@/api/nodes";
import type { NodeSummary } from "@/api/types";
import { AddNodeModal } from "@/components/AddNodeModal";
import { DataTable, type DataTableColumn } from "@/components/DataTable";
import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { StatusPill } from "@/components/StatusPill";
import { UpdatedAgo } from "@/components/UpdatedAgo";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
      Add a node
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
      <PageShell title="Nodes" right={addNodeButton}>
        <DataTable
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
      <PageShell title="Nodes" right={addNodeButton}>
        <ErrorState error={query.error} onRetry={() => query.refetch()} />
        {addNodeModal}
      </PageShell>
    );
  }

  const nodes = query.data.nodes;

  return (
    <PageShell
      title="Nodes"
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
          title="No nodes yet — add one to get started"
          description={
            <div className="flex flex-col gap-2 text-left">
              <p>
                A peer joins the fleet by running one command on their machine —
                click "Add a node" above to mint a one-time enrollment token and
                get that command.
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
];

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
        <h1 className="text-lg font-semibold text-primary">{title}</h1>
        {right}
      </div>
      {children}
    </div>
  );
}
