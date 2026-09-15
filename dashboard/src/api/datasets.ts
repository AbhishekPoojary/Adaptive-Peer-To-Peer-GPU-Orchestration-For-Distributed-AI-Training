import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, fallbackMessage } from "./client";
import { getToken } from "./session";
import { formatBytes } from "@/lib/format";
import { shrinkArchive } from "./shrinkArchive";

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

/**
 * True when this page is being served through a Cloudflare quick tunnel.
 *
 * `demo.ps1 -Public` puts the dashboard behind one so a collaborator on
 * another network can reach it, and that tunnel is the one part of the path
 * with a limit nothing here controls.
 */
export function servedThroughQuickTunnel(): boolean {
  return window.location.hostname.endsWith(".trycloudflare.com");
}

/**
 * A request that ended without any response at all.
 *
 * `fallbackMessage(0)` says the orchestrator could not be reached. That is
 * right when nothing was sent and wrong when most of a file was: a far end
 * that accepted 34 MB and then stopped was reachable. Quick tunnels cut off an
 * upload that runs longer than a minute or two, and telling someone their
 * server is down when it is their *link* that cannot carry the file sends them
 * to fix the wrong thing entirely.
 */
function uploadInterrupted(sent: number, total: number): ApiError {
  if (sent <= 0 || sent >= total) return new ApiError(fallbackMessage(0), 0);
  const howFar = `The upload stopped after ${formatBytes(sent)} of ${formatBytes(total)}.`;
  return new ApiError(
    servedThroughQuickTunnel()
      ? `${howFar} The public link cuts off uploads that take more than a minute or two. Upload a file this large from the machine running the orchestrator, or use a smaller archive.`
      : `${howFar} The connection dropped part-way through.`,
    0,
  );
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
  phase: "preparing" | "uploading" | "processing";
  loaded: number;
  /** Total bytes, or 0 when the browser cannot say. */
  total: number;
}

export interface UploadDatasetInput {
  name: string;
  description?: string;
  file: File;
  /**
   * Resize images to the trainer's working size before sending.
   *
   * Removes most of the bytes at no cost to the model, because the trainer
   * resizes to that size anyway. Off means the archive is uploaded exactly as
   * chosen.
   */
  shrinkImages?: boolean;
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

/** Limits and sizes the server wants a client to plan around. */
export interface UploadLimits {
  chunk_bytes: number;
  max_upload_bytes: number;
  /** Resolution the trainer reduces every image to. */
  image_size: number;
}

export function useUploadLimitsQuery() {
  return useQuery({
    queryKey: ["dataset-upload-limits"],
    queryFn: async (): Promise<UploadLimits> => {
      const response = await fetch(`${API_BASE}/datasets/upload-limits`, {
        headers: authHeaders(),
      });
      if (!response.ok) throw await toApiError(response);
      return (await response.json()) as UploadLimits;
    },
    staleTime: Infinity,
  });
}

/** One upload session, as the orchestrator describes it. */
interface UploadSessionStatus {
  upload_id: string;
  chunk_bytes: number;
  total_chunks: number;
  total_bytes: number;
  received: number[];
}

/** Attempts per chunk before the whole upload gives up. */
const CHUNK_ATTEMPTS = 4;

function pause(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** The upload limits, or null. Never throws — see the call site. */
async function postJsonSafe(): Promise<UploadLimits | null> {
  try {
    const response = await fetch(`${API_BASE}/datasets/upload-limits`, {
      headers: authHeaders(),
    });
    if (!response.ok) return null;
    return (await response.json()) as UploadLimits;
  } catch {
    return null;
  }
}

async function postJson<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: {
      ...authHeaders(),
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw await toApiError(response);
  return (await response.json()) as T;
}

/**
 * Send one chunk, retrying a few times before giving up on the upload.
 *
 * Retrying is not belt-and-braces here, it is the mechanism. The transport this
 * exists for drops transfers unpredictably, and the endpoint makes a repeated
 * chunk replace its predecessor precisely so that resending is always safe.
 * Without this, one unlucky chunk out of ninety would still lose the archive.
 */
async function sendChunk(
  uploadId: string,
  index: number,
  piece: Blob,
): Promise<void> {
  let last: unknown;
  for (let attempt = 0; attempt < CHUNK_ATTEMPTS; attempt += 1) {
    try {
      const response = await fetch(
        `${API_BASE}/datasets/uploads/${uploadId}/chunks/${index}`,
        { method: "PUT", headers: authHeaders(), body: piece },
      );
      if (response.ok) return;
      const error = await toApiError(response);
      // A refusal is a verdict on the request, not on the connection, and it
      // will be the same verdict next time. Only a server or transport fault
      // is worth repeating.
      if (response.status < 500) throw error;
      last = error;
    } catch (err) {
      if (err instanceof ApiError && err.status !== undefined && err.status < 500) {
        throw err;
      }
      last = err;
    }
    await pause(500 * 2 ** attempt);
  }
  throw last instanceof Error
    ? last
    : new ApiError("A piece of the upload could not be sent.", 0);
}

export function useUploadDatasetMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    /**
     * Upload in chunks rather than one request.
     *
     * A single request carrying the whole archive is the obvious shape, and it
     * works right up until the dashboard is reached through something that
     * limits how long one request may run. The public Cloudflare tunnel cuts a
     * transfer off after a minute or two, which on a home upstream arrives long
     * before a large archive finishes — no amount of retrying a single request
     * gets past that, because every attempt hits the same wall.
     *
     * Split into pieces, no request runs long enough to be cut, and one that
     * fails anyway costs a piece instead of the file. Small archives take this
     * path too: one code path that always works beats two where the rare one is
     * the one that has to handle trouble.
     */
    mutationFn: async (input: UploadDatasetInput): Promise<UploadDatasetResult> => {
      let payload: File | Blob = input.file;
      let clientNotes: string[] = [];

      if (input.shrinkImages) {
        // The size comes from the server so there is one copy of it. Fetched
        // here rather than held in a hook, because this runs outside React.
        const limits = await postJsonSafe();
        if (limits) {
          input.onProgress?.({ phase: "preparing", loaded: 0, total: input.file.size });
          const outcome = await shrinkArchive(
            input.file,
            limits.image_size,
            (progress) =>
              input.onProgress?.({
                phase: "preparing",
                loaded: progress.bytesRead,
                total: progress.totalBytes,
              }),
          );
          payload = outcome.file;
          clientNotes = outcome.notes;
        }
      }

      const session = await postJson<UploadSessionStatus>("/datasets/uploads", {
        name: input.name,
        description: input.description ?? null,
        total_bytes: payload.size,
        client_notes: clientNotes,
      });

      const already = new Set(session.received);
      let sent = already.size * session.chunk_bytes;
      input.onProgress?.({ phase: "uploading", loaded: sent, total: payload.size });

      try {
        for (let index = 0; index < session.total_chunks; index += 1) {
          if (already.has(index)) continue;
          const from = index * session.chunk_bytes;
          const piece = payload.slice(from, from + session.chunk_bytes);
          await sendChunk(session.upload_id, index, piece);
          sent = Math.min(payload.size, from + piece.size);
          input.onProgress?.({
            phase: "uploading",
            loaded: sent,
            total: payload.size,
          });
        }
      } catch (err) {
        throw err instanceof ApiError && err.status === 0
          ? uploadInterrupted(sent, payload.size)
          : err;
      }

      // The bytes are up; what remains is validation, possibly a layout
      // rewrite, and the push to storage — none of which reports progress.
      input.onProgress?.({
        phase: "processing",
        loaded: payload.size,
        total: payload.size,
      });
      return await postJson<UploadDatasetResult>(
        `/datasets/uploads/${session.upload_id}/complete`,
      );
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
