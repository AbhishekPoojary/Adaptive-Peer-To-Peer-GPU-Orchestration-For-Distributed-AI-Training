import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  useCancelJobMutation,
  useJobDetailQuery,
  useJobMetricsQuery,
  useSchedulingDecisionsQuery,
} from "@/api/jobs";
import { ApiError } from "@/api/client";
import { asJobEventDetail, asJobResult, asJobSpec, isTerminalJobState } from "@/api/types";
import { Breadcrumbs } from "@/components/Breadcrumbs";
import { ErrorState } from "@/components/ErrorState";
import { StatTile } from "@/components/StatTile";
import { StatusPill } from "@/components/StatusPill";
import { TechnicalDetails } from "@/components/TechnicalDetails";
import { UpdatedAgo } from "@/components/UpdatedAgo";
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
import { toast } from "@/hooks/use-toast";
import { formatBytes, formatTimestamp, shortId } from "@/lib/format";

export function JobDetail() {
  const { jobId } = useParams<{ jobId: string }>();
  const [confirmOpen, setConfirmOpen] = useState(false);

  const query = useJobDetailQuery(jobId);
  const isAdaptive = query.data?.scheduler_name === "adaptive";
  const decisionsQuery = useSchedulingDecisionsQuery(isAdaptive ? jobId : undefined);
  const metricsQuery = useJobMetricsQuery(jobId);
  const cancelMutation = useCancelJobMutation(jobId ?? "");

  if (query.isPending) {
    return (
      <div className="flex flex-col gap-4">
        <Breadcrumbs items={[{ label: "Jobs", to: "/jobs" }, { label: "…" }]} />
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-48 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (query.isError) {
    return (
      <div className="flex flex-col gap-4">
        <Breadcrumbs items={[{ label: "Jobs", to: "/jobs" }, { label: "Error" }]} />
        <ErrorState error={query.error} onRetry={() => query.refetch()} />
      </div>
    );
  }

  const job = query.data;
  const spec = asJobSpec(job.spec);
  const result = asJobResult(job.result);
  const canCancel = !isTerminalJobState(job.state);

  async function handleConfirmCancel() {
    try {
      await cancelMutation.mutateAsync();
      toast({ title: "Job cancelled", variant: "success" });
      setConfirmOpen(false);
    } catch (err) {
      toast({
        title: "Couldn't cancel job",
        description: err instanceof ApiError ? err.message : "Please try again.",
        variant: "destructive",
      });
    }
  }

  return (
    <div className="flex flex-col gap-5">
      <Breadcrumbs items={[{ label: "Jobs", to: "/jobs" }, { label: shortId(job.id) }]} />

      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-3">
          <h1 className="font-data text-lg font-semibold text-primary">{shortId(job.id)}</h1>
          <StatusPill kind="job" status={job.state} />
        </div>
        <div className="flex items-center gap-3">
          <UpdatedAgo dataUpdatedAt={query.dataUpdatedAt} isFetching={query.isFetching} />
          {canCancel && (
            <Button variant="destructive" size="sm" onClick={() => setConfirmOpen(true)}>
              Cancel job
            </Button>
          )}
        </div>
      </div>

      {job.failure_reason && (
        <div className="rounded-md border border-bad/40 bg-bad/10 px-3 py-2 text-sm text-primary">
          <span className="font-medium">Failure reason: </span>
          {job.failure_reason}
        </div>
      )}

      {result && (
        <section className="rounded-md border border-hairline bg-panel p-4">
          <h2 className="mb-3 text-sm font-semibold text-primary">Training result</h2>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <StatTile
              label="Final test accuracy"
              value={
                result.final_test_accuracy !== null && result.final_test_accuracy !== undefined
                  ? `${(result.final_test_accuracy * 100).toFixed(1)}%`
                  : "—"
              }
            />
            <StatTile
              label="Final loss"
              value={
                result.final_loss !== null && result.final_loss !== undefined
                  ? result.final_loss.toFixed(4)
                  : "—"
              }
            />
            <StatTile
              label="Epochs completed"
              value={result.epochs_completed !== undefined ? String(result.epochs_completed) : "—"}
            />
            <StatTile label="Device" value={result.device ?? "—"} />
          </div>
        </section>
      )}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <section className="rounded-md border border-hairline bg-panel p-4">
          <h2 className="mb-3 text-sm font-semibold text-primary">Training spec</h2>
          <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
            <Field label="Dataset" value={spec.dataset ?? "—"} />
            <Field label="Model" value={spec.model ?? "—"} />
            <Field label="Epochs" value={spec.epochs !== undefined ? String(spec.epochs) : "—"} mono />
            <Field label="Batch size" value={spec.batch_size !== undefined ? String(spec.batch_size) : "—"} mono />
            <Field label="Learning rate" value={spec.learning_rate !== undefined ? String(spec.learning_rate) : "—"} mono />
            <Field label="World size" value={spec.world_size !== undefined ? String(spec.world_size) : "—"} mono />
            <Field
              label="Min GPU memory"
              value={
                spec.min_gpu_mem_bytes === null || spec.min_gpu_mem_bytes === undefined
                  ? "CPU-eligible"
                  : formatBytes(spec.min_gpu_mem_bytes)
              }
              mono
            />
          </dl>
        </section>

        <section className="rounded-md border border-hairline bg-panel p-4">
          <h2 className="mb-3 text-sm font-semibold text-primary">Assignment</h2>
          <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
            <Field label="Scheduler" value={job.scheduler_name} mono />
            <Field label="Lease epoch" value={String(job.current_lease_epoch)} mono />
            <div className="col-span-2">
              <dt className="text-xs uppercase tracking-wide text-tertiary">Node</dt>
              <dd className="font-data text-sm text-primary">
                {job.scheduled_node_id ? (
                  <Link
                    to={`/nodes/${job.scheduled_node_id}`}
                    className="text-accent hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded"
                  >
                    {shortId(job.scheduled_node_id)}
                  </Link>
                ) : (
                  "Not yet assigned"
                )}
              </dd>
            </div>
            <Field label="Submitted by" value={job.submitted_by} />
            <Field label="Submitted at" value={formatTimestamp(job.submitted_at)} mono />
            <Field label="Started at" value={formatTimestamp(job.started_at)} mono />
            <Field label="Finished at" value={formatTimestamp(job.finished_at)} mono />
          </dl>
        </section>
      </div>

      <section className="rounded-md border border-hairline bg-panel p-4">
        <h2 className="mb-3 text-sm font-semibold text-primary">Event timeline</h2>
        {job.events.length === 0 ? (
          <p className="text-sm text-secondary">No events recorded yet.</p>
        ) : (
          <ol className="flex flex-col gap-3">
            {job.events.map((event, i) => {
              const detail = asJobEventDetail(event.detail);
              return (
                <li key={`${event.ts}-${i}`} className="flex gap-3">
                  <div className="flex flex-col items-center pt-1">
                    <span className="size-2 shrink-0 rounded-full bg-accent" />
                    {i < job.events.length - 1 && (
                      <span className="mt-1 w-px flex-1 bg-hairline" />
                    )}
                  </div>
                  <div className="flex-1 pb-1">
                    <div className="flex flex-wrap items-center gap-2">
                      {event.from_state && (
                        <>
                          <StatusPill kind="job" status={event.from_state} />
                          <span className="text-tertiary">→</span>
                        </>
                      )}
                      <StatusPill kind="job" status={event.to_state} />
                      <span className="font-data text-xs text-tertiary">
                        {formatTimestamp(event.ts)}
                      </span>
                    </div>
                    <p className="mt-1 text-sm text-primary">
                      {detail.message ?? "(no message recorded)"}
                    </p>
                  </div>
                </li>
              );
            })}
          </ol>
        )}
      </section>

      {job.leases.length > 0 && (
        <section className="rounded-md border border-hairline bg-panel p-4">
          <h2 className="mb-3 text-sm font-semibold text-primary">Leases</h2>
          <div className="flex flex-col gap-2">
            {job.leases.map((lease) => (
              <div
                key={lease.id}
                className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded border border-hairline bg-elevated px-3 py-2 text-xs"
              >
                <StatusPill kind="lease" status={lease.state} />
                <span className="font-data text-secondary">epoch {lease.lease_epoch}</span>
                <Link
                  to={`/nodes/${lease.node_id}`}
                  className="font-data text-accent hover:underline"
                >
                  node {shortId(lease.node_id)}
                </Link>
                <span className="font-data text-tertiary">
                  granted {formatTimestamp(lease.granted_at)}
                </span>
                <span className="font-data text-tertiary">
                  expires {formatTimestamp(lease.expires_at)}
                </span>
                {lease.released_at && (
                  <span className="font-data text-tertiary">
                    released {formatTimestamp(lease.released_at)}
                  </span>
                )}
              </div>
            ))}
          </div>
        </section>
      )}

      {metricsQuery.data && metricsQuery.data.metrics.length > 0 && (
        <section className="rounded-md border border-hairline bg-panel p-4">
          <h2 className="mb-3 text-sm font-semibold text-primary">Training metrics</h2>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-hairline text-tertiary">
                  <th className="px-2 py-1 text-left">Epoch</th>
                  <th className="px-2 py-1 text-right">Loss</th>
                  <th className="px-2 py-1 text-right">Test accuracy</th>
                  <th className="px-2 py-1 text-right">Recorded</th>
                </tr>
              </thead>
              <tbody>
                {metricsQuery.data.metrics.map((m) => (
                  <tr key={m.id} className="font-data text-secondary">
                    <td className="px-2 py-1 text-primary">{m.epoch}</td>
                    <td className="px-2 py-1 text-right">{m.loss.toFixed(4)}</td>
                    <td className="px-2 py-1 text-right">
                      {m.test_accuracy !== null ? `${(m.test_accuracy * 100).toFixed(1)}%` : "—"}
                    </td>
                    <td className="px-2 py-1 text-right text-tertiary">
                      {formatTimestamp(m.ts)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {isAdaptive && (
        <TechnicalDetails title="Technical details — adaptive scheduling decisions">
          {decisionsQuery.isPending ? (
            <Skeleton className="h-24 w-full" />
          ) : decisionsQuery.isError ? (
            <ErrorState error={decisionsQuery.error} onRetry={() => decisionsQuery.refetch()} />
          ) : decisionsQuery.data.decisions.length === 0 ? (
            <p className="text-sm text-secondary">
              No scheduling decisions recorded for this job yet.
            </p>
          ) : (
            <div className="flex flex-col gap-4">
              {decisionsQuery.data.decisions.map((decision) => (
                <div key={decision.id} className="flex flex-col gap-2">
                  <div className="font-data text-xs text-tertiary">
                    {formatTimestamp(decision.ts)} · weights α={decision.alpha} β=
                    {decision.beta} γ={decision.gamma} · reliability half-life{" "}
                    {decision.reliability_halflife_seconds}s · wilson_z {decision.wilson_z}
                  </div>
                  <div className="overflow-x-auto">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="border-b border-hairline text-tertiary">
                          <th className="px-2 py-1 text-left">Node</th>
                          <th className="px-2 py-1 text-right">L</th>
                          <th className="px-2 py-1 text-right">R</th>
                          <th className="px-2 py-1 text-right">D</th>
                          <th className="px-2 py-1 text-right">S</th>
                          <th className="px-2 py-1 text-right">Raw util</th>
                          <th className="px-2 py-1 text-right">Raw RTT (ms)</th>
                          <th className="px-2 py-1 text-right">Weighted OK</th>
                          <th className="px-2 py-1 text-right">Weighted fail</th>
                          <th className="px-2 py-1 text-center">Selected</th>
                        </tr>
                      </thead>
                      <tbody>
                        {decision.candidates.map((c, i) => (
                          <tr
                            key={`${c.node_id ?? "unknown"}-${i}`}
                            className={
                              c.was_selected
                                ? "bg-accent/10 font-data"
                                : "font-data text-secondary"
                            }
                          >
                            <td className="px-2 py-1">{c.node_name}</td>
                            <td className="px-2 py-1 text-right">{c.l_score.toFixed(3)}</td>
                            <td className="px-2 py-1 text-right">{c.r_score.toFixed(3)}</td>
                            <td className="px-2 py-1 text-right">{c.d_score.toFixed(3)}</td>
                            <td className="px-2 py-1 text-right">{c.s_score.toFixed(3)}</td>
                            <td className="px-2 py-1 text-right">
                              {c.raw_util !== null ? c.raw_util.toFixed(1) : "—"}
                            </td>
                            <td className="px-2 py-1 text-right">
                              {c.raw_rtt_ewma_ms !== null ? c.raw_rtt_ewma_ms.toFixed(1) : "—"}
                            </td>
                            <td className="px-2 py-1 text-right">
                              {c.weighted_success.toFixed(2)}
                            </td>
                            <td className="px-2 py-1 text-right">
                              {c.weighted_failure.toFixed(2)}
                            </td>
                            <td className="px-2 py-1 text-center">
                              {c.was_selected ? "✓" : ""}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              ))}
            </div>
          )}
        </TechnicalDetails>
      )}

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Cancel this job?</DialogTitle>
            <DialogDescription>
              This transitions the job to CANCELLED and releases its active lease,
              if any, so the node becomes free for other work. This cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="secondary" onClick={() => setConfirmOpen(false)}>
              Keep job
            </Button>
            <Button
              variant="destructive"
              onClick={() => void handleConfirmCancel()}
              disabled={cancelMutation.isPending}
            >
              {cancelMutation.isPending ? "Cancelling…" : "Cancel job"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
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
