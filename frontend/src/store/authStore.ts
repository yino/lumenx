import { create } from "zustand";

import type { AuthUser } from "@/lib/api";

export type AuthStatus = "checking" | "anonymous" | "authenticated";

interface AuthStore {
  status: AuthStatus;
  user: AuthUser | null;
  error: string | null;
  beginSessionCheck: () => void;
  setAuthenticated: (user: AuthUser) => void;
  setAnonymous: (error?: string | null) => void;
}

export const useAuthStore = create<AuthStore>((set) => ({
  status: "checking",
  user: null,
  error: null,
  beginSessionCheck: () => set({ status: "checking", user: null, error: null }),
  setAuthenticated: (user) => set({ status: "authenticated", user, error: null }),
  setAnonymous: (error = null) => set({ status: "anonymous", user: null, error }),
}));
