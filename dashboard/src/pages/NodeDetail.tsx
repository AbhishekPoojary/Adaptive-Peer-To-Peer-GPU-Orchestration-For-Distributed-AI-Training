import { useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useNodeDetailQuery } from "@/api/nodes";
import { useAllJobDetails, useJobsQuery } from "@/api/jobs";
import { asJobSpec, type GpuTelemetry, type Lease, type TelemetrySample } from "@/api/types";
import { Breadcrumbs } from "@/components/Breadcrumbs";
import { DataTable, type DataTableColumn } from "@/components/DataTable";
import { ErrorState } from "@/components/ErrorState";
import { StatusPill } from "@/components/StatusPill";
import { StatTile } from "@/components/StatTile";
import { UpdatedAgo } from "@/components/UpdatedAgo";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  formatBytes,
  formatMs,
  formatPercent,
  formatRelativeTime,
  formatTemperature,
  formatTimestamp,
  shortId,
} from "@/lib/format";

const SAMPLE_OPTIONS = ["25", "50", "100", "250"] as const;

function gpuSummary(gpu: GpuTelemetry[] | null): string {
  if (!gpu || gpu.length === 0) return "—";
  return gpu
    .map(
      (g, i) =>
        `${gpu.length > 1 ? `GPU${i} ` : ""}${formatPercent(g.util_percent)} util · ` +
        `${formatBytes(g.mem_used_bytes)}/${formatBytes(g.mem_total_bytes)} · ` +
        `${formatTemperature(g.temperature_c)}`,
    )
    .join(" · ");
}

export function NodeDetail() {
  const { nodeId } = useParams<{ nodeId: string }>();
  const navigate = useNavigate();
  const [samples, setSamples] = useState<(typeof SAMPLE_OPTIONS)[number]>("100");

  const query = useNodeDetailQuery(nodeId, Number(samples));
  const jobsQuery = useJobsQuery();
  const jobIds = useMemo(() => (jobsQuery.data?.jobs ?? []).map((j) => j.id), [jobsQuery.data]);
  const jobDetailResults = useAllJobDetails(jobIds);

  if (query.isPending) {
    return (
      <div className="flex flex-col gap-4">
        <Breadcrumbs items={[{ label: "Nodes", to: "/nodes" }, { label: "…" }]} />
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (query.isError) {
    return (
      <div className="flex flex-col gap-4">
        <Breadcrumbs items={[{ label: "Nodes", to: "/nodes" }, { label: "Error" }]} />
        <ErrorState error={query.error} onRetry={() => query.refetch()} />
      </div>
    );
  }

  const node = query.data;

  // Derive this node's real lease history by cross-referencing every job's
  // lease list (the node endpoints don't expose leases directly — see
  // useAllJobDetails). Nothing here is invented: a job whose detail hasn't
  // loaded yet is simply not counted yet, not padded with a placeholder.
  const leaseRows: { lease: Lease; jobId: string; jobModel: string }[] = [];
  for (const result of jobDetailResults) {
    const job = result.data;
    if (!job) continue;
    for (const lease of job.leases) {
      if (lease.node_id === node.id) {
        leaseRows.push({ lease, jobId: job.id, jobModel: asJobSpec(job.spec).model ?? job.id });
      }
    }
  }
  leaseRows.sort((a, b) => (a.lease.granted_at < b.lease.granted_at ? 1 : -1));
  const leasesStillLoading = jobDetailResults.some((r) => r.isPending) && jobIds.length > 0;

  return (
    <div className="flex flex-col gap-5">
      <Breadcrumbs items={[{ label: "Nodes", to: "/nodes" }, { label: node.name }]} />

      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-3">
          <h1 className="font-data text-lg font-semibold text-primary">{node.name}</h1>
          <StatusPill kind="node" status={node.status} />
          {node.heartbeat_stale && <Badge variant="warn">Heartbeat stale</Badge>}
        </div>
        <UpdatedAgo dataUpdatedAt={query.dataUpdatedAt} isFetching={query.isFetching} />
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <StatTile
          label="Reliability"
          value={`${node.lease_success_count} / ${node.lease_success_count + node.lease_failure_count}`}
          hint="Successful leases / total"
        />
        <StatTile
          label="Last heartbeat"
          value={formatRelativeTime(node.last_heartbeat_at)}
          hint={formatTimestamp(node.last_heartbeat_at)}
        />
        <StatTile
          label="RTT (EWMA)"
          value={formatMs(node.latest_telemetry?.rtt_ewma_ms ?? null)}
          hint="Smoothed round-trip time"
        />
      </div>

      <section className="rounded-md border border-hairline bg-panel p-4">
        <h2 className="mb-3 text-sm font-semibold text-primary">Hardware</h2>
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-4">
          <Field label="Hostname" value={node.hardware.hostname} mono />
          <Field label="OS" value={node.hardware.os} />
          <Field label="CPU" value={node.hardware.cpu_model} />
          <Field label="Cores" value={String(node.hardware.cores)} mono />
          <Field label="RAM" value={formatBytes(node.hardware.ram_bytes)} mono />
          <Field
            label="GPUs"
            value={
              node.hardware.gpus.length === 0
                ? "None (CPU-only node)"
                : node.hardware.gpus
                    .map((g) => `${g.name} (${formatBytes(g.vram_bytes)})`)
                    .join(", ")
            }
            mono
          />
        </dl>
      </section>

      <section className="rounded-md border border-hairline bg-panel p-4">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-primary">Telemetry history</h2>
          <div className="flex items-center gap-2">
            <span className="text-xs text-tertiary">Samples</span>
            <Select value={samples} onValueChange={(v) => setSamples(v as typeof samples)}>
              <SelectTrigger className="h-7 w-20 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {SAMPLE_OPTIONS.map((o) => (
                  <SelectItem key={o} value={o}>
                    {o}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>
        {node.telemetry_samples.length === 0 ? (
          <p className="text-sm text-secondary">
            No telemetry recorded yet — this node hasn't sent a heartbeat.
          </p>
        ) : (
          <DataTable
            columns={telemetryColumns}
            rows={node.telemetry_samples}
            getRowKey={(s) => s.ts}
            className="max-h-96 overflow-y-auto"
          />
        )}
      </section>

      <section className="rounded-md border border-hairline bg-panel p-4">
        <h2 className="mb-3 text-sm font-semibold text-primary">Lease history</h2>
        {jobsQuery.isPending || leasesStillLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : leaseRows.length === 0 ? (
          <p className="text-sm text-secondary">
            No leases recorded for this node yet.
          </p>
        ) : (
          <DataTable
            columns={leaseColumns}
            rows={leaseRows}
            getRowKey={(r) => r.lease.id}
            onRowClick={(r) => navigate(`/jobs/${r.jobId}`)}
          />
        )}
      </section>
    </div>
  );
}

function Field({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-tertiary">{label}</dt>
      <dd className={mono ? "font-data text-sm text-primary" : "text-sm text-primary"}>
        {value}
      </dd>
    </div>
  );
}

const telemetryColumns: DataTableColumn<TelemetrySample>[] = [
  {
    key: "ts",
    header: "Time",
    render: (s) => <span className="font-data text-xs">{formatTimestamp(s.ts)}</span>,
  },
  {
    key: "cpu",
    header: "CPU",
    render: (s) => <span className="font-data text-xs">{formatPercent(s.cpu_percent)}</span>,
  },
  {
    key: "ram",
    header: "RAM",
    render: (s) => (
      <span className="font-data text-xs">
        {formatBytes(s.ram_used_bytes)}/{formatBytes(s.ram_total_bytes)}
      </span>
    ),
  },
  {
    key: "gpu",
    header: "GPU",
    render: (s) => <span className="font-data text-xs">{gpuSummary(s.gpu)}</span>,
  },
  {
    key: "rtt",
    header: "RTT / EWMA",
    render: (s) => (
      <span className="font-data text-xs">
        {formatMs(s.rtt_ms)} / {formatMs(s.rtt_ewma_ms)}
      </span>
    ),
  },
];

const leaseColumns: DataTableColumn<{ lease: Lease; jobId: string; jobModel: string }>[] = [
  {
    key: "job",
    header: "Job",
    render: (r) => <span className="font-data text-xs">{shortId(r.jobId)} · {r.jobModel}</span>,
  },
  {
    key: "epoch",
    header: "Epoch",
    render: (r) => <span className="font-data text-xs">{r.lease.lease_epoch}</span>,
  },
  {
    key: "state",
    header: "State",
    render: (r) => <StatusPill kind="lease" status={r.lease.state} />,
  },
  {
    key: "granted",
    header: "Granted",
    render: (r) => <span className="font-data text-xs">{formatTimestamp(r.lease.granted_at)}</span>,
  },
  {
    key: "expires",
    header: "Expires",
    render: (r) => <span className="font-data text-xs">{formatTimestamp(r.lease.expires_at)}</span>,
  },
  {
    key: "released",
    header: "Released",
    render: (r) => (
      <span className="font-data text-xs">
        {r.lease.released_at ? formatTimestamp(r.lease.released_at) : "—"}
      </span>
    ),
  },
];
