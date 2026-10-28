/**
 * Session state.
 *
 * The session is a first-party HttpOnly cookie, so this context only tracks *who* the
 * caller is and what the server says they may do. It deliberately holds no token. The
 * permission list drives presentation only; every action is re-authorized by the API.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { api, ApiError, type SignInRequest } from "../api/client";
import type { CurrentUser } from "../api/types";

interface SessionValue {
  user: CurrentUser | null;
  loading: boolean;
  error: ApiError | null;
  signIn: (payload: SignInRequest) => Promise<void>;
  signOut: () => Promise<void>;
  refresh: () => Promise<void>;
  can: (permission: string) => boolean;
}

const SessionContext = createContext<SessionValue | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);

  const load = useCallback(async () => {
    try {
      const current = await api.currentUser();
      setUser(current);
      setError(null);
    } catch (cause) {
      const apiError =
        cause instanceof ApiError
          ? cause
          : new ApiError(0, "unknown_error", "Sign-in state could not be determined.", null, null);
      // A 401 is the normal signed-out state, not a failure to report.
      if (apiError.isUnauthenticated) {
        setUser(null);
        setError(null);
      } else {
        setError(apiError);
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const signIn = useCallback(async (payload: SignInRequest) => {
    const current = await api.signIn(payload);
    setUser(current);
    setError(null);
  }, []);

  const signOut = useCallback(async () => {
    try {
      await api.signOut();
    } finally {
      setUser(null);
    }
  }, []);

  const value = useMemo<SessionValue>(
    () => ({
      user,
      loading,
      error,
      signIn,
      signOut,
      refresh: load,
      can: (permission: string) => user?.permissions.includes(permission) ?? false,
    }),

    [user, loading, error, signIn, signOut, load],
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionValue {
  const value = useContext(SessionContext);
  if (!value) {
    throw new Error("useSession must be used within a SessionProvider");
  }
  return value;
}
