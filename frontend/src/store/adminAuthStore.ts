import { create } from "zustand";

import type { AdminAuthUser } from "@/lib/api";

export type AdminAuthStatus = "checking" | "anonymous" | "authenticated";

interface AdminAuthStore {
  status: AdminAuthStatus;
  admin: AdminAuthUser | null;
  error: string | null;
  beginSessionCheck: () => void;
  setAuthenticated: (admin: AdminAuthUser) => void;
  setAnonymous: (error?: string | null) => void;
}

export const useAdminAuthStore = create<AdminAuthStore>((set) => ({
  status: "checking",
  admin: null,
  error: null,
  beginSessionCheck: () => set({ status: "checking", admin: null, error: null }),
  setAuthenticated: (admin) => set({ status: "authenticated", admin, error: null }),
  setAnonymous: (error = null) => set({ status: "anonymous", admin: null, error }),
}));
