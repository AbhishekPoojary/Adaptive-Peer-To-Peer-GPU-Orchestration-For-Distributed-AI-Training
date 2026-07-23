import type { ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ListX } from "lucide-react";
import { useJobsQuery } from "@/api/jobs";
import { asJobSpec, type JobSummary } from "@/api/types";
import { DataTable, type DataTableColumn } from "@/components/DataTable";
import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { StatusPill } from "@/components/StatusPill";
import { UpdatedAgo } from "@/components/UpdatedAgo";
import { Button } from "@/components/ui/button";
import { formatRelativeTime, formatTimestamp, shortId } from "@/lib/format";

export function JobsList() {
  const navigate = useNavigate();
  const query = useJobsQuery();

  if (query.isPending) {
    return (
      <PageShell title="Jobs">
        <DataTable columns={columns} rows={[]} getRowKey={(j) => j.id} isLoading skeletonRows={4} />
      </PageShell>
    );
  }

  if (query.isError) {
    return (
      <PageShell title="Jobs">
        <ErrorState error={query.error} onRetry={() => query.refetch()} />
      </PageShell>
    );
  }

  const jobs = query.data.jobs;

  return (
    <PageShell
      title="Jobs"
      right={<UpdatedAgo dataUpdatedAt={query.dataUpdatedAt} isFetching={query.isFetching} />}
    >
      {jobs.length === 0 ? (
        <EmptyState
          icon={<ListX className="size-8" />}
          title="No jobs yet — submit one to get started"
          description="Queue a real training job against the enrolled fleet."
          action={
            <Button asChild size="sm">
              <Link to="/submit">Submit a job</Link>
            </Button>
          }
        />
      ) : (
        <DataTable
          columns={columns}
          rows={jobs}
          getRowKey={(j) => j.id}
          onRowClick={(j) => navigate(`/jobs/${j.id}`)}
        />
      )}
    </PageShell>
  );
}

const columns: DataTableColumn<JobSummary>[] = [
  {
    key: "id",
    header: "Job",
    render: (j) => {
      const spec = asJobSpec(j.spec);
      return (
        <div className="flex flex-col">
          <span className="font-data text-xs font-medium text-primary">{shortId(j.id)}</span>
          <span className="text-xs text-secondary">
            {spec.model ?? "unknown model"} · {spec.dataset ?? "?"}
          </span>
        </div>
      );
    },
  },
  {
    key: "state",
    header: "State",
    render: (j) => <StatusPill kind="job" status={j.state} />,
  },
  {
    key: "scheduler",
    header: "Scheduler",
    render: (j) => <span className="font-data text-xs text-secondary">{j.scheduler_name}</span>,
  },
  {
    key: "node",
    header: "Node",
    render: (j) => (
      <span className="font-data text-xs text-secondary">
        {j.scheduled_node_id ? shortId(j.scheduled_node_id) : "—"}
      </span>
    ),
  },
  {
    key: "submitted",
    header: "Submitted",
    render: (j) => (
      <span className="font-data text-xs text-secondary" title={formatTimestamp(j.submitted_at)}>
        {formatRelativeTime(j.submitted_at)}
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
