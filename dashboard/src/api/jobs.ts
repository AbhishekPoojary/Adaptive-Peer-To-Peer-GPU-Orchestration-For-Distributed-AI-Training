import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "./client";
import { POLL_INTERVAL_MS } from "./nodes";
import type { JobSubmitRequest } from "./types";

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

export function useJobMetricsQuery(jobId: string | undefined) {
  return useQuery({
    queryKey: ["jobs", jobId, "metrics"],
    queryFn: async () => {
      if (!jobId) throw new Error("jobId is required");
      return unwrap(
        await api.GET("/jobs/{job_id}/metrics", { params: { path: { job_id: jobId } } }),
      );
    },
    enabled: Boolean(jobId),
    refetchInterval: POLL_INTERVAL_MS,
  });
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
