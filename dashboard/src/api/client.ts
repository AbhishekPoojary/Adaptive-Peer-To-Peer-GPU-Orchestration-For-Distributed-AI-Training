import createClient from "openapi-fetch";
import type { paths } from "./schema.gen";

/**
 * Typed fetch client generated against the live orchestrator schema
 * (http://localhost:8090/openapi.json via `npm run generate:api`).
 *
 * `baseUrl` is "/api": in dev, Vite's server.proxy (vite.config.ts) forwards
 * everything under /api to the orchestrator at http://localhost:8090 (prefix
 * stripped before forwarding), so requests are same-origin from the
 * browser's perspective and no CORS configuration is needed on the
 * orchestrator. The prefix is deliberately NOT a bare path like /nodes or
 * /jobs — those are also this SPA's own client-side routes, and proxying
 * them directly breaks refresh-safety on those pages (see vite.config.ts).
 * See ADR-011.
 */
export const api = createClient<paths>({ baseUrl: "/api" });

/** Narrow, honest error shape for the UI layer — never a raw stack trace. */
export class ApiError extends Error {
  readonly status?: number;

  constructor(message: string, status?: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/**
 * Plain-language fallback for a failed request that carried no FastAPI
 * `detail` string (e.g. the orchestrator is unreachable and the Vite dev
 * proxy itself answers with a bare 502/504). `status` is kept on `ApiError`
 * for any internal branching later, but it must never be interpolated into
 * user-facing copy — no raw HTTP status codes shown to the user, per the
 * quality floor.
 */
function fallbackMessage(status: number): string {
  if (status === 404) return "That wasn't found. It may have been removed.";
  if (status >= 500 || status === 0) {
    return "Couldn't reach the orchestrator. It may be restarting or temporarily unreachable.";
  }
  return "The orchestrator couldn't complete this request.";
}

/**
 * Unwrap an openapi-fetch result: return `data` or throw a plain-language
 * `ApiError`. Centralizes error normalization so every hook behaves the same
 * way whether the failure is a network error (orchestrator down) or an HTTP
 * error status.
 */
export function unwrap<T>(result: {
  data?: T;
  error?: unknown;
  response: Response;
}): T {
  if (result.error !== undefined) {
    const detail =
      typeof result.error === "object" &&
      result.error !== null &&
      "detail" in result.error &&
      typeof (result.error as { detail?: unknown }).detail === "string"
        ? (result.error as { detail: string }).detail
        : undefined;
    throw new ApiError(
      detail ?? fallbackMessage(result.response.status),
      result.response.status,
    );
  }
  if (result.data === undefined) {
    throw new ApiError("Server returned an empty response");
  }
  return result.data;
}
