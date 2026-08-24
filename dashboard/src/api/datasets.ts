import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "./client";
import { getToken } from "./session";

/**
 * Dataset surface for the dashboard (ADR-014).
 *
 * Written against `fetch` rather than the generated `api` client because
 * `schema.gen.ts` predates these routes, and because the upload is
 * `multipart/form-data` — openapi-fetch's JSON body handling is the wrong shape
 * for it regardless. Run `npm run generate:api` against a running orchestrator
 * to pick the routes up; the list/detail calls can then move over, but the
 * upload will still want its own path.
 */

const API_BASE = "/api";

export interface Dataset {
  id: string;
  name: string;
  description: string | null;
  sha256: string;
  size_bytes: number;
  classes: string[];
  num_classes: number;
  train_images: number;
  test_images: number;
  per_class_counts: { train: Record<string, number>; test: Record<string, number> };
  created_by: string;
  created_at: string;
}

function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/** Turn a failed response into the same ApiError shape the rest of the UI shows. */
async function toApiError(response: Response): Promise<ApiError> {
  const payload: unknown = await response.json().catch(() => null);
  const detail =
    typeof payload === "object" &&
    payload !== null &&
    "detail" in payload &&
    typeof (payload as { detail?: unknown }).detail === "string"
      ? (payload as { detail: string }).detail
      : undefined;
  return new ApiError(
    detail ?? "The orchestrator couldn't complete this request.",
    response.status,
  );
}

export function useDatasetsQuery() {
  return useQuery({
    queryKey: ["datasets"],
    queryFn: async (): Promise<Dataset[]> => {
      const response = await fetch(`${API_BASE}/datasets`, {
        headers: authHeaders(),
      });
      if (!response.ok) throw await toApiError(response);
      const body = (await response.json()) as { datasets: Dataset[] };
      return body.datasets;
    },
  });
}

export interface UploadDatasetInput {
  name: string;
  description?: string;
  file: File;
}

export interface UploadDatasetResult {
  dataset: Dataset;
  summary: string;
}

export function useUploadDatasetMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: UploadDatasetInput): Promise<UploadDatasetResult> => {
      const form = new FormData();
      form.append("name", input.name);
      if (input.description) form.append("description", input.description);
      form.append("file", input.file);

      // Content-Type is deliberately not set: the browser has to add it itself
      // so the multipart boundary matches the body it generates.
      const response = await fetch(`${API_BASE}/datasets`, {
        method: "POST",
        headers: authHeaders(),
        body: form,
      });
      if (!response.ok) throw await toApiError(response);
      return (await response.json()) as UploadDatasetResult;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["datasets"] });
    },
  });
}

export function useDeleteDatasetMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (datasetId: string): Promise<void> => {
      const response = await fetch(`${API_BASE}/datasets/${datasetId}`, {
        method: "DELETE",
        headers: authHeaders(),
      });
      if (!response.ok) throw await toApiError(response);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["datasets"] });
    },
  });
}
