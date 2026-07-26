import { useMutation } from "@tanstack/react-query";
import { api, ApiError, unwrap } from "./client";

/**
 * Admin key for minting enrollment tokens (POST /auth/enrollment-tokens).
 *
 * TODO(M8): move token minting behind real user auth — the admin key must not
 * ship in a browser bundle. Every `VITE_`-prefixed env var is inlined into the
 * built JS at build time and is trivially visible to anyone who opens
 * devtools; this is accepted as a known, documented limitation of this dev
 * milestone (see dashboard/.env.example), not something to silently work
 * around client-side.
 */
const ADMIN_API_KEY = import.meta.env.VITE_ADMIN_API_KEY as string | undefined;

export function hasAdminKeyConfigured(): boolean {
  return Boolean(ADMIN_API_KEY);
}

export interface CreateEnrollmentTokenInput {
  created_by: string;
  ttl_seconds?: number;
}

export function useCreateEnrollmentTokenMutation() {
  return useMutation({
    mutationFn: async (body: CreateEnrollmentTokenInput) => {
      if (!ADMIN_API_KEY) {
        throw new ApiError(
          "This dashboard build has no admin key configured (VITE_ADMIN_API_KEY). " +
            "See dashboard/.env.example.",
        );
      }
      return unwrap(
        await api.POST("/auth/enrollment-tokens", {
          body,
          headers: { "X-Admin-Key": ADMIN_API_KEY },
        }),
      );
    },
  });
}
