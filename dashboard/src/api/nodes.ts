import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "./client";

/** Poll interval for fleet-wide views: no WebSocket yet (REST-polled, ADR-011). */
export const POLL_INTERVAL_MS = 4000;

export function useNodesQuery() {
  return useQuery({
    queryKey: ["nodes"],
    queryFn: async () => unwrap(await api.GET("/nodes")),
    refetchInterval: POLL_INTERVAL_MS,
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
