import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, fallbackMessage } from "./client";
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

/**
 * Turn a failed response into the same ApiError shape the rest of the UI shows.
 *
 * Takes the raw body rather than a `Response` so the upload path, which uses
 * XMLHttpRequest, gets identical wording to everything on `fetch`.
 *
 * The fallback comes from `client.ts` rather than being written again here.
 * An earlier local copy always said "the orchestrator couldn't complete this
 * request", which is the one thing that is *not* true when the orchestrator
 * never answered at all — while it restarts, the Vite dev proxy replies with a
 * bare 500 whose body is not JSON, and the uploader was told their request had
 * been considered and refused.
 */
function errorFromBody(status: number, body: string): ApiError {
  let detail: string | undefined;
  try {
    const payload: unknown = JSON.parse(body);
    if (typeof payload === "object" && payload !== null && "detail" in payload) {
      const raw = (payload as { detail: unknown }).detail;
      if (typeof raw === "string") {
        detail = raw;
      } else if (Array.isArray(raw)) {
        // FastAPI request-validation failures put an array of objects here, so
        // the string branch above misses them and a wrong field name would
        // otherwise surface as a generic apology that names nothing.
        detail = raw
          .map((item) =>
            typeof item === "object" && item !== null && "msg" in item
              ? String((item as { msg: unknown }).msg)
              : "",
          )
          .filter(Boolean)
          .join("; ");
      }
    }
  } catch {
    // Not JSON: something other than the orchestrator answered. `status` is
    // enough for the shared fallback to say which situation this is.
  }
  return new ApiError(detail || fallbackMessage(status), status);
}

/** Turn a failed `fetch` response into an ApiError. */
async function toApiError(response: Response): Promise<ApiError> {
  const body = await response.text().catch(() => "");
  return errorFromBody(response.status, body);
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

/**
 * Where an upload has got to.
 *
 * Two phases, because they take comparable amounts of time on a large archive
 * and only the first one has a percentage. Once the last byte is sent the
 * server still has to validate the zip, possibly rewrite its layout, and push
 * it to object storage — a bar parked at 100% through all of that is
 * indistinguishable from a hang, which is the thing the bar exists to rule out.
 */
export interface UploadProgress {
  phase: "uploading" | "processing";
  loaded: number;
  /** Total bytes, or 0 when the browser cannot say. */
  total: number;
}

export interface UploadDatasetInput {
  name: string;
  description?: string;
  file: File;
  onProgress?: (progress: UploadProgress) => void;
}

export interface UploadDatasetResult {
  dataset: Dataset;
  summary: string;
  /**
   * What the server had to change to make the archive usable — folders renamed
   * onto `train/` and `test/`, unlabelled images skipped, a held-out split
   * carved. Empty when the archive already matched the required layout.
   *
   * Shown rather than swallowed: a rearrangement the uploader never sees is
   * indistinguishable from the server guessing at their data.
   */
  layout_notes: string[];
}

export function useUploadDatasetMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: UploadDatasetInput): Promise<UploadDatasetResult> =>
      // XMLHttpRequest rather than fetch, for the one thing fetch cannot do:
      // report how much of a request body has been sent. A 350 MB archive over
      // a home connection is minutes of a button that says "Uploading…" and
      // nothing else, which is why the first question is always whether it has
      // frozen.
      new Promise<UploadDatasetResult>((resolve, reject) => {
        const form = new FormData();
        form.append("name", input.name);
        if (input.description) form.append("description", input.description);
        form.append("file", input.file);

        const request = new XMLHttpRequest();
        request.open("POST", `${API_BASE}/datasets`);
        // Content-Type is deliberately not set: the browser has to add it
        // itself so the multipart boundary matches the body it generates.
        for (const [header, value] of Object.entries(authHeaders())) {
          request.setRequestHeader(header, value);
        }

        request.upload.onprogress = (event) => {
          input.onProgress?.({
            phase: "uploading",
            loaded: event.loaded,
            total: event.lengthComputable ? event.total : 0,
          });
        };
        request.upload.onload = () => {
          input.onProgress?.({
            phase: "processing",
            loaded: input.file.size,
            total: input.file.size,
          });
        };

        request.onload = () => {
          if (request.status >= 200 && request.status < 300) {
            try {
              resolve(JSON.parse(request.responseText) as UploadDatasetResult);
            } catch {
              reject(
                new ApiError(
                  "The upload finished but the reply could not be read.",
                  request.status,
                ),
              );
            }
            return;
          }
          reject(errorFromBody(request.status, request.responseText));
        };
        // Status 0: the request never completed, so there is no status to
        // reason about. The shared fallback words that as unreachable rather
        // than as a refusal.
        request.onerror = () => reject(new ApiError(fallbackMessage(0), 0));
        request.ontimeout = () =>
          reject(new ApiError("The upload timed out before it finished.", 0));
        request.onabort = () =>
          reject(new ApiError("The upload was cancelled.", 0));

        request.send(form);
      }),
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
