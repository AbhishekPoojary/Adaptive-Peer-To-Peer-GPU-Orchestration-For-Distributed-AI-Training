import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "./client";
import { getToken } from "./session";

/**
 * Admin user management (ADR-012 addendum 2).
 *
 * Plain `fetch` rather than the generated `api` client, because `schema.gen.ts`
 * predates these routes. Run `npm run generate:api` against a running
 * orchestrator and these can move over — the hand-written types below are a
 * bridge, not a new pattern.
 */

const API_BASE = "/api";

export interface ManagedUser {
  id: string;
  username: string;
  role: "ADMIN" | "OPERATOR";
  email: string | null;
  created_at: string;
  last_login_at: string | null;
  disabled_at: string | null;
  has_password: boolean;
  google_linked: boolean;
}

function authHeaders(): Record<string, string> {
  const token = getToken();
  return token
    ? { Authorization: `Bearer ${token}`, "Content-Type": "application/json" }
    : { "Content-Type": "application/json" };
}

async function toApiError(response: Response): Promise<ApiError> {
  const payload: unknown = await response.json().catch(() => null);
  let detail: string | undefined;
  if (typeof payload === "object" && payload !== null && "detail" in payload) {
    const raw = (payload as { detail: unknown }).detail;
    if (typeof raw === "string") {
      detail = raw;
    } else if (Array.isArray(raw)) {
      // FastAPI validation errors arrive as a list of {loc, msg}. Surfacing the
      // message beats showing the user a raw JSON blob.
      const first = raw[0] as { msg?: unknown } | undefined;
      if (first && typeof first.msg === "string") detail = first.msg;
    }
  }
  return new ApiError(
    detail ?? "The orchestrator couldn't complete this request.",
    response.status,
  );
}

export function useUsersQuery(enabled = true) {
  return useQuery({
    queryKey: ["users"],
    enabled,
    queryFn: async (): Promise<ManagedUser[]> => {
      const response = await fetch(`${API_BASE}/users`, { headers: authHeaders() });
      if (!response.ok) throw await toApiError(response);
      return ((await response.json()) as { users: ManagedUser[] }).users;
    },
  });
}

export interface CreateUserInput {
  username: string;
  role: "ADMIN" | "OPERATOR";
  password?: string;
  email?: string;
}

export function useCreateUserMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: CreateUserInput): Promise<ManagedUser> => {
      const response = await fetch(`${API_BASE}/users`, {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify(input),
      });
      if (!response.ok) throw await toApiError(response);
      return (await response.json()) as ManagedUser;
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["users"] }),
  });
}

/**
 * Only the fields present are changed server-side. `email: null` explicitly
 * clears the address (and Google sign-in with it), which is why this is
 * `Partial` with an explicit null rather than an optional string.
 */
export interface UpdateUserInput {
  role?: "ADMIN" | "OPERATOR";
  password?: string;
  email?: string | null;
  disabled?: boolean;
}

export function useUpdateUserMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (args: {
      id: string;
      changes: UpdateUserInput;
    }): Promise<ManagedUser> => {
      const response = await fetch(`${API_BASE}/users/${args.id}`, {
        method: "PATCH",
        headers: authHeaders(),
        body: JSON.stringify(args.changes),
      });
      if (!response.ok) throw await toApiError(response);
      return (await response.json()) as ManagedUser;
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["users"] }),
  });
}
