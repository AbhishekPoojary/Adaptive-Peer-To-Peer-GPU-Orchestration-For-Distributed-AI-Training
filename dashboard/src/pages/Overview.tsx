import { Gauge, ListTodo, Server, Zap } from "lucide-react";
import { useNodesQuery } from "@/api/nodes";
import { useJobsQuery } from "@/api/jobs";
import { QUEUE_DEPTH_STATES } from "@/api/types";
import { StatTile } from "@/components/StatTile";
import { ErrorState } from "@/components/ErrorState";
import { UpdatedAgo } from "@/components/UpdatedAgo";

export function Overview() {
  const nodesQuery = useNodesQuery();
  const jobsQuery = useJobsQuery();

  const isLoading = nodesQuery.isPending || jobsQuery.isPending;
  const error = nodesQuery.error ?? jobsQuery.error;

  if (error) {
    return (
      <ErrorState
        error={error}
        onRetry={() => {
          void nodesQuery.refetch();
          void jobsQuery.refetch();
        }}
      />
    );
  }

  const nodes = nodesQuery.data?.nodes ?? [];
  const jobs = jobsQuery.data?.jobs ?? [];

  const liveNodes = nodes.filter((n) => n.status === "ONLINE" && !n.heartbeat_stale);
  const runningJobs = jobs.filter((j) => j.state === "RUNNING");
  const totalGpus = nodes.reduce((sum, n) => sum + n.hardware.gpus.length, 0);
  const queueDepth = jobs.filter((j) =>
    (QUEUE_DEPTH_STATES as string[]).includes(j.state),
  );

  return (
    <div className="flex flex-col gap-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold text-primary">Cluster overview</h1>
          <p className="text-sm text-secondary">
            Live aggregate counts from the node registry and job queue.
          </p>
        </div>
        {nodesQuery.dataUpdatedAt > 0 && (
          <UpdatedAgo
            dataUpdatedAt={nodesQuery.dataUpdatedAt}
            isFetching={nodesQuery.isFetching || jobsQuery.isFetching}
          />
        )}
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatTile
          label="Live nodes"
          value={`${liveNodes.length} / ${nodes.length}`}
          hint="Online and reporting fresh heartbeats"
          icon={<Server className="size-3.5" aria-hidden="true" />}
          isLoading={isLoading}
        />
        <StatTile
          label="Running jobs"
          value={runningJobs.length}
          hint={`${jobs.length} total submitted`}
          icon={<Gauge className="size-3.5" aria-hidden="true" />}
          isLoading={isLoading}
        />
        <StatTile
          label="Total GPUs"
          value={totalGpus}
          hint="Summed across every enrolled node"
          icon={<Zap className="size-3.5" aria-hidden="true" />}
          isLoading={isLoading}
        />
        <StatTile
          label="Queue depth"
          value={queueDepth.length}
          hint="Queued or scheduled, not yet leased"
          icon={<ListTodo className="size-3.5" aria-hidden="true" />}
          isLoading={isLoading}
        />
      </div>

      {!isLoading && nodes.length === 0 && jobs.length === 0 && (
        <p className="rounded-md border border-hairline bg-panel px-4 py-3 text-sm text-secondary">
          The cluster is empty — no nodes have enrolled and no jobs have been
          submitted yet. Head to <strong className="text-primary">Nodes</strong> for
          the enrollment command, or <strong className="text-primary">Submit</strong>{" "}
          to queue the first training job.
        </p>
      )}
    </div>
  );
}
