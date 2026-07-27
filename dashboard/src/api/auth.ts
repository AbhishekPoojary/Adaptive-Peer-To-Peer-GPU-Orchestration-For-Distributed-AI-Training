import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "./client";
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
