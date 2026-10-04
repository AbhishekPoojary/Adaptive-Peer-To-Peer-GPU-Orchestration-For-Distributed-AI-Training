import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, fallbackMessage, unwrap } from "./client";

/** Poll interval for fleet-wide views: no WebSocket yet (REST-polled, ADR-011). */
export const POLL_INTERVAL_MS = 4000;

export function useNodesQuery() {
  return useQuery({
    queryKey: ["nodes"],
    queryFn: async () => unwrap(await api.GET("/nodes")),
    refetchInterval: POLL_INTERVAL_MS,
  });
}

/** Fast poll interval used only while the "Add a node" modal is open and
 * actively watching for a real new enrollment — separate from the fleet-wide
 * 4s cadence so the modal can honestly claim "checking every ~2s". */
const NODE_WATCH_INTERVAL_MS = 2000;

/**
 * Same `GET /nodes` data as `useNodesQuery`, on its own query key so its
 * faster interval never fights the app-wide 4s poll. Only actually polls
 * while `enabled` (the modal being open and still waiting for a connection).
 */
export function useWatchForNewNodeQuery(enabled: boolean) {
  return useQuery({
    queryKey: ["nodes", "add-node-watch"],
    queryFn: async () => unwrap(await api.GET("/nodes")),
    enabled,
    refetchInterval: enabled ? NODE_WATCH_INTERVAL_MS : false,
  });
}

export function useNodeDetailQuery(nodeId: string | undefined, samples = 100) {
  return useQuery({
    queryKey: ["nodes", nodeId, "detail", samples],
    queryFn: async () => {
      if (!nodeId) throw new Error("nodeId is required");
      return unwrap(
        await api.GET("/nodes/{node_id}", {
          params: { path: { node_id: nodeId }, query: { samples } },
        }),
      );
    },
    enabled: Boolean(nodeId),
    refetchInterval: POLL_INTERVAL_MS,
  });
}

/**
 * Remove a machine from the fleet (admin only; DELETE /nodes/{id}).
 *
 * Its history stays -- the jobs it trained still name it -- but it leaves the
 * fleet list and its agent can no longer sign in. 204 has no body, which
 * `unwrap` would read as an empty response, so errors are mapped here.
 */
export function useRemoveNodeMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (nodeId: string) => {
      const result = await api.DELETE("/nodes/{node_id}", {
        params: { path: { node_id: nodeId } },
      });
      if (result.error !== undefined) {
        const detail = (result.error as { detail?: unknown }).detail;
        throw new ApiError(
          typeof detail === "string" ? detail : fallbackMessage(result.response.status),
          result.response.status,
        );
      }
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["nodes"] }),
  });
}
