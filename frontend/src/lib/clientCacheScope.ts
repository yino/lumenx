import { IS_CLOUD_DEPLOYMENT } from "@/lib/deployment";

interface ClientCacheScope {
  userId: string;
  workspaceId: string;
}

let activeScope: ClientCacheScope | null = null;

export function setClientCacheScope(userId: string, workspaceId: string): void {
  activeScope = { userId, workspaceId };
}

export function clearClientCacheScope(): void {
  activeScope = null;
}

export function getClientCacheScope(): ClientCacheScope | null {
  return activeScope;
}

export function clientStorageKey(baseKey: string): string | null {
  if (!IS_CLOUD_DEPLOYMENT) return baseKey;
  if (!activeScope) return null;
  return `lumenx:${encodeURIComponent(activeScope.userId)}:workspace:${encodeURIComponent(activeScope.workspaceId)}:${baseKey}`;
}

export function readClientStorage(key: string): string | null {
  if (typeof window === "undefined") return null;
  const scopedKey = clientStorageKey(key);
  if (!scopedKey) return null;
  try {
    return window.localStorage.getItem(scopedKey);
  } catch {
    return null;
  }
}

export function writeClientStorage(key: string, value: string): void {
  if (typeof window === "undefined") return;
  const scopedKey = clientStorageKey(key);
  if (!scopedKey) return;
  try {
    window.localStorage.setItem(scopedKey, value);
  } catch {
    // Storage can be unavailable in private browsing or after quota exhaustion.
  }
}

export function removeClientStorage(key: string): void {
  if (typeof window === "undefined") return;
  const scopedKey = clientStorageKey(key);
  if (!scopedKey) return;
  try {
    window.localStorage.removeItem(scopedKey);
  } catch {
    // Ignore unavailable browser storage.
  }
}
