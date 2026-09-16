import { Link } from "react-router-dom";
import { ArrowRight, Download } from "lucide-react";
import { useJobsQuery } from "@/api/jobs";
import { useNodesQuery } from "@/api/nodes";
import { isAdmin } from "@/api/session";
import {
  QUEUE_DEPTH_STATES,
  asJobResult,
  isTerminalJobState,
  type JobSummary,
} from "@/api/types";
import { ErrorState } from "@/components/ErrorState";
import { Figure, FigureRow } from "@/components/Figure";
import { StatusPill } from "@/components/StatusPill";
import { UpdatedAgo } from "@/components/UpdatedAgo";
import { Button } from "@/components/ui/button";
import { Panel, PanelHeader } from "@/components/ui/panel";
import { Skeleton } from "@/components/ui/skeleton";

/**
 * The front door.
 *
 * Rebuilt around the user PRODUCT.md calls primary: someone with a dataset and
 * no GPU. The previous version led with four fleet counters and made "Add a
 * node" the primary action, which answers the operator's question first and
 * leaves the visitor who came to train something hunting through a sidebar.
 *
 * So the composition inverts. The visitor's own latest run leads at two-thirds
 * width with its result and the action that follows from it; the fleet is a
 * quiet column beside it. The figure row above stays, because the honest
 * answer to "can this train my data right now" is a machine count — but every
 * figure carries the quantity that makes it mean something.
 */
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
  const inFlight = jobs.filter((j) => !isTerminalJobState(j.state));
  const queued = jobs.filter((j) => (QUEUE_DEPTH_STATES as string[]).includes(j.state));
  const modelsReady = jobs.filter(
    (j) => j.state === "COMPLETED" && asJobResult(j.result) !== null,
  );
  const latest = jobs[0];

  return (
    <div className="flex flex-col gap-7">
      <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-2">
        <h1 className="text-[1.375rem] font-semibold tracking-[-0.02em] text-ink">
          {greeting()}
        </h1>
        {nodesQuery.dataUpdatedAt > 0 && (
          <UpdatedAgo
            dataUpdatedAt={nodesQuery.dataUpdatedAt}
            isFetching={nodesQuery.isFetching || jobsQuery.isFetching}
          />
        )}
      </div>

      {isLoading ? (
        <FigureSkeleton />
      ) : (
        <FigureRow>
          <Figure
            value={liveNodes.length}
            label="Machines ready"
            measuredBy={`of ${nodes.length} ever enrolled`}
            tone={liveNodes.length > 0 ? "ok" : "caution"}
          />
          <Figure
            value={
              liveNodes.reduce((sum, n) => sum + n.hardware.gpus.length, 0) || null
            }
            label="GPUs available"
            measuredBy="reported by those machines"
            unmeasuredReason="no machine is reporting"
          />
          <Figure
            value={inFlight.length}
            label="Runs in flight"
            measuredBy={
              queued.length > 0 ? `${queued.length} still waiting for a machine` : "none waiting"
            }
          />
          <Figure
            value={modelsReady.length}
            label="Models ready"
            measuredBy="finished and downloadable"
          />
        </FigureRow>
      )}

      <div className="grid gap-5 lg:grid-cols-3 lg:items-start">
        <div className="lg:col-span-2">
          {isLoading ? (
            <Panel>
              <Skeleton className="h-5 w-40" />
              <Skeleton className="mt-4 h-24 w-full" />
            </Panel>
          ) : latest ? (
            <LatestRun job={latest} />
          ) : (
            <NothingYet />
          )}
        </div>

        <Panel>
          <PanelHeader
            title="The machines"
            hint={
              nodes.length === 0
                ? undefined
                : `${liveNodes.length} of ${nodes.length} reporting`
            }
            action={
              <Link
                to="/nodes"
                className="text-[0.8125rem] font-medium text-accent hover:underline"
              >
                All
              </Link>
            }
          />
          {isLoading ? (
            <div className="flex flex-col gap-3">
              <Skeleton className="h-9 w-full" />
              <Skeleton className="h-9 w-full" />
            </div>
          ) : nodes.length === 0 ? (
            <p className="text-[0.8125rem] text-muted">
              No machine has enrolled yet. Training needs at least one.
            </p>
          ) : (
            <ul className="-mx-1 flex flex-col">
              {nodes.slice(0, 6).map((node) => {
                const fresh = node.status === "ONLINE" && !node.heartbeat_stale;
                const gpu = node.hardware.gpus[0];
                return (
                  <li key={node.id}>
                    <Link
                      to={`/nodes/${node.id}`}
                      className="flex items-center gap-3 rounded-[9px] px-1 py-2 transition-colors duration-150 ease-out hover:bg-sunken"
                    >
                      <span
                        aria-hidden="true"
                        className={`size-2 shrink-0 rounded-full ${
                          fresh
                            ? "bg-ok"
                            : node.status === "DRAINING"
                              ? "bg-caution"
                              : "bg-nosignal"
                        }`}
                      />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate font-data text-[0.8125rem] text-ink">
                          {node.name}
                        </span>
                        <span className="block truncate text-xs text-muted">
                          {gpu ? gpu.name : "no GPU reported"}
                        </span>
                      </span>
                    </Link>
                  </li>
                );
              })}
            </ul>
          )}
          {nodes.length > 6 && (
            <Link
              to="/nodes"
              className="mt-3 block text-xs text-muted hover:text-ink"
            >
              {nodes.length - 6} more not shown
            </Link>
          )}
        </Panel>
      </div>
    </div>
  );
}

/**
 * The visitor's most recent run, and the one action that follows from it.
 *
 * Which action depends on the state, because there is exactly one sensible
 * next step at any point: watch it while it runs, collect the model when it
 * finished, read the reason when it failed.
 */
function LatestRun({ job }: { job: JobSummary }) {
  const result = asJobResult(job.result);
  const live = job.state === "RUNNING" || job.state === "LEASED";
  const done = job.state === "COMPLETED" && result !== null;
  const accuracy = result?.final_test_accuracy;

  return (
    <Panel className="flex flex-col">
      <PanelHeader
        title="Your latest run"
        live={live}
        action={<StatusPill kind="job" status={job.state} />}
      />

      <div className="flex flex-wrap items-end gap-x-10 gap-y-6">
        <div className="settle">
          <Figure
            value={
              typeof accuracy === "number"
                ? `${(accuracy * 100).toFixed(1)}%`
                : null
            }
            label="Held-out accuracy"
            measuredBy={
              typeof result?.epochs_completed === "number"
                ? `after ${result.epochs_completed} epochs on ${result.device ?? "an unnamed device"}`
                : undefined
            }
            unmeasuredReason={
              live ? "still training" : "this run reported none"
            }
            tone="ink"
          />
        </div>

        <dl className="flex flex-wrap gap-x-8 gap-y-3">
          <Detail label="Run" value={job.id.slice(0, 8)} mono />
          <Detail
            label="Submitted by"
            value={job.submitted_by}
          />
          <Detail
            label="Attempts"
            value={
              job.failed_attempt_count > 0
                ? `${job.failed_attempt_count + 1}`
                : "1"
            }
            mono
          />
        </dl>
      </div>

      {job.failure_reason && (
        <p className="mt-5 rounded-[var(--radius-control)] bg-fault-wash px-3 py-2.5 text-[0.8125rem] text-fault">
          {job.failure_reason}
        </p>
      )}

      <div className="mt-6 flex flex-wrap gap-2.5 border-t border-hairline pt-5">
        {done ? (
          <Button asChild>
            <Link to={`/jobs/${job.id}`}>
              <Download aria-hidden="true" />
              Collect the model
            </Link>
          </Button>
        ) : (
          <Button asChild>
            <Link to={`/jobs/${job.id}`}>
              {live ? "Watch it train" : "Open this run"}
              <ArrowRight aria-hidden="true" />
            </Link>
          </Button>
        )}
        <Button asChild variant="secondary">
          <Link to="/submit">Train something else</Link>
        </Button>
      </div>
    </Panel>
  );
}

function Detail({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div>
      <dt className="label">{label}</dt>
      <dd
        className={`mt-1 text-[0.8125rem] text-ink ${mono ? "font-data" : ""}`}
      >
        {value}
      </dd>
    </div>
  );
}

/**
 * The first-run state.
 *
 * Not an illustration and not a tour: the one thing a visitor with no runs
 * needs is the action that creates one, and the honest precondition for it.
 */
function NothingYet() {
  return (
    <Panel className="flex flex-col py-9">
      <h2 className="text-[1.0625rem] font-semibold tracking-[-0.01em] text-ink">
        Nothing has been trained here yet
      </h2>
      <p className="mt-2 max-w-[52ch] text-[0.8125rem] leading-relaxed text-muted">
        Bring an image dataset as a <code className="font-data text-ink">.zip</code>{" "}
        and it will be trained on whichever machines are free. You do not need a
        GPU of your own, and you do not need to arrange the folders a particular
        way — common layouts are rearranged for you.
      </p>
      <div className="mt-6 flex flex-wrap gap-2.5">
        {isAdmin() ? (
          <Button asChild>
            <Link to="/datasets">
              Upload a dataset
              <ArrowRight aria-hidden="true" />
            </Link>
          </Button>
        ) : (
          <Button asChild>
            <Link to="/submit">
              Train on a built-in dataset
              <ArrowRight aria-hidden="true" />
            </Link>
          </Button>
        )}
      </div>
      {!isAdmin() && (
        <p className="mt-4 text-xs text-muted">
          Uploading your own dataset needs an admin account — whoever runs this
          fleet can grant one.
        </p>
      )}
    </Panel>
  );
}

function FigureSkeleton() {
  return (
    <div className="grid grid-cols-2 gap-x-6 gap-y-7 lg:grid-cols-4">
      {[0, 1, 2, 3].map((i) => (
        <div key={i}>
          <Skeleton className="h-10 w-20" />
          <Skeleton className="mt-2.5 h-3 w-24" />
          <Skeleton className="mt-1.5 h-3 w-32" />
        </div>
      ))}
    </div>
  );
}

/** Time-of-day greeting. The product knows the hour; pretending otherwise
 *  would be a missed chance to sound like something a person made. */
function greeting(): string {
  const hour = new Date().getHours();
  if (hour < 5) return "Still up";
  if (hour < 12) return "Good morning";
  if (hour < 18) return "Good afternoon";
  return "Good evening";
}
