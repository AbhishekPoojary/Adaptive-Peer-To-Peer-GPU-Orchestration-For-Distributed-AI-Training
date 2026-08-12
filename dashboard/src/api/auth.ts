import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, unwrap } from "./client";
import { clearSession, setSession, type SessionUser } from "./session";

/**
 * Auth surface for the dashboard (ADR-012).
 *
 * There is deliberately no admin key anywhere in this file. Through M7 this
 * module read `VITE_ADMIN_API_KEY`, which Vite inlines into the built bundle
 * at build time — the key that gates enrolling machines into the fleet was
 * sitting in the JavaScript, readable from devtools by anyone who loaded the
 * page. It is now gone: the browser logs in as a person and holds a
 * short-lived, user-scoped token, and enrollment-token minting is authorized
 * by that user's ADMIN role server-side.
 */

export interface LoginInput {
  username: string;
  password: string;
}

export function useLoginMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: LoginInput) => {
      const result = unwrap(await api.POST("/auth/login", { body }));
      setSession(result.access_token, result.user as SessionUser);
      return result;
    },
    onSuccess: () => {
      // The previous session's cached data was fetched under a different
      // identity; drop it rather than briefly showing it to the new user.
      void queryClient.invalidateQueries();
    },
  });
}

export function useLogout() {
  const queryClient = useQueryClient();
  return () => {
    clearSession();
    queryClient.clear();
  };
}

/**
 * Verify the stored token against the server on app load.
 *
 * A token in sessionStorage only proves someone logged in at some point in
 * this tab's life; it may have expired or the account may have been disabled.
 * Asking the server is the only way to know, and the 401 middleware in
 * client.ts turns a negative answer into a clean sign-out.
 */
export function useCurrentUserQuery(enabled: boolean) {
  return useQuery({
    queryKey: ["auth", "me"],
    queryFn: async () => unwrap(await api.GET("/auth/me", {})),
    enabled,
    retry: false,
    staleTime: 60_000,
  });
}

/**
 * Google sign-in (ADR-012 addendum).
 *
 * `/auth/providers` and `/auth/google` are called with plain `fetch` rather than
 * the generated `api` client, because `schema.gen.ts` predates them. Run
 * `npm run generate:api` against a running orchestrator and these two should be
 * moved onto `api.GET`/`api.POST` like everything else — the hand-written types
 * below are a temporary bridge, not a new pattern. They deliberately reuse
 * `ApiError` so the UI's error handling is identical either way.
 */

const API_BASE = "/api";

async function postJson<T>(path: string, body: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    // Network-level failure: the orchestrator or the dev proxy is unreachable.
    throw new ApiError(
      "Couldn't reach the orchestrator. It may be restarting or temporarily unreachable.",
      0,
    );
  }

  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail =
      typeof payload === "object" &&
      payload !== null &&
      "detail" in payload &&
      typeof (payload as { detail?: unknown }).detail === "string"
        ? (payload as { detail: string }).detail
        : undefined;
    throw new ApiError(
      detail ?? "The orchestrator couldn't complete this request.",
      response.status,
    );
  }
  return payload as T;
}

export interface AuthProviders {
  password: boolean;
  google: { enabled: boolean; client_id: string | null };
}

/**
 * Which sign-in mechanisms this deployment offers.
 *
 * Asked at runtime rather than baked in at build time so an operator can enable
 * Google sign-in by setting one environment variable, without rebuilding the
 * bundle. On failure the caller falls back to password-only — the offline path
 * must never be gated on an answer we couldn't get.
 */
export function useAuthProvidersQuery() {
  return useQuery({
    queryKey: ["auth", "providers"],
    queryFn: async (): Promise<AuthProviders> => {
      const response = await fetch(`${API_BASE}/auth/providers`);
      if (!response.ok) throw new ApiError("Couldn't load sign-in options");
      return (await response.json()) as AuthProviders;
    },
    retry: false,
    staleTime: 5 * 60_000,
  });
}

interface LoginResult {
  access_token: string;
  user: SessionUser;
}

export function useGoogleLoginMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    // `credential` is the ID token Google Identity Services hands the browser.
    // It is forwarded verbatim; the orchestrator verifies it against Google's
    // published keys, so nothing here has to be trusted.
    mutationFn: async (credential: string) => {
      const result = await postJson<LoginResult>("/auth/google", { credential });
      setSession(result.access_token, result.user);
      return result;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries();
    },
  });
}

export interface CreateEnrollmentTokenInput {
  created_by: string;
  ttl_seconds?: number;
}

export function useCreateEnrollmentTokenMutation() {
  return useMutation({
    mutationFn: async (body: CreateEnrollmentTokenInput) =>
      unwrap(await api.POST("/auth/enrollment-tokens", { body })),
  });
}
