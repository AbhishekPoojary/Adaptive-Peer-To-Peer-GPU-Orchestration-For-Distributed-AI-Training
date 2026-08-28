import { useEffect, useRef, useState } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, unwrap } from "./client";
import { POLL_INTERVAL_MS } from "./nodes";
import { getToken } from "./session";
import type { JobSubmitRequest, TrainingLogLine } from "./types";

export function useJobsQuery() {
  return useQuery({
    queryKey: ["jobs"],
    queryFn: async () => unwrap(await api.GET("/jobs")),
    refetchInterval: POLL_INTERVAL_MS,
  });
}

async function fetchJobDetail(jobId: string) {
  return unwrap(await api.GET("/jobs/{job_id}", { params: { path: { job_id: jobId } } }));
}

function jobDetailQueryOptions(jobId: string) {
  return {
    queryKey: ["jobs", jobId, "detail"] as const,
    queryFn: () => fetchJobDetail(jobId),
    refetchInterval: POLL_INTERVAL_MS,
  };
}

export function useJobDetailQuery(jobId: string | undefined) {
  return useQuery({
    ...jobDetailQueryOptions(jobId ?? ""),
    enabled: Boolean(jobId),
  });
}

/**
 * Fetches every job's full detail (leases included) so a node's page can
 * derive its real lease history by cross-referencing `lease.node_id` — the
 * node endpoints themselves don't expose leases, only jobs do. Cheap at this
 * milestone's dev/demo scale; each result is cached under the same key
 * `useJobDetailQuery` uses, so visiting a job page afterwards is free.
 */
export function useAllJobDetails(jobIds: string[]) {
  return useQueries({
    queries: jobIds.map((id) => jobDetailQueryOptions(id)),
  });
}

export function useSchedulingDecisionsQuery(jobId: string | undefined) {
  return useQuery({
    queryKey: ["jobs", jobId, "scheduling-decisions"],
    queryFn: async () => {
      if (!jobId) throw new Error("jobId is required");
      return unwrap(
        await api.GET("/jobs/{job_id}/scheduling-decisions", {
          params: { path: { job_id: jobId } },
        }),
      );
    },
    enabled: Boolean(jobId),
  });
}

/**
 * `pollWhileLive`: keep polling only while the job is in a non-terminal
 * state (the job detail page passes `!isTerminalJobState(job.state)`) — once
 * a job finishes there is nothing new to fetch, so polling stops rather than
 * hitting the API forever for a page left open.
 */
export function useJobMetricsQuery(jobId: string | undefined, pollWhileLive: boolean) {
  return useQuery({
    queryKey: ["jobs", jobId, "metrics"],
    queryFn: async () => {
      if (!jobId) throw new Error("jobId is required");
      return unwrap(
        await api.GET("/jobs/{job_id}/metrics", { params: { path: { job_id: jobId } } }),
      );
    },
    enabled: Boolean(jobId),
    refetchInterval: pollWhileLive ? POLL_INTERVAL_MS : false,
  });
}

// --- Live log streaming (M7): cursor-based polling ---------------------------

const LOG_POLL_INTERVAL_MS = 1500;
/** Generous first page: covers a whole short demo run's transcript in one
 * request; later pages only ever carry what's new since the last cursor. */
const LOG_PAGE_SIZE = 500;

export interface UseJobLogsResult {
  /** All lines fetched so far, oldest first. Reset whenever `jobId` changes. */
  lines: TrainingLogLine[];
  /** True on the very first fetch only (never re-flashes empty on later polls). */
  isLoading: boolean;
  /** Set when the most recent poll failed; cleared the moment a poll succeeds
   * again. The UI renders this as a quiet "reconnecting…" note, never a hard
   * error, since the accumulated lines are still shown and polling keeps
   * retrying on its own schedule. */
  isReconnecting: boolean;
}

/**
 * Poll `GET /jobs/{id}/logs` with a cursor, accumulating only genuinely new
 * lines client-side. Stops polling once `pollWhileLive` is false (the job
 * reached a terminal state) — the transcript up to that point stays visible.
 *
 * Assumes the caller remounts this hook's owning component when `jobId`
 * changes (App.tsx keys the job detail route by `:jobId`) — so there is no
 * "reset accumulated state when the job changes" bookkeeping here; a fresh
 * `jobId` always means a fresh component instance and fresh initial state.
 */
export function useJobLogs(
  jobId: string | undefined,
  pollWhileLive: boolean,
): UseJobLogsResult {
  const [lines, setLines] = useState<TrainingLogLine[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isReconnecting, setIsReconnecting] = useState(false);
  const cursorRef = useRef<number | null>(null);

  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function poll() {
      try {
        const body = unwrap(
          await api.GET("/jobs/{job_id}/logs", {
            params: {
              path: { job_id: jobId as string },
              query: { after: cursorRef.current ?? undefined, limit: LOG_PAGE_SIZE },
            },
          }),
        );
        if (cancelled) return;
        if (body.lines.length > 0) {
          setLines((prev) => [...prev, ...body.lines]);
        }
        cursorRef.current = body.next_after ?? cursorRef.current;
        setIsReconnecting(false);
      } catch {
        if (!cancelled) setIsReconnecting(true);
      } finally {
        if (!cancelled) setIsLoading(false);
      }
      if (!cancelled && pollWhileLive) {
        timer = setTimeout(() => void poll(), LOG_POLL_INTERVAL_MS);
      }
    }

    void poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [jobId, pollWhileLive]);

  return { lines, isLoading, isReconnecting };
}

export function useSubmitJobMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: JobSubmitRequest) =>
      unwrap(await api.POST("/jobs", { body })),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
  });
}

export function useCancelJobMutation(jobId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/jobs/{job_id}/cancel", {
          params: { path: { job_id: jobId } },
        }),
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
      void queryClient.invalidateQueries({ queryKey: ["jobs", jobId] });
    },
  });
}

/**
 * This job's trained model, if it saved one (ADR-006 addendum 2).
 *
 * Written against `fetch` because `schema.gen.ts` predates the route. A 404 is
 * an ordinary answer — plenty of jobs never checkpoint, because checkpointing
 * needs object storage configured on the peer that ran them — so it resolves to
 * `null` rather than throwing. A 503 (storage unreachable) *does* throw, because
 * "we cannot tell" is genuinely different from "there is none".
 */
export interface JobCheckpoint {
  key: string;
  step: number;
  epoch: number;
  loss: number | null;
  world_size: number;
  timestamp_utc: string;
  size_bytes: number | null;
  download_path: string;
  filename: string;
  format: string;
}

export function useJobCheckpointQuery(jobId: string | undefined) {
  return useQuery({
    queryKey: ["jobs", jobId, "checkpoint"],
    enabled: Boolean(jobId),
    retry: false,
    queryFn: async (): Promise<JobCheckpoint | null> => {
      const token = getToken();
      const response = await fetch(`/api/jobs/${jobId}/checkpoint`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (response.status === 404) return null;
      if (!response.ok) {
        const payload: unknown = await response.json().catch(() => null);
        const detail =
          typeof payload === "object" &&
          payload !== null &&
          "detail" in payload &&
          typeof (payload as { detail?: unknown }).detail === "string"
            ? (payload as { detail: string }).detail
            : "Couldn't check for a saved model.";
        throw new ApiError(detail, response.status);
      }
      return (await response.json()) as JobCheckpoint;
    },
  });
}
