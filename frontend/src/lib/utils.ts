import { API_URL, getCachedMediaUrl, parseMediaReference } from "./api";
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
    return twMerge(clsx(inputs));
}

function normalizeBackendFilesPath(path: string): string {
    if (path.startsWith("/files/output/")) {
        return `/files/${path.slice("/files/output/".length)}`;
    }
    return path;
}

function normalizeAbsoluteBackendFilesUrl(path: string): string {
    if (!/^https?:/i.test(path)) return path;

    try {
        const mediaUrl = new URL(path);
        const backendUrl = new URL(API_URL);
        if (mediaUrl.origin !== backendUrl.origin || !mediaUrl.pathname.startsWith("/files/")) {
            return path;
        }

        mediaUrl.pathname = normalizeBackendFilesPath(mediaUrl.pathname);
        return mediaUrl.toString();
    } catch {
        return path;
    }
}

function getDevMediaProxyUrl(path: string): string | null {
    if (process.env.NODE_ENV !== "development") return null;
    if (path.startsWith("/api-proxy/files/output/")) {
        return `/api-proxy/files/${path.slice("/api-proxy/files/output/".length)}`;
    }
    if (path.startsWith("/api-proxy/files/")) return path;
    if (path.startsWith("/files/")) return `/api-proxy${normalizeBackendFilesPath(path)}`;
    if (!/^https?:/i.test(path)) return null;

    try {
        const mediaUrl = new URL(path);
        const backendUrl = new URL(API_URL);
        if (mediaUrl.origin !== backendUrl.origin || !mediaUrl.pathname.startsWith("/files/")) {
            return null;
        }
        return `/api-proxy${normalizeBackendFilesPath(mediaUrl.pathname)}${mediaUrl.search}${mediaUrl.hash}`;
    } catch {
        return null;
    }
}

export function getAssetUrl(path: string | null | undefined): string {
    if (!path) return "";
    const resolvedPath = parseMediaReference(path) ? getCachedMediaUrl(path) : path;
    if (!resolvedPath) return "";

    const proxiedUrl = getDevMediaProxyUrl(resolvedPath);
    if (proxiedUrl) return proxiedUrl;
    if (resolvedPath.startsWith("/api-proxy/files/output/")) {
        return `/api-proxy/files/${resolvedPath.slice("/api-proxy/files/output/".length)}`;
    }
    if (resolvedPath.startsWith("/api-proxy/files/")) return resolvedPath;
    if (/^https?:/i.test(resolvedPath)) return normalizeAbsoluteBackendFilesUrl(resolvedPath);
    if (/^(blob:|data:)/i.test(resolvedPath)) return resolvedPath;
    if (resolvedPath.startsWith("/files/")) {
        return `${API_URL}${normalizeBackendFilesPath(resolvedPath)}`;
    }

    // Stored local outputs include the filesystem-only `output/` prefix, while
    // the backend `/files` mount already points at that directory.
    const withoutLeadingSlash = resolvedPath.startsWith("/") ? resolvedPath.slice(1) : resolvedPath;
    const cleanPath = withoutLeadingSlash.startsWith("output/")
        ? withoutLeadingSlash.slice("output/".length)
        : withoutLeadingSlash.startsWith("files/")
            ? withoutLeadingSlash.slice("files/".length)
            : withoutLeadingSlash;
    const filesPath = `/files/${cleanPath}`;
    return getDevMediaProxyUrl(filesPath) ?? `${API_URL}${filesPath}`;
}

export function getAssetUrlWithTimestamp(path: string | null | undefined, timestamp?: number): string {
    const baseUrl = getAssetUrl(path);
    if (!baseUrl) return "";

    // If URL already has query params, append with & otherwise with ?
    const separator = baseUrl.includes('?') ? '&' : '?';
    return baseUrl + separator + `t=${timestamp || 0}`;
}

export function extractErrorDetail(error: any, fallback = "未知错误"): string {
    return error?.response?.data?.detail
        || error?.response?.data?.message
        || error?.message
        || fallback;
}
