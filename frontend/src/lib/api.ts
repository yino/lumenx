import axiosFactory from "axios";
import type { AxiosError, InternalAxiosRequestConfig } from "axios";
import { DEFAULT_I2V_MODEL_ID, VIDEO_I2V_MODELS } from "@/lib/modelCatalog";
import { IS_CLOUD_DEPLOYMENT, withoutCloudModelOverrides } from "@/lib/deployment";

// Dynamic API URL detection (no port enumeration):
// 1. Explicit override: NEXT_PUBLIC_API_URL (any env / proxy setup).
// 2. Dev mode (`next dev`, NODE_ENV==='development'): backend runs on a separate
//    port, so target the same host on the backend port — works for ANY dev port.
// 3. Production / packaged (Electron): frontend is served by the backend, so use
//    the same origin.
const BACKEND_PORT = process.env.NEXT_PUBLIC_BACKEND_PORT || "17177";

const getApiUrl = (): string => {
    if (typeof window !== 'undefined') {
        const { protocol, hostname, port } = window.location;

        // Hosted builds keep browser credentials strictly same-origin.
        if (IS_CLOUD_DEPLOYMENT && process.env.NODE_ENV !== 'development') {
            return `${window.location.origin}/api/v1`;
        }

        const override = process.env.NEXT_PUBLIC_API_URL;
        if (override && override.trim()) {
            return override.trim().replace(/\/+$/, "");
        }

        // Dev server: backend lives on a different port regardless of which
        // dev port Next.js picked (3008/3009/3018/...).
        if (process.env.NODE_ENV === 'development') {
            return `${protocol}//${hostname}:${BACKEND_PORT}`;
        }

        // Production / packaged: frontend is served by the backend → same origin.
        return `${protocol}//${hostname}${port ? ':' + port : ''}`;
    }

    // Cloud routes are client-authenticated and same-origin. An empty SSR base
    // avoids embedding a localhost authority in exported cloud assets.
    return IS_CLOUD_DEPLOYMENT ? "" : `http://localhost:${BACKEND_PORT}`;
};

export const API_URL = getApiUrl();

const getCookieValue = (name: string): string | null => {
    if (typeof document === "undefined") return null;
    const prefix = `${encodeURIComponent(name)}=`;
    const match = document.cookie
        .split(";")
        .map((item) => item.trim())
        .find((item) => item.startsWith(prefix));
    return match ? decodeURIComponent(match.slice(prefix.length)) : null;
};

export const SESSION_EXPIRED_EVENT = "lumenx:session-expired";
export const ADMIN_SESSION_EXPIRED_EVENT = "lumenx:admin-session-expired";

let activeWorkspaceId: string | null = null;
const MEDIA_REFERENCE_PREFIX = "media:";
const MEDIA_ID_PATTERN = /^\d+$/;

interface CachedMediaAccess {
    url: string;
    expiresAt: number;
}

const mediaAccessCache = new Map<string, CachedMediaAccess>();
const mediaAccessRequests = new Map<string, Promise<string>>();
const resourceVersions = new Map<string, number>();

type VersionedResourceKind = "project" | "series" | "asset";

function resourceVersionKey(kind: VersionedResourceKind, id: string): string {
    return `${activeWorkspaceId || "no-workspace"}:${kind}:${id}`;
}

function rememberResourceVersion(
    kind: VersionedResourceKind,
    id: unknown,
    version: unknown,
): void {
    if (typeof id !== "string" || !id) return;
    const parsedVersion = typeof version === "number" ? version : Number(version);
    if (!Number.isInteger(parsedVersion) || parsedVersion < 1) return;
    resourceVersions.set(resourceVersionKey(kind, id), parsedVersion);
}

export function toMediaReference(mediaId: string): string {
    return `${MEDIA_REFERENCE_PREFIX}${mediaId}`;
}

export function parseMediaReference(reference: string | null | undefined): string | null {
    if (!reference?.startsWith(MEDIA_REFERENCE_PREFIX)) return null;
    const mediaId = reference.slice(MEDIA_REFERENCE_PREFIX.length);
    return MEDIA_ID_PATTERN.test(mediaId) ? mediaId : null;
}

function mediaCacheKey(mediaId: string): string {
    return `${activeWorkspaceId || "no-workspace"}:${mediaId}`;
}

export function getCachedMediaUrl(reference: string | null | undefined): string | null {
    const mediaId = parseMediaReference(reference);
    if (!mediaId) return null;
    const cached = mediaAccessCache.get(mediaCacheKey(mediaId));
    if (!cached || cached.expiresAt <= Date.now() + 5_000) return null;
    return cached.url;
}

export function setActiveWorkspaceId(workspaceId: string | null): void {
    if (activeWorkspaceId !== workspaceId) {
        mediaAccessCache.clear();
        mediaAccessRequests.clear();
        resourceVersions.clear();
    }
    activeWorkspaceId = workspaceId;
}

export function getActiveWorkspaceId(): string | null {
    return activeWorkspaceId;
}

export function createIdempotencyKey(scope = "ai"): string {
    const randomPart =
        typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
            ? crypto.randomUUID()
            : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
    return `${scope}:${randomPart}`;
}

export interface SafeAPIError {
    code?: string;
    message: string;
    status?: number;
    correlationId?: string;
}

export interface AgentRunResponse {
    run_id: string;
    project_id: string;
    workspace_id: string;
    target_profile: string;
    generation_mode: "t2v" | "i2v" | "r2v";
    status: string;
    current_stage: string;
    stage_status: string;
    task_status: string;
    validation_report: {
        status: string;
        findings: Array<{ severity: string; code: string; message: string; shot_id?: string | null }>;
    };
    approval: { decision: string; actor_id?: string | null; reason?: string | null };
    submitted_task_ids: string[];
    [key: string]: unknown;
}

export interface AgentProfileResponse {
    profile_id: string;
    model_id: string;
    provider: string;
    display_name: string;
    modes: string[];
    duration: { min: number; max: number };
    resolutions: string[];
    default_resolution?: string;
    max_reference_images: number;
    supports_audio: boolean;
}

const API_ERROR_MESSAGES: Record<string, string> = {
    ACCESS_DENIED: "当前操作没有权限",
    ACCOUNT_SUSPENDED: "账号已停用，请联系平台管理员",
    ADMIN_ACCOUNT_SUSPENDED: "管理员账号已停用",
    ADMIN_AUTH_REQUIRED: "管理员登录状态已失效，请重新登录",
    ADMIN_CSRF_INVALID: "管理员安全校验失败，请刷新后重试",
    ADMIN_SESSION_EXPIRED: "管理员登录已过期，请重新登录",
    ADMIN_REQUIRED: "当前账号没有此操作权限",
    AI_NEW_TASKS_DISABLED: "AI 新任务已暂停，已有任务仍可查询和处理",
    AI_TASK_NOT_FOUND: "AI 任务不存在或无权访问",
    AI_CONFIGURATION_UNAVAILABLE: "AI 服务配置暂不可用，请稍后重试",
    AI_CONTEXT_INVALID: "请选择有效的工作区",
    AI_REQUEST_INVALID: "AI 请求参数无效，请检查后重试",
    AI_RESERVATION_CONFLICT: "算力券预扣状态已变化，请刷新后重试",
    AI_RESOURCE_NOT_FOUND: "AI 请求资源不存在或无权访问",
    AI_TASK_CONFLICT: "AI 任务状态已变化，请刷新后重试",
    AUTH_REQUIRED: "登录状态已失效，请重新登录",
    CSRF_INVALID: "安全校验失败，请刷新页面后重试",
    CONFIGURATION_CONFLICT: "配置状态已变化，请刷新后重试",
    CONFIGURATION_INVALID: "配置内容不符合要求，请检查后重试",
    CONFIGURATION_NOT_FOUND: "配置版本不存在",
    CONTENT_INVALID: "内容不符合要求，请检查后重试",
    CONTENT_NOT_FOUND: "内容不存在或无权访问",
    CONTENT_VERSION_CONFLICT: "内容已被更新，请刷新后重试",
    CONTENT_VERSION_REQUIRED: "内容版本信息缺失，请刷新后重试",
    IDEMPOTENCY_CONFLICT: "请求标识已被其他操作使用，请刷新后重试",
    INSUFFICIENT_TICKET_BALANCE: "算力券余额不足，请联系平台管理员补充",
    TICKET_BALANCE_INSUFFICIENT: "算力券余额不足，请联系平台管理员补充",
    INVALID_CREDENTIALS: "手机号或密码不正确",
    MEDIA_NOT_FOUND: "媒体不存在或无权访问",
    MEDIA_INVALID: "媒体文件不符合要求，请检查后重试",
    MEDIA_STATE_CONFLICT: "媒体状态已变化，请刷新后重试",
    MEDIA_STORAGE_UNAVAILABLE: "媒体存储暂不可用，请稍后重试",
    PHONE_ALREADY_REGISTERED: "该手机号已注册，请直接登录",
    RATE_LIMITED: "操作过于频繁，请稍后再试",
    REGISTRATION_DISABLED: "注册暂未开放，请稍后再试",
    INVITATION_INVALID: "邀请码无效或已失效",
    RUNTIME_POLICY_UNAVAILABLE: "平台运行策略暂不可用，请稍后再试",
    REQUEST_CONFLICT: "内容已发生变化，请刷新后重试",
    REQUEST_INVALID: "请求参数无效，请检查后重试",
    RESET_CREDENTIAL_INVALID: "重置凭据无效或已过期",
    RESOURCE_NOT_FOUND: "内容不存在或无权访问",
    SESSION_EXPIRED: "登录状态已失效，请重新登录",
    SESSION_REVOKED: "当前登录已失效，请重新登录",
    VALIDATION_ERROR: "提交的信息不符合要求，请检查后重试",
    INTERNAL_ERROR: "服务暂时不可用，请稍后重试",
    SERVICE_UNAVAILABLE: "服务暂时不可用，请稍后重试",
    TICKET_ADJUSTMENT_CONFLICT: "算力券调整无法完成，请刷新后重试",
    TICKET_HISTORY_INVALID: "算力券记录查询参数无效",
    TICKET_WALLET_NOT_FOUND: "算力券账户不存在",
    USER_NOT_FOUND: "用户不存在",
    WORKSPACE_CONTEXT_INVALID: "请选择有效的工作区",
    WORKSPACE_CONFLICT: "工作区已发生变化，请刷新后重试",
    WORKSPACE_NOT_FOUND: "工作区不存在或无权访问",
    IMPORT_VALIDATION_FAILED: "本地数据预检未通过，请检查导入源",
    IMPORT_CONFLICT: "导入批次或目标数据已发生变化，请重新预检",
};

const HAS_CHINESE = /[\u3400-\u9fff]/;
const MUTATION_METHODS = new Set(["post", "put", "patch", "delete"]);
const PLAINTEXT_SECRET_FIELDS = new Set([
    "access_key",
    "access_key_id",
    "access_key_secret",
    "access_token",
    "api_key",
    "api_secret",
    "authorization",
    "bearer_token",
    "credential",
    "credentials",
    "password_secret",
    "secret",
    "secret_key",
]);

function canonicalFieldName(name: string): string {
    return name
        .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
        .replace(/[^a-zA-Z0-9]+/g, "_")
        .toLowerCase();
}

function isPlaintextSecretField(name: string): boolean {
    const canonical = canonicalFieldName(name);
    return (
        PLAINTEXT_SECRET_FIELDS.has(canonical) ||
        canonical.endsWith("_api_key") ||
        canonical.endsWith("_secret_key") ||
        canonical.endsWith("_access_token")
    );
}

function stripPlaintextSecrets(value: unknown): unknown {
    if (!IS_CLOUD_DEPLOYMENT) return value;
    if (Array.isArray(value)) return value.map(stripPlaintextSecrets);
    if (!value || typeof value !== "object") return value;
    if (typeof FormData !== "undefined" && value instanceof FormData) return value;
    if (Object.getPrototypeOf(value) !== Object.prototype) return value;
    return Object.fromEntries(
        Object.entries(value as Record<string, unknown>)
            .filter(([key]) => !isPlaintextSecretField(key))
            .map(([key, item]) => [key, stripPlaintextSecrets(item)]),
    );
}

const CLOUD_AI_ROUTE_PATTERNS = [
    /^\/playground\/generate$/,
    /^\/series\/[^/]+\/assets\/generate$/,
    /^\/projects\/[^/]+\/(?:extract_preview|generate_assets|generate_storyboard|generate_video|generate_audio)$/,
    /^\/projects\/[^/]+\/(?:video_tasks|dialogue_audio\/batch)$/,
    /^\/projects\/[^/]+\/(?:storyboard\/(?:analyze|refine_prompt|refine_batch|render))$/,
    /^\/projects\/[^/]+\/frames\/[^/]+\/(?:refine|audio|dub\/(?:preview|apply))$/,
    /^\/projects\/[^/]+\/mix\/(?:generate_sfx|generate_bgm)$/,
    /^\/projects\/[^/]+\/assets\/(?:generate|generate_motion_ref)$/,
    /^\/projects\/[^/]+\/assets\/[^/]+\/[^/]+\/generate_video$/,
    /^\/projects\/[^/]+\/(?:previous_episode\/summary|art_direction\/analyze|reparse)$/,
    /^\/video\/(?:polish_prompt|polish_r2v_prompt)$/,
    /^\/canvas\/compose_prompt$/,
    /^\/voice\/(?:preview|clone|design\/preview|design\/translate)$/,
];

function requestPathname(url: string | undefined): string {
    if (!url) return "";
    try {
        const pathname = new URL(
            url,
            typeof window !== "undefined" ? window.location.origin : "http://localhost",
        ).pathname;
        if (IS_CLOUD_DEPLOYMENT && pathname.startsWith("/api/v1/")) {
            return pathname.slice("/api/v1".length);
        }
        return pathname;
    } catch {
        return url.split("?", 1)[0];
    }
}

function isCloudAIRequest(url: string | undefined): boolean {
    if (!IS_CLOUD_DEPLOYMENT) return false;
    const pathname = requestPathname(url);
    return (
        (pathname === "/projects" || pathname === "/projects/") ||
        CLOUD_AI_ROUTE_PATTERNS.some((pattern) => pattern.test(pathname))
    );
}

function rememberAssetVersions(value: unknown): void {
    if (Array.isArray(value)) {
        value.forEach(rememberAssetVersions);
        return;
    }
    if (!value || typeof value !== "object") return;
    const payload = value as Record<string, unknown>;
    if (typeof payload.asset_record_id === "string") {
        rememberResourceVersion("asset", payload.id, payload.version);
    }
    Object.values(payload).forEach(rememberAssetVersions);
}

function rememberResponseVersions(url: string | undefined, value: unknown): void {
    rememberAssetVersions(value);
    const pathname = requestPathname(url);
    const rememberDocuments = (
        kind: "project" | "series",
        documents: unknown,
    ): void => {
        if (Array.isArray(documents)) {
            documents.forEach((document) => {
                if (document && typeof document === "object") {
                    const item = document as Record<string, unknown>;
                    rememberResourceVersion(kind, item.id, item.version);
                }
            });
        }
    };

    if (/^\/projects\/?$/.test(pathname)) {
        rememberDocuments("project", value);
        return;
    }
    if (/^\/series\/?$/.test(pathname)) {
        rememberDocuments("series", value);
        return;
    }
    if (!value || typeof value !== "object" || Array.isArray(value)) return;
    const payload = value as Record<string, unknown>;
    if (typeof payload.asset_record_id === "string") return;

    const projectMatch = pathname.match(/^\/projects\/([^/]+)/);
    if (projectMatch) {
        rememberResourceVersion("project", decodeURIComponent(projectMatch[1]), payload.version);
        return;
    }
    const seriesMatch = pathname.match(/^\/series\/([^/]+)/);
    if (seriesMatch) {
        rememberResourceVersion("series", decodeURIComponent(seriesMatch[1]), payload.version);
    }
}

function requestAssetId(pathname: string, data: unknown): string | null {
    const payload = data && typeof data === "object" && !Array.isArray(data)
        ? data as Record<string, unknown>
        : null;
    if (pathname.includes("/assets/") && typeof payload?.asset_id === "string") {
        return payload.asset_id;
    }
    const scopedAsset = pathname.match(
        /^\/(?:projects|series)\/[^/]+\/(?:characters|scenes|props)\/([^/]+)(?:\/(?:voice|voice_params))?$/,
    );
    if (scopedAsset) return decodeURIComponent(scopedAsset[1]);
    const uploadedAsset = pathname.match(
        /^\/projects\/[^/]+\/assets\/[^/]+\/([^/]+)\/upload$/,
    );
    if (uploadedAsset) return decodeURIComponent(uploadedAsset[1]);
    const seriesAsset = pathname.match(
        /^\/series\/[^/]+\/assets\/[^/]+\/([^/]+)$/,
    );
    if (seriesAsset) return decodeURIComponent(seriesAsset[1]);
    const libraryAsset = pathname.match(/^\/library\/assets\/[^/]+\/([^/]+)$/);
    if (libraryAsset) return decodeURIComponent(libraryAsset[1]);
    const customVoice = pathname.match(/^\/series\/[^/]+\/custom_voices\/([^/]+)$/);
    return customVoice ? decodeURIComponent(customVoice[1]) : null;
}

function requestVersionKey(url: string | undefined, data: unknown): string | null {
    const pathname = requestPathname(url);
    const assetId = requestAssetId(pathname, data);
    if (assetId) return resourceVersionKey("asset", assetId);
    const projectMatch = pathname.match(/^\/projects\/([^/]+)/);
    if (projectMatch) {
        return resourceVersionKey("project", decodeURIComponent(projectMatch[1]));
    }
    const seriesMatch = pathname.match(/^\/series\/([^/]+)/);
    if (seriesMatch) {
        return resourceVersionKey("series", decodeURIComponent(seriesMatch[1]));
    }
    return null;
}

function convertCloudMediaReferences(value: unknown): unknown {
    const mediaIds = new Set<string>();
    const convert = (item: unknown): unknown => {
        if (typeof item === "string") {
            const mediaId = parseMediaReference(item);
            if (mediaId) {
                mediaIds.add(mediaId);
                return undefined;
            }
            return item;
        }
        if (Array.isArray(item)) {
            const converted = item
                .map(convert)
                .filter((entry) => entry !== undefined);
            return converted.length > 0 ? converted : undefined;
        }
        if (!item || typeof item !== "object") return item;
        if (typeof FormData !== "undefined" && item instanceof FormData) return item;
        if (Object.getPrototypeOf(item) !== Object.prototype) return item;
        return Object.fromEntries(
            Object.entries(item as Record<string, unknown>)
                .map(([key, entry]) => [key, convert(entry)] as const)
                .filter(([, entry]) => entry !== undefined),
        );
    };

    const converted = convert(value);
    if (!converted || typeof converted !== "object" || Array.isArray(converted)) {
        return converted;
    }
    const payload = converted as Record<string, unknown>;
    const existing = Array.isArray(payload.media_ids)
        ? payload.media_ids.map(String)
        : [];
    if (mediaIds.size > 0) {
        payload.media_ids = Array.from(new Set([...existing, ...Array.from(mediaIds)]));
    }
    return payload;
}

function sanitizeRequestData(url: string | undefined, value: unknown): unknown {
    const withoutSecrets = stripPlaintextSecrets(value);
    if (!IS_CLOUD_DEPLOYMENT || url?.includes("/admin/configuration")) {
        return withoutSecrets;
    }
    const withoutOverrides = withoutCloudModelOverrides(withoutSecrets);
    return isCloudAIRequest(url)
        ? convertCloudMediaReferences(withoutOverrides)
        : withoutOverrides;
}

function serverErrorPayload(value: unknown): {
    code?: string;
    message?: string;
    correlationId?: string;
} {
    if (!value || typeof value !== "object") return {};
    const payload = value as Record<string, unknown>;
    const nestedDetail = payload.detail && typeof payload.detail === "object"
        ? payload.detail as Record<string, unknown>
        : undefined;
    const detail = typeof payload.detail === "string" ? payload.detail : undefined;
    return {
        code:
            typeof payload.code === "string"
                ? payload.code
                : typeof nestedDetail?.code === "string"
                  ? nestedDetail.code
                  : undefined,
        message:
            typeof payload.message === "string"
                ? payload.message
                : typeof nestedDetail?.message === "string"
                  ? nestedDetail.message
                  : detail,
        correlationId:
            typeof payload.correlation_id === "string"
                ? payload.correlation_id
                : typeof nestedDetail?.correlation_id === "string"
                  ? nestedDetail.correlation_id
                : undefined,
    };
}

function safeErrorFromResponse(
    status: number | undefined,
    data: unknown,
    headerCorrelationId?: string,
): SafeAPIError {
    const payload = serverErrorPayload(data);
    const mapped = payload.code ? API_ERROR_MESSAGES[payload.code] : undefined;
    const safeServerMessage =
        payload.message && HAS_CHINESE.test(payload.message) ? payload.message : undefined;
    const statusMessage =
        status === 401
            ? "登录状态已失效，请重新登录"
            : status === 403
              ? "当前操作没有权限"
              : status === 404
                ? "请求的内容不存在"
                : status === 409
                  ? "内容已发生变化，请刷新后重试"
                  : status === 422
                    ? "提交的信息不符合要求，请检查后重试"
                    : status && status >= 500
                      ? "服务暂时不可用，请稍后重试"
                      : "暂时无法完成操作，请稍后重试";
    return {
        code: payload.code,
        message: mapped || safeServerMessage || statusMessage,
        status,
        correlationId: payload.correlationId || headerCorrelationId,
    };
}

function emitSessionExpired(error: SafeAPIError): void {
    if (typeof window === "undefined") return;
    window.dispatchEvent(new CustomEvent(SESSION_EXPIRED_EVENT, { detail: error }));
}

function emitAdminSessionExpired(error: SafeAPIError): void {
    if (typeof window === "undefined") return;
    window.dispatchEvent(new CustomEvent(ADMIN_SESSION_EXPIRED_EVENT, { detail: error }));
}

function isAdminApiRequest(value: unknown): boolean {
    return typeof value === "string" && /\/admin(?:\/|$)/.test(value);
}

export function getSafeApiError(error: unknown): SafeAPIError {
    if (axiosFactory.isAxiosError(error)) {
        const correlationId = error.response?.headers?.["x-correlation-id"];
        return safeErrorFromResponse(
            error.response?.status,
            error.response?.data,
            typeof correlationId === "string" ? correlationId : undefined,
        );
    }
    if (error instanceof APIRequestError) {
        return {
            code: error.code,
            message: error.message,
            status: error.status,
            correlationId: error.correlationId,
        };
    }
    return { message: "网络连接异常，请检查网络后重试" };
}

export const apiClient = axiosFactory.create({
    baseURL: API_URL || undefined,
    timeout: 60_000,
    withCredentials: true,
});

apiClient.interceptors.request.use((config: InternalAxiosRequestConfig) => {
    config.withCredentials = true;
    const method = (config.method || "get").toLowerCase();
    const adminRequest = isAdminApiRequest(config.url);
    const csrfToken = getCookieValue(
        adminRequest ? "lumenx_admin_csrf" : "lumenx_csrf",
    );
    if (MUTATION_METHODS.has(method) && csrfToken) {
        config.headers.set("X-CSRF-Token", csrfToken);
    }
    if (IS_CLOUD_DEPLOYMENT) {
        config.headers.delete("Authorization");
        config.headers.delete("X-API-Key");
        if (!adminRequest && activeWorkspaceId) {
            config.headers.set("X-Workspace-ID", activeWorkspaceId);
        } else {
            config.headers.delete("X-Workspace-ID");
        }
        config.data = sanitizeRequestData(config.url, config.data);
        if (
            MUTATION_METHODS.has(method) &&
            !isCloudAIRequest(config.url) &&
            !config.headers.get("If-Match")
        ) {
            const versionKey = requestVersionKey(config.url, config.data);
            const version = versionKey ? resourceVersions.get(versionKey) : undefined;
            if (version) config.headers.set("If-Match", String(version));
        }
        if (isCloudAIRequest(config.url) && !config.headers.get("Idempotency-Key")) {
            const payloadKey =
                config.data && typeof config.data === "object"
                    ? (config.data as Record<string, unknown>).idempotency_key
                    : undefined;
            config.headers.set(
                "Idempotency-Key",
                typeof payloadKey === "string" && payloadKey
                    ? payloadKey
                    : createIdempotencyKey("ai"),
            );
        }
    }
    return config;
});

apiClient.interceptors.response.use(
    async (response) => {
        if (IS_CLOUD_DEPLOYMENT) {
            rememberResponseVersions(response.config.url, response.data);
            await prefetchMediaReferences(response.data);
        }
        return response;
    },
    (error: AxiosError) => {
        const safeError = getSafeApiError(error);
        error.message = safeError.message;
        if (error.response?.data && typeof error.response.data === "object") {
            Object.assign(error.response.data, {
                code: safeError.code,
                message: safeError.message,
                detail: safeError.message,
                correlation_id: safeError.correlationId,
            });
        }
        if (safeError.status === 401 || safeError.code === "ACCOUNT_SUSPENDED") {
            if (isAdminApiRequest(error.config?.url)) emitAdminSessionExpired(safeError);
            else emitSessionExpired(safeError);
        }
        return Promise.reject(error);
    },
);

export class APIRequestError extends Error {
    constructor(
        message: string,
        readonly status: number,
        readonly code?: string,
        readonly correlationId?: string,
    ) {
        super(message);
        this.name = "APIRequestError";
    }
}

export async function apiFetch(
    input: RequestInfo | URL,
    init: RequestInit = {},
): Promise<Response> {
    const method = (init.method || "GET").toLowerCase();
    const headers = new Headers(init.headers);
    const adminRequest = isAdminApiRequest(String(input));
    const csrfToken = getCookieValue(
        adminRequest ? "lumenx_admin_csrf" : "lumenx_csrf",
    );
    if (MUTATION_METHODS.has(method) && csrfToken) {
        headers.set("X-CSRF-Token", csrfToken);
    }
    if (IS_CLOUD_DEPLOYMENT) {
        headers.delete("Authorization");
        headers.delete("X-API-Key");
        if (!adminRequest && activeWorkspaceId) headers.set("X-Workspace-ID", activeWorkspaceId);
        else headers.delete("X-Workspace-ID");
    }

    let body = init.body;
    let versionPayload: unknown;
    if (
        IS_CLOUD_DEPLOYMENT &&
        typeof body === "string" &&
        headers.get("Content-Type")?.includes("application/json")
    ) {
        try {
            const parsed = JSON.parse(body);
            const sanitized = sanitizeRequestData(String(input), parsed);
            versionPayload = sanitized;
            body = JSON.stringify(sanitized);
            if (isCloudAIRequest(String(input)) && !headers.has("Idempotency-Key")) {
                const payloadKey =
                    sanitized && typeof sanitized === "object"
                        ? (sanitized as Record<string, unknown>).idempotency_key
                        : undefined;
                headers.set(
                    "Idempotency-Key",
                    typeof payloadKey === "string" && payloadKey
                        ? payloadKey
                        : createIdempotencyKey("ai"),
                );
            }
        } catch {
            // Keep malformed JSON unchanged so the server returns canonical validation.
        }
    }
    if (
        IS_CLOUD_DEPLOYMENT &&
        MUTATION_METHODS.has(method) &&
        !isCloudAIRequest(String(input)) &&
        !headers.has("If-Match")
    ) {
        const versionKey = requestVersionKey(String(input), versionPayload);
        const version = versionKey ? resourceVersions.get(versionKey) : undefined;
        if (version) headers.set("If-Match", String(version));
    }
    if (IS_CLOUD_DEPLOYMENT && isCloudAIRequest(String(input)) && !headers.has("Idempotency-Key")) {
        headers.set("Idempotency-Key", createIdempotencyKey("ai"));
    }

    const response = await globalThis.fetch(input, {
        ...init,
        body,
        credentials: "include",
        headers,
    });
    if (response.ok) {
        if (
            IS_CLOUD_DEPLOYMENT &&
            response.headers.get("content-type")?.includes("application/json")
        ) {
            try {
                const responseData = await response.clone().json();
                rememberResponseVersions(String(input), responseData);
                await prefetchMediaReferences(responseData);
            } catch {
                // Media prefetch is best-effort and must not mask a valid response.
            }
        }
        return response;
    }

    let data: unknown;
    try {
        data = await response.clone().json();
    } catch {
        data = undefined;
    }
    const safeError = safeErrorFromResponse(
        response.status,
        data,
        response.headers.get("x-correlation-id") || undefined,
    );
    if (safeError.status === 401 || safeError.code === "ACCOUNT_SUSPENDED") {
        if (adminRequest) emitAdminSessionExpired(safeError);
        else emitSessionExpired(safeError);
    }
    throw new APIRequestError(
        safeError.message,
        safeError.status || response.status,
        safeError.code,
        safeError.correlationId,
    );
}

const authenticatedMutationConfig = () => {
    const csrfToken = getCookieValue("lumenx_csrf");
    return {
        withCredentials: true,
        headers: csrfToken ? { "X-CSRF-Token": csrfToken } : undefined,
    };
};

export interface AuthUser {
    id: string;
    phone?: string | null;
    username?: string | null;
    account_label: string;
    phone_verified: boolean;
    phone_verification_status: string;
    default_workspace_id: string;
}

export interface AdminAuthUser {
    id: string;
    username: string;
    must_change_password: boolean;
}

export interface AdminAuthResponse {
    admin: AdminAuthUser;
}

export interface AuthResponse {
    user: AuthUser;
}

export interface AuthSession {
    id: string;
    current: boolean;
    created_at: string;
    last_seen_at: string;
    idle_expires_at: string;
    absolute_expires_at: string;
    revoked_at?: string | null;
    user_agent?: string | null;
}

export interface AuthAPIError {
    code?: string;
    message: string;
}

export type RegistrationMode = "disabled" | "invite_only" | "open" | "verified_open";

export interface RegistrationPolicy {
    mode: RegistrationMode;
    verification_available: boolean;
}

export interface UserWorkspace {
    id: string;
    name: string;
    version: number;
    deleted: boolean;
    retention_expires_at: string | null;
}

export const authApi = {
    registrationPolicy: () =>
        apiClient
            .get<RegistrationPolicy>(`${API_URL}/auth/registration-policy`, {
                timeout: 10_000,
            })
            .then((response) => response.data),
    currentUser: () =>
        apiClient
            .get<AuthResponse>(`${API_URL}/auth/me`, {
                withCredentials: true,
                timeout: 10_000,
            })
            .then((response) => response.data),
    login: (identifier: string, password: string) =>
        apiClient
            .post<AuthResponse>(
                `${API_URL}/auth/login`,
                { identifier, password },
                { withCredentials: true, timeout: 15_000 },
            )
            .then((response) => response.data),
    register: (phone: string, password: string, invitationCode?: string) =>
        apiClient
            .post<AuthResponse>(
                `${API_URL}/auth/register`,
                { phone, password, invitation_code: invitationCode || undefined },
                { withCredentials: true, timeout: 15_000 },
            )
            .then((response) => response.data),
    logout: () =>
        apiClient
            .post<{ message: string }>(`${API_URL}/auth/logout`)
            .then((response) => response.data),
    changePassword: (currentPassword: string, newPassword: string) =>
        apiClient
            .post<{ message: string }>(`${API_URL}/auth/password`, {
                current_password: currentPassword,
                new_password: newPassword,
            })
            .then((response) => response.data),
    listSessions: () =>
        apiClient
            .get<AuthSession[]>(`${API_URL}/auth/sessions`)
            .then((response) => response.data),
    revokeSession: (sessionId: string) =>
        apiClient
            .delete<{ message: string }>(`${API_URL}/auth/sessions/${sessionId}`)
            .then((response) => response.data),
    revokeAllSessions: () =>
        apiClient
            .post<{ message: string }>(`${API_URL}/auth/sessions/revoke-all`)
            .then((response) => response.data),
};

export const adminAuthApi = {
    currentAdmin: () =>
        apiClient
            .get<AdminAuthResponse>(`${API_URL}/admin/auth/me`, { timeout: 10_000 })
            .then((response) => response.data),
    login: (username: string, password: string) =>
        apiClient
            .post<AdminAuthResponse>(
                `${API_URL}/admin/auth/login`,
                { identifier: username, password },
                { timeout: 15_000 },
            )
            .then((response) => response.data),
    logout: () =>
        apiClient
            .post<{ message: string }>(`${API_URL}/admin/auth/logout`)
            .then((response) => response.data),
    changePassword: (currentPassword: string, newPassword: string) =>
        apiClient
            .post<{ message: string }>(`${API_URL}/admin/auth/password`, {
                current_password: currentPassword,
                new_password: newPassword,
            })
            .then((response) => response.data),
};

export const workspaceApi = {
    list: (includeDeleted = false) =>
        apiClient
            .get<UserWorkspace[]>(`${API_URL}/workspaces`, {
                params: { include_deleted: includeDeleted },
            })
            .then((response) => response.data),
    create: (name: string) =>
        apiClient
            .post<UserWorkspace>(`${API_URL}/workspaces`, { name })
            .then((response) => response.data),
    select: (workspaceId: string) =>
        apiClient
            .post<UserWorkspace>(`${API_URL}/workspaces/${workspaceId}/select`)
            .then((response) => response.data),
    rename: (workspaceId: string, name: string, expectedVersion: number) =>
        apiClient
            .patch<UserWorkspace>(`${API_URL}/workspaces/${workspaceId}`, {
                name,
                expected_version: expectedVersion,
            })
            .then((response) => response.data),
    remove: (workspaceId: string) =>
        apiClient
            .delete<{ message: string }>(`${API_URL}/workspaces/${workspaceId}`)
            .then((response) => response.data),
    restore: (workspaceId: string) =>
        apiClient
            .post<UserWorkspace>(`${API_URL}/workspaces/${workspaceId}/restore`)
            .then((response) => response.data),
};

export interface MediaUploadResponse {
    id: string;
    mime_type: string;
    size_bytes: number;
    checksum_sha256: string;
}

export interface MediaMetadata extends MediaUploadResponse {
    project_id: string | null;
    lifecycle_state: string;
    provenance: Record<string, unknown>;
    created_at: string;
}

export interface MediaAccessResponse {
    media_id: string;
    url: string;
    expires_at: string;
}

export const mediaApi = {
    upload: async (file: File, projectId?: string): Promise<MediaUploadResponse> => {
        const formData = new FormData();
        formData.append("file", file);
        const response = await apiClient.post<MediaUploadResponse>(`${API_URL}/media`, formData, {
            params: projectId ? { project_id: projectId } : undefined,
            headers: { "Content-Type": "multipart/form-data" },
        });
        await resolveMediaUrl(toMediaReference(response.data.id));
        return response.data;
    },
    metadata: (mediaId: string) =>
        apiClient
            .get<MediaMetadata>(`${API_URL}/media/${mediaId}`)
            .then((response) => response.data),
    access: (mediaId: string, expiresSeconds = 300) =>
        apiClient
            .get<MediaAccessResponse>(`${API_URL}/media/${mediaId}/access`, {
                params: { expires_seconds: expiresSeconds },
            })
            .then((response) => response.data),
    remove: (mediaId: string) =>
        apiClient
            .delete<{ status: string; id: string }>(`${API_URL}/media/${mediaId}`)
            .then((response) => response.data),
};

export async function resolveMediaUrl(reference: string): Promise<string> {
    const mediaId = parseMediaReference(reference);
    if (!mediaId) return reference;
    const key = mediaCacheKey(mediaId);
    const cached = mediaAccessCache.get(key);
    if (cached && cached.expiresAt > Date.now() + 30_000) return cached.url;

    const pending = mediaAccessRequests.get(key);
    if (pending) return pending;

    const request = mediaApi
        .access(mediaId)
        .then((access) => {
            const parsedExpiry = Date.parse(access.expires_at);
            mediaAccessCache.set(key, {
                url: access.url,
                expiresAt: Number.isFinite(parsedExpiry)
                    ? parsedExpiry
                    : Date.now() + 240_000,
            });
            return access.url;
        })
        .finally(() => mediaAccessRequests.delete(key));
    mediaAccessRequests.set(key, request);
    return request;
}

export async function prefetchMediaReferences(value: unknown): Promise<void> {
    const references = new Set<string>();
    const visit = (item: unknown): void => {
        if (typeof item === "string") {
            if (parseMediaReference(item)) references.add(item);
            return;
        }
        if (Array.isArray(item)) {
            item.forEach(visit);
            return;
        }
        if (!item || typeof item !== "object") return;
        Object.values(item as Record<string, unknown>).forEach(visit);
    };
    visit(value);
    await Promise.allSettled(Array.from(references, resolveMediaUrl));
}

export function getSafeAuthError(error: unknown): AuthAPIError {
    const safeError = getSafeApiError(error);
    return { code: safeError.code, message: safeError.message };
}

export type ProviderMode = "dashscope" | "vendor";
export type SeedanceProviderMode = "ark" | "mulerouter";

function normalizeCloudXlinksT2IParameters(
    parameters: Record<string, unknown> | undefined,
): Record<string, string | number> {
    const requestedSize = String(parameters?.size || parameters?.resolution || "1024x1024")
        .toLowerCase()
        .replace("*", "x");
    let size = "1024x1024";
    if (["portrait", "9:16", "3:4"].includes(requestedSize)) {
        size = "1024x1536";
    } else if (["landscape", "16:9", "4:3"].includes(requestedSize)) {
        size = "1536x1024";
    } else if (requestedSize.includes("x")) {
        const [width, height] = requestedSize.split("x", 2).map(Number);
        if (Number.isFinite(width) && Number.isFinite(height) && width !== height) {
            size = width > height ? "1536x1024" : "1024x1536";
        }
    }
    const requestedQuality = String(parameters?.quality || "high").toLowerCase();
    const quality = ["auto", "low", "medium", "high"].includes(requestedQuality)
        ? requestedQuality
        : "high";
    const requestedBackground = String(parameters?.background || "auto").toLowerCase();
    const background = ["auto", "opaque", "transparent"].includes(requestedBackground)
        ? requestedBackground
        : "auto";
    return {
        count: 1,
        size,
        quality,
        output_format: "png",
        background,
    };
}

export type AdminAICapability =
    | "script.analysis"
    | "prompt.polish"
    | "image.t2i"
    | "image.i2i"
    | "video.t2v"
    | "video.i2v"
    | "video.r2v"
    | "video.v2v"
    | "speech.tts"
    | "audio.sfx";

export interface EffectiveImageEngine {
    capability: "image.t2i";
    model_id: string;
    model_display_name: string;
    provider_display_name: string;
    features: {
        character_design_sheet: boolean;
    };
}

export interface AdminParameterRule {
    name: string;
    value_type: "string" | "integer" | "number" | "boolean";
    required?: boolean;
    minimum?: number | null;
    maximum?: number | null;
    choices?: Array<string | number | boolean> | null;
}

export interface AdminModelRoute {
    capability: AdminAICapability;
    display_name_zh: string;
    provider: string;
    provider_model_id: string;
    enabled: boolean;
    is_primary: boolean;
    priority: number;
    default_parameters: Record<string, string | number | boolean>;
    parameter_schema: AdminParameterRule[];
    metering_formula: Record<string, unknown> & { kind: "llm" | "image" | "video" | "speech" };
    fallback_policy: {
        enabled: boolean;
        eligible_error_codes: string[];
        max_attempts: number;
        require_nonbillable_previous_attempt: boolean;
    };
    secret_ref: string;
}

export interface AdminPlatformConfig {
    tokens_per_ticket: number;
    registration_initial_grant_microtickets: number;
    session_idle_seconds: number;
    session_absolute_seconds: number;
    max_sessions_per_user: number;
    max_ai_concurrency_per_user: number;
    exposed_capabilities: AdminAICapability[];
    feature_flags: {
        registration_mode: RegistrationMode;
        new_ai_tasks_enabled: boolean;
    };
    operational: {
        signed_media_url_seconds: number;
        soft_delete_retention_days: number;
        stale_hold_minutes: number;
    };
}

export interface AdminDeploymentState {
    worker_concurrency: number;
    authority: "environment_or_compose";
    activation_required: boolean;
    deployment_mode: "desktop" | "cloud" | "test" | "unknown";
    object_store_adapter: "oss" | "deterministic" | "unknown";
    provider_adapter: "production" | "deterministic" | "unknown";
    test_adapters_enabled: boolean;
    oss_private: boolean;
    registration_emergency_disabled: boolean;
    new_ai_tasks_emergency_disabled: boolean;
    resource_fingerprints: {
        postgresql: string | null;
        redis: string | null;
        oss_bucket: string | null;
        provider_account: string | null;
    };
}

export interface AdminConfigurationVersion {
    id: string;
    version_number: number;
    status: "draft" | "active" | "superseded" | "disabled";
    schema_version: number;
    reason: string;
    platform: AdminPlatformConfig;
    routes: AdminModelRoute[];
    created_by_user_id: string;
    created_at: string;
    activated_at?: string | null;
    superseded_at?: string | null;
}

export interface AdminConfigurationDraft {
    reason: string;
    platform: AdminPlatformConfig;
    routes: AdminModelRoute[];
}

export const adminConfigurationApi = {
    getDeploymentState: () =>
        apiClient
            .get<AdminDeploymentState>(`${API_URL}/admin/configuration/deployment-state`, {
                withCredentials: true,
            })
            .then((response) => response.data),
    listVersions: () =>
        apiClient
            .get<AdminConfigurationVersion[]>(`${API_URL}/admin/configuration/versions`, {
                withCredentials: true,
            })
            .then((response) => response.data),
    getActive: () =>
        apiClient
            .get<AdminConfigurationVersion>(`${API_URL}/admin/configuration/active`, {
                withCredentials: true,
            })
            .then((response) => response.data),
    getVersion: (versionId: string) =>
        apiClient
            .get<AdminConfigurationVersion>(
                `${API_URL}/admin/configuration/versions/${versionId}`,
                { withCredentials: true },
            )
            .then((response) => response.data),
    createVersion: (draft: AdminConfigurationDraft) =>
        apiClient
            .post<AdminConfigurationVersion>(
                `${API_URL}/admin/configuration/versions`,
                draft,
                authenticatedMutationConfig(),
            )
            .then((response) => response.data),
    validateVersion: (versionId: string) =>
        apiClient
            .post<{ valid: true; message: string; configuration: AdminConfigurationVersion }>(
                `${API_URL}/admin/configuration/versions/${versionId}/validate`,
                undefined,
                authenticatedMutationConfig(),
            )
            .then((response) => response.data),
    activateVersion: (versionId: string, reason: string) =>
        apiClient
            .post<AdminConfigurationVersion>(
                `${API_URL}/admin/configuration/versions/${versionId}/activate`,
                { reason },
                authenticatedMutationConfig(),
            )
            .then((response) => response.data),
    disableVersion: (versionId: string, reason: string) =>
        apiClient
            .post<AdminConfigurationVersion>(
                `${API_URL}/admin/configuration/versions/${versionId}/disable`,
                { reason },
                authenticatedMutationConfig(),
            )
            .then((response) => response.data),
    rollbackVersion: (versionId: string, reason: string) =>
        apiClient
            .post<AdminConfigurationVersion>(
                `${API_URL}/admin/configuration/versions/${versionId}/rollback`,
                { reason },
                authenticatedMutationConfig(),
            )
            .then((response) => response.data),
};

export type AdminTicketOperation = "grant" | "debit" | "compensation";

export interface AdminTicketWallet {
    user_id: string;
    available_microtickets: string;
    held_microtickets: string;
    total_microtickets: string;
    available_tickets: string;
    held_tickets: string;
    total_tickets: string;
    version: number;
}

export interface AdminTicketLedgerItem {
    id: string;
    entry_type: "grant" | "hold" | "settlement" | "release" | "adjustment" | "compensation";
    amount_microtickets: string;
    amount_tickets: string;
    available_delta: string;
    held_delta: string;
    available_after: string;
    held_after: string;
    reason?: string | null;
    actor_admin_id?: string | null;
    legacy_actor_user_id?: string | null;
    correlation: Record<string, unknown>;
    created_at: string;
}

export interface AdminTicketWalletView {
    wallet: AdminTicketWallet;
    ledger: AdminTicketLedgerItem[];
    total: number;
}

export const adminTicketApi = {
    getWallet: (userId: string, offset = 0, limit = 50) =>
        apiClient
            .get<AdminTicketWalletView>(`${API_URL}/admin/tickets/users/${userId}`, {
                params: { offset, limit },
                withCredentials: true,
            })
            .then((response) => response.data),
    adjust: (
        userId: string,
        operation: AdminTicketOperation,
        amountTickets: string,
        reason: string,
    ) => {
        const endpoint = operation === "compensation" ? "compensate" : operation;
        return apiClient
            .post<{ message: string; wallet: AdminTicketWallet }>(
                `${API_URL}/admin/tickets/users/${userId}/${endpoint}`,
                { amount_tickets: amountTickets, reason },
                authenticatedMutationConfig(),
            )
            .then((response) => response.data);
    },
};

export type AdminUserStatus = "active" | "suspended";

export interface AdminUserItem {
    id: string;
    username?: string | null;
    account_label: string;
    phone?: string | null;
    status: AdminUserStatus;
    status_zh: string;
    phone_verified: boolean;
    available_tickets: string;
    held_tickets: string;
    wallet_exception: boolean;
    workspace_count: number;
    task_summary: { total: number; exceptions: number };
    created_at: string;
    updated_at: string;
}

export interface AdminCreatedUser {
    id: string;
    workspace_id: string;
    phone: string;
    status: "active";
}

export interface AdminActualModel {
    display_name: string;
    model_id: string;
    provider: string;
}

export interface AdminTaskItem {
    id: string;
    user_id: string;
    user_phone: string;
    workspace_id: string;
    project_id?: string | null;
    capability: string;
    status: string;
    status_zh: string;
    quoted_tickets: string;
    provider_billable: boolean;
    cancellation_requested: boolean;
    support_review_reason?: string | null;
    safe_error_message?: string | null;
    actual_model: AdminActualModel;
    created_at: string;
    updated_at: string;
    completed_at?: string | null;
}

export interface AdminUsageItem {
    id: string;
    user_id: string;
    user_phone: string;
    workspace_id: string;
    project_id?: string | null;
    task_id: string;
    capability: string;
    outcome: string;
    outcome_zh: string;
    metering_tokens: string;
    tokens_per_ticket: string;
    charged_tickets: string;
    created_at: string;
}

export interface AdminAuditEventItem {
    id: string;
    actor_user_id?: string | null;
    actor_admin_id?: string | null;
    target_user_id?: string | null;
    workspace_id?: string | null;
    action: string;
    target_type: string;
    target_id?: string | null;
    reason?: string | null;
    correlation_id: string;
    created_at: string;
}

export interface AdminImportBatchItem {
    id: string;
    actor_admin_id?: string | null;
    legacy_actor_user_id?: string | null;
    target_user_id: string;
    target_workspace_id: string;
    source_fingerprint: string;
    status: string;
    status_zh: string;
    has_dry_run_report: boolean;
    has_result_report: boolean;
    has_error_report: boolean;
    created_at: string;
    started_at?: string | null;
    completed_at?: string | null;
    reverted_at?: string | null;
    rollback_reason?: string | null;
}

export interface AdminInvitation {
    id: string;
    phone: string;
    status: "active" | "consumed" | "revoked" | "expired";
    expires_at: string;
    created_at: string;
    consumed_at?: string | null;
    revoked_at?: string | null;
    issue_reason: string;
    revoke_reason?: string | null;
    invitation_code?: string | null;
}

export interface AdminImportIssue {
    code: string;
    message: string;
    source_key: string;
    blocking: boolean;
}

export interface AdminImportDryRunResult {
    batch_id: string;
    message: string;
    ready: boolean;
    source_fingerprint: string;
    planned_counts: Record<string, number>;
    media_bytes: number;
    issues: AdminImportIssue[];
}

export interface AdminImportBatchDetail {
    id: string;
    status: string;
    source_fingerprint: string;
    target_user_id: string;
    target_workspace_id: string;
    item_status_counts: Record<string, number>;
    dry_run_report?: AdminImportDryRunResult | null;
    result_report?: Record<string, unknown> | null;
    error_report?: Record<string, unknown> | null;
    created_at: string;
    started_at?: string | null;
    completed_at?: string | null;
    reverted_at?: string | null;
    rollback_reason?: string | null;
}

export interface AdminPage<T> {
    items: T[];
    total: number;
    offset: number;
    limit: number;
}

export interface AdminUsagePage extends AdminPage<AdminUsageItem> {
    total_metering_tokens: string;
    total_charged_tickets: string;
}

export interface AdminExceptionItem {
    key: string;
    kind: string;
    severity: "info" | "warning" | "high";
    user_id?: string | null;
    workspace_id?: string | null;
    resource_type: string;
    resource_id?: string | null;
    summary: string;
    updated_at?: string | null;
}

export interface AdminDashboard {
    generated_at: string;
    window: { start_at: string; end_at: string };
    users: { total: number; new: number };
    orders: { paid_count: number; net_cash_fen: string; net_ticket_microtickets: string };
    usage: { events: number; metering_tokens: string; charged_microtickets: string };
    tasks: { total: number; by_status: Record<string, number>; support_review: number; failed: number };
    exceptions: { total: number; items: AdminExceptionItem[] };
}

export interface AdminUserOverview {
    account: {
        id: string;
        username?: string | null;
        phone_masked?: string | null;
        status: AdminUserStatus;
        phone_verified: boolean;
        created_at: string;
        updated_at: string;
    };
    wallet: {
        available_microtickets: string;
        available_tickets: string;
        held_microtickets: string;
        held_tickets: string;
        lifetime_recharged_microtickets: string;
        lifetime_refunded_microtickets: string;
    };
    counts: Record<string, number>;
    exceptions: { tasks: number; orders: number; total: number };
    recent_activity: {
        tasks: Array<Record<string, string | null>>;
        orders: Array<Record<string, string | null>>;
    };
}

export type AdminInspectionResource =
    | "workspaces" | "projects" | "series" | "assets" | "media"
    | "tasks" | "attempts" | "usage" | "ledger" | "orders"
    | "sessions" | "audit";

export type ManualRechargeStatus =
    | "pending" | "completed" | "cancelled" | "partially_refunded" | "refunded";

export interface ManualRechargeOrder {
    id: string;
    order_number: string;
    user_id: string;
    cash_amount_fen: string;
    cash_amount_yuan: string;
    ticket_amount_microtickets: string;
    ticket_amount: string;
    currency: "CNY";
    status: ManualRechargeStatus;
    status_zh: string;
    exchange_snapshot: Record<string, unknown>;
    offline_reference?: string | null;
    create_reason: string;
    cancel_reason?: string | null;
    refunded_cash_fen: string;
    refunded_microtickets: string;
    remaining_refundable_cash_fen: string;
    remaining_refundable_microtickets: string;
    version: number;
    created_by_admin_id?: string | null;
    completed_by_admin_id?: string | null;
    cancelled_by_admin_id?: string | null;
    last_refunded_by_admin_id?: string | null;
    created_at: string;
    updated_at: string;
    completed_at?: string | null;
    cancelled_at?: string | null;
    refunded_at?: string | null;
    manual_confirmation: true;
}

export interface ManualRechargePage extends AdminPage<ManualRechargeOrder> {
    total_cash_fen: string;
    total_ticket_microtickets: string;
}

export interface ManualRechargeDetail {
    order: ManualRechargeOrder;
    wallet: {
        available_microtickets: string;
        available_tickets: string;
        held_microtickets: string;
        held_tickets: string;
    };
    events: ManualRechargeEvent[];
    ledger: ManualRechargeLedgerEntry[];
}

export interface ManualRechargeEvent {
    id: string;
    event_type: "created" | "completed" | "cancelled" | "refunded";
    event_type_zh: string;
    actor_admin_id?: string | null;
    cash_amount_fen: string;
    ticket_amount_microtickets: string;
    ledger_entry_id?: string | null;
    version_before: number;
    version_after: number;
    reason: string;
    snapshot: Record<string, unknown>;
    created_at: string;
}

export interface ManualRechargeLedgerEntry {
    id: string;
    entry_type: string;
    amount_microtickets: string;
    available_delta: string;
    available_after: string;
    held_after: string;
    actor_admin_id?: string | null;
    reason?: string | null;
    created_at: string;
}

export interface ManualRechargeReconciliation {
    report_id: string;
    order_id: string;
    status: "consistent" | "mismatch";
    status_zh: string;
    severity: string;
    details: Record<string, unknown>;
}

export interface SystemScenePayload {
    name: string;
    description: string;
    category: string;
    tags: string[];
    prompt: string;
    negative_prompt: string;
    style: string;
    aspect_ratio: "16:9" | "9:16" | "1:1" | "4:3" | "3:4";
    visibility: "enabled" | "disabled";
    sort_order: number;
    schema_version: number;
    cover_media_id?: number | null;
}

export interface SystemScene extends SystemScenePayload {
    id: string;
    version: number;
    lifecycle: "active" | "archived";
    usage_count?: number | null;
    created_at: string;
    updated_at: string;
    archived_at?: string | null;
}

export const adminPlatformApi = {
    listInvitations: () =>
        apiClient
            .get<AdminInvitation[]>(`${API_URL}/admin/invitations`, {
                withCredentials: true,
            })
            .then((response) => response.data),
    createInvitation: (phone: string, expiresAt: string, reason: string) =>
        apiClient
            .post<AdminInvitation>(
                `${API_URL}/admin/invitations`,
                { phone, expires_at: expiresAt, reason },
                authenticatedMutationConfig(),
            )
            .then((response) => response.data),
    revokeInvitation: (invitationId: string, reason: string) =>
        apiClient
            .post<AdminInvitation>(
                `${API_URL}/admin/invitations/${invitationId}/revoke`,
                { reason },
                authenticatedMutationConfig(),
            )
            .then((response) => response.data),
    listUsers: (params: { status?: AdminUserStatus; query?: string; user_id?: string; wallet_exception?: boolean; start_at?: string; end_at?: string; offset?: number; limit?: number } = {}) =>
        apiClient
            .get<AdminPage<AdminUserItem>>(`${API_URL}/admin/users`, {
                params,
                withCredentials: true,
            })
            .then((response) => response.data),
    createUser: (phone: string, password: string, reason: string) =>
        apiClient
            .post<AdminCreatedUser>(
                `${API_URL}/admin/users`,
                { phone, password, reason },
                authenticatedMutationConfig(),
            )
            .then((response) => response.data),
    listTasks: (params: { status?: string; user_id?: string; workspace_id?: string; start_at?: string; end_at?: string; offset?: number; limit?: number } = {}) =>
        apiClient
            .get<AdminPage<AdminTaskItem>>(`${API_URL}/admin/tasks`, {
                params,
                withCredentials: true,
            })
            .then((response) => response.data),
    listUsage: (params: { outcome?: string; user_id?: string; workspace_id?: string; start_at?: string; end_at?: string; offset?: number; limit?: number } = {}) =>
        apiClient
            .get<AdminUsagePage>(`${API_URL}/admin/usage`, {
                params,
                withCredentials: true,
            })
            .then((response) => response.data),
    listAuditEvents: (params: { action?: string; actor_user_id?: string; actor_admin_id?: string; target_user_id?: string; workspace_id?: string; target_type?: string; target_id?: string; purpose?: string; correlation_id?: string; start_at?: string; end_at?: string; offset?: number; limit?: number } = {}) =>
        apiClient
            .get<AdminPage<AdminAuditEventItem>>(`${API_URL}/admin/audit-events`, {
                params,
                withCredentials: true,
            })
            .then((response) => response.data),
    listImportBatches: (params: { status?: string; offset?: number; limit?: number } = {}) =>
        apiClient
            .get<AdminPage<AdminImportBatchItem>>(`${API_URL}/admin/import-batches`, {
                params,
                withCredentials: true,
            })
            .then((response) => response.data),
    dryRunImport: (payload: {
        target_user_id: string;
        target_workspace_id: string;
        source_directory: string;
        include_playground: boolean;
        missing_media_policy: "reject" | "clear";
    }) =>
        apiClient
            .post<AdminImportDryRunResult>(
                `${API_URL}/admin/imports/dry-run`,
                payload,
                authenticatedMutationConfig(),
            )
            .then((response) => response.data),
    getImportBatch: (batchId: string) =>
        apiClient
            .get<AdminImportBatchDetail>(`${API_URL}/admin/imports/${batchId}`, {
                withCredentials: true,
            })
            .then((response) => response.data),
    executeImport: (batchId: string) =>
        apiClient
            .post<Record<string, unknown>>(
                `${API_URL}/admin/imports/${batchId}/execute`,
                {},
                authenticatedMutationConfig(),
            )
            .then((response) => response.data),
    rollbackImport: (batchId: string, reason: string) =>
        apiClient
            .post<{ message: string; reverted_items: number; preserved_items: number }>(
                `${API_URL}/admin/imports/${batchId}/rollback`,
                { reason },
                authenticatedMutationConfig(),
            )
            .then((response) => response.data),
    updateUserStatus: (userId: string, status: AdminUserStatus, reason: string) =>
        apiClient
            .post<{ message: string }>(
                `${API_URL}/admin/users/${userId}/${status === "active" ? "reactivate" : "suspend"}`,
                { reason },
                authenticatedMutationConfig(),
            )
            .then((response) => response.data),
    revokeUserSessions: (userId: string, reason: string) =>
        apiClient
            .post<{ message: string }>(
                `${API_URL}/admin/users/${userId}/revoke-sessions`,
                { reason },
                authenticatedMutationConfig(),
            )
            .then((response) => response.data),
    issueResetCredential: (userId: string, reason: string) =>
        apiClient
            .post<{ credential: string; expires_at: string }>(
                `${API_URL}/admin/users/${userId}/reset-credentials`,
                { reason },
                authenticatedMutationConfig(),
            )
            .then((response) => response.data),
    dashboard: (params: { start_at?: string; end_at?: string } = {}) =>
        apiClient.get<AdminDashboard>(`${API_URL}/admin/dashboard`, { params }).then((response) => response.data),
    exceptions: (limit = 50) =>
        apiClient.get<{ items: AdminExceptionItem[]; total: number; generated_at: string }>(
            `${API_URL}/admin/exceptions`, { params: { limit } },
        ).then((response) => response.data),
    userOverview: (userId: string) =>
        apiClient.get<AdminUserOverview>(`${API_URL}/admin/users/${userId}/overview`).then((response) => response.data),
    userResources: (
        userId: string,
        resource: AdminInspectionResource,
        params: { workspace_id?: string; status?: string; asset_type?: string; offset?: number; limit?: number } = {},
    ) => apiClient.get<AdminPage<Record<string, unknown>>>(
        `${API_URL}/admin/users/${userId}/resources/${resource}`, { params },
    ).then((response) => response.data),
    taskDetail: (userId: string, workspaceId: string, taskId: string) =>
        apiClient.get<Record<string, unknown>>(`${API_URL}/admin/users/${userId}/tasks/${taskId}`, {
            params: { workspace_id: workspaceId },
        }).then((response) => response.data),
    viewScript: (userId: string, projectId: string, workspaceId: string, purpose: string) =>
        apiClient.post<{ project_id: string; title: string; text: string; truncated: boolean }>(
            `${API_URL}/admin/users/${userId}/scripts/${projectId}/view`,
            { workspace_id: Number(workspaceId), purpose },
        ).then((response) => response.data),
    viewTaskPrompt: (userId: string, taskId: string, workspaceId: string, purpose: string) =>
        apiClient.post<Record<string, unknown>>(
            `${API_URL}/admin/users/${userId}/tasks/${taskId}/prompt`,
            { workspace_id: Number(workspaceId), purpose },
        ).then((response) => response.data),
    previewMedia: (userId: string, mediaId: string, workspaceId: string, purpose: string) =>
        apiClient.post<{ media_id: string; url: string; expires_at: string; disposition: "inline" }>(
            `${API_URL}/admin/users/${userId}/media/${mediaId}/preview`,
            { workspace_id: Number(workspaceId), purpose },
        ).then((response) => response.data),
    exportCsv: (
        resource: "users" | "orders" | "usage" | "tasks",
        params: { start_at: string; end_at: string; purpose: string; user_id?: string; status?: string },
    ) => apiClient.post<Blob>(`${API_URL}/admin/exports/${resource}.csv`, null, {
        params,
        responseType: "blob",
    }).then((response) => response.data),
};

export const adminRechargeApi = {
    list: (params: { order_number?: string; user_id?: string; status?: ManualRechargeStatus; actor_admin_id?: string; offline_reference?: string; start_at?: string; end_at?: string; offset?: number; limit?: number } = {}) =>
        apiClient.get<ManualRechargePage>(`${API_URL}/admin/recharge-orders`, { params }).then((response) => response.data),
    get: (orderId: string) =>
        apiClient.get<ManualRechargeDetail>(`${API_URL}/admin/recharge-orders/${orderId}`).then((response) => response.data),
    create: (payload: { user_id: number; cash_amount_fen: number; ticket_amount_microtickets: number; offline_reference?: string; reason: string }, idempotencyKey = createIdempotencyKey("recharge-create")) =>
        apiClient.post<ManualRechargeOrder>(`${API_URL}/admin/recharge-orders`, payload, {
            headers: { "Idempotency-Key": idempotencyKey },
        }).then((response) => response.data),
    complete: (orderId: string, expectedVersion: number, reason: string, idempotencyKey = createIdempotencyKey("recharge-complete")) =>
        apiClient.post<ManualRechargeOrder>(`${API_URL}/admin/recharge-orders/${orderId}/complete`, {
            expected_version: expectedVersion, reason,
        }, { headers: { "Idempotency-Key": idempotencyKey } }).then((response) => response.data),
    cancel: (orderId: string, expectedVersion: number, reason: string, idempotencyKey = createIdempotencyKey("recharge-cancel")) =>
        apiClient.post<ManualRechargeOrder>(`${API_URL}/admin/recharge-orders/${orderId}/cancel`, {
            expected_version: expectedVersion, reason,
        }, { headers: { "Idempotency-Key": idempotencyKey } }).then((response) => response.data),
    refund: (orderId: string, payload: { expected_version: number; cash_amount_fen: number; ticket_amount_microtickets: number; reason: string }, idempotencyKey = createIdempotencyKey("recharge-refund")) =>
        apiClient.post<ManualRechargeOrder>(`${API_URL}/admin/recharge-orders/${orderId}/refund`, payload, {
            headers: { "Idempotency-Key": idempotencyKey },
        }).then((response) => response.data),
    reconcile: (orderId: string, reason: string) =>
        apiClient.post<ManualRechargeReconciliation>(`${API_URL}/admin/recharge-orders/${orderId}/reconcile`, { reason }).then((response) => response.data),
};

export const systemSceneAdminApi = {
    list: (params: { scene_id?: string; query?: string; category?: string; tag?: string; visibility?: "enabled" | "disabled"; lifecycle?: "active" | "archived"; schema_version?: string; start_at?: string; end_at?: string; offset?: number; limit?: number } = {}) =>
        apiClient.get<AdminPage<SystemScene>>(`${API_URL}/admin/system-scenes`, { params }).then((response) => response.data),
    get: (sceneId: string) =>
        apiClient.get<SystemScene>(`${API_URL}/admin/system-scenes/${sceneId}`).then((response) => response.data),
    create: (scene: SystemScenePayload, reason: string) =>
        apiClient.post<SystemScene>(`${API_URL}/admin/system-scenes`, { scene, reason }).then((response) => response.data),
    update: (sceneId: string, scene: SystemScenePayload, expectedVersion: number, reason: string, confirmedUsageCount?: number) =>
        apiClient.put<SystemScene>(`${API_URL}/admin/system-scenes/${sceneId}`, {
            scene, expected_version: expectedVersion, reason, confirmed_usage_count: confirmedUsageCount,
        }).then((response) => response.data),
    visibility: (sceneId: string, visibility: "enabled" | "disabled", expectedVersion: number, reason: string, confirmedUsageCount?: number) =>
        apiClient.post<SystemScene>(`${API_URL}/admin/system-scenes/${sceneId}/visibility`, {
            visibility, expected_version: expectedVersion, reason, confirmed_usage_count: confirmedUsageCount,
        }).then((response) => response.data),
    archive: (sceneId: string, expectedVersion: number, reason: string, confirmedUsageCount?: number) =>
        apiClient.post<SystemScene>(`${API_URL}/admin/system-scenes/${sceneId}/archive`, {
            expected_version: expectedVersion, reason, confirmed_usage_count: confirmedUsageCount,
        }).then((response) => response.data),
    reorder: (sceneId: string, sortOrder: number, expectedVersion: number, reason: string) =>
        apiClient.post<SystemScene>(`${API_URL}/admin/system-scenes/${sceneId}/reorder`, {
            sort_order: sortOrder, expected_version: expectedVersion, reason,
        }).then((response) => response.data),
    uploadMedia: (file: File, reason: string) => {
        const body = new FormData();
        body.append("file", file);
        body.append("reason", reason);
        return apiClient.post<{ media_id: string; mime_type: string; size_bytes: number; checksum_sha256: string }>(
            `${API_URL}/admin/system-scenes/media`, body,
        ).then((response) => response.data);
    },
    mediaAccess: (mediaId: string) =>
        apiClient.get<{ media_id: string; url: string; expires_at: string }>(
            `${API_URL}/admin/system-scenes/media/${mediaId}/access`,
        ).then((response) => response.data),
};

export interface SystemSceneCopyResult {
    asset_record_id: string;
    asset_id: string;
    scope: "workspace" | "project";
    project_id?: string | null;
    workspace_id: string;
    source_system_scene_id: string;
    source_version: number;
    version: number;
}

export const systemSceneApi = {
    list: () => apiClient.get<{ items: SystemScene[] }>(`${API_URL}/system-scenes`)
        .then((response) => response.data.items),
    get: (sceneId: string) => apiClient.get<SystemScene>(`${API_URL}/system-scenes/${sceneId}`)
        .then((response) => response.data),
    mediaAccess: (mediaId: string) => apiClient.get<{ media_id: string; url: string; expires_at: string }>(
        `${API_URL}/system-scenes/media/${mediaId}/access`,
    ).then((response) => response.data),
    copy: (sceneId: string, projectId?: string | null) => apiClient.post<SystemSceneCopyResult>(
        `${API_URL}/system-scenes/${sceneId}/copy`,
        { project_id: projectId ? Number(projectId) : null },
    ).then((response) => response.data),
};

export interface UserTicketWallet {
    available_microtickets: string;
    held_microtickets: string;
    total_microtickets: string;
    available_tickets: string;
    held_tickets: string;
    total_tickets: string;
    version: number;
}

export interface UserTicketLedgerItem {
    id: string;
    entry_type: string;
    operation_zh: string;
    workspace_id?: string | null;
    project_id?: string | null;
    task_id?: string | null;
    metering_tokens?: string | null;
    amount_microtickets: string;
    amount_tickets: string;
    display_delta_microtickets: string;
    display_delta_tickets: string;
    available_after: string;
    held_after: string;
    available_after_tickets: string;
    held_after_tickets: string;
    status: string;
    status_zh: string;
    reason?: string | null;
    created_at: string;
}

export interface UserTicketUsageItem {
    id: string;
    workspace_id: string;
    project_id?: string | null;
    task_id: string;
    capability: string;
    outcome: string;
    status_zh: string;
    metering_tokens: string;
    charged_microtickets: string;
    charged_tickets: string;
    tokens_per_ticket: string;
    created_at: string;
}

export interface UserTicketHistoryResponse<T> {
    view: "ledger" | "usage";
    items: T[];
    total: number;
    offset: number;
    limit: number;
}

export const userTicketApi = {
    getWallet: () =>
        apiClient
            .get<UserTicketWallet>(`${API_URL}/wallet`, { withCredentials: true })
            .then((response) => response.data),
    getLedger: (offset = 0, limit = 20) =>
        apiClient
            .get<UserTicketHistoryResponse<UserTicketLedgerItem>>(`${API_URL}/wallet/history`, {
                params: { view: "ledger", offset, limit },
                withCredentials: true,
            })
            .then((response) => response.data),
    getUsage: (offset = 0, limit = 20) =>
        apiClient
            .get<UserTicketHistoryResponse<UserTicketUsageItem>>(`${API_URL}/wallet/history`, {
                params: { view: "usage", offset, limit },
                withCredentials: true,
            })
            .then((response) => response.data),
};

/**
 * PR-3g #3 · TTS voice metadata returned by GET /voices.
 * Family-aware fields (family/dialect/lang_primary/supports_instruction)
 * power the Voice picker modal's tabbed UI (Q15.5 B):
 *   Tab 1 系统音色 → origin === "system"
 *   Tab 2 我的复刻 → origin === "clone"   (PR-3h)
 *   Tab 3 我的设计 → origin === "design"  (PR-3i)
 * Inside Tab 1, group by family (cosyvoice / qwen3) + dialect markers.
 */
export interface VoiceMeta {
    id: string;
    name: string;
    gender: "Male" | "Female" | "Neutral" | "Unknown";
    model: string;                                            // backend model id (cosyvoice-v3-flash / qwen3-tts-flash / ...)
    family: "cosyvoice" | "qwen3";
    supports_instruction: boolean;
    dialect?: string | null;                                  // 'shanghai' | 'beijing' | 'sichuan' | 'cantonese' | etc.
    lang_primary?: string | null;                             // 'es' | 'ru' | 'it' | 'ko' | 'ja' | 'de' | 'fr' for international
    origin: "system" | "clone" | "design";
}

/**
 * PR-3h · Custom voice entry from series.custom_voices[].
 * Returned by GET /series/{id}/custom_voices and POST /voice/clone (single).
 * Picker tabs 2/3 render these alongside the system catalog.
 */
export interface CustomVoice {
    id: string;                            // dashscope voice_id
    label: string;                         // user-given display name
    origin: "clone" | "design";
    target_model: string;                  // e.g. "cosyvoice-v3.5-plus"
    family: "cosyvoice" | "qwen3";
    created_at: number;
    source_audio_url?: string | null;      // clone-specific
    voice_prompt?: string | null;          // design-specific (PR-3i)
}

export interface EnvConfigPayload {
    DASHSCOPE_API_KEY?: string;
    ALIBABA_CLOUD_ACCESS_KEY_ID?: string;
    ALIBABA_CLOUD_ACCESS_KEY_SECRET?: string;
    OSS_BUCKET_NAME?: string;
    OSS_ENDPOINT?: string;
    OSS_BASE_PATH?: string;
    OSS_ENABLE?: boolean;
    KLING_PROVIDER_MODE?: ProviderMode;
    VIDU_PROVIDER_MODE?: ProviderMode;
    PIXVERSE_PROVIDER_MODE?: ProviderMode;
    SEEDANCE_PROVIDER_MODE?: SeedanceProviderMode;
    KLING_ACCESS_KEY?: string;
    KLING_SECRET_KEY?: string;
    VIDU_API_KEY?: string;
    ARK_API_KEY?: string;
    ARK_SEEDANCE_MODEL?: string;
    endpoint_overrides?: Record<string, string>;
    // Secrets from GET are masked (bullets + last 4 chars). This map reports
    // which credential fields are actually configured on the backend.
    secrets_configured?: Record<string, boolean>;
    [key: string]: string | Record<string, string> | Record<string, boolean> | boolean | undefined;
}

// R2V v2 Phase 4 — Cross-episode reconcile types
export interface ReconcileSuggestion {
    local_id: string;
    local_name: string;
    suggested_series_id: string | null;
    suggested_series_name: string | null;
    confidence: number;
}

export interface BgmPreset {
    id: string;
    label: string;
    mood: string;
    url: string;
}

export interface ReconcileAction {
    local_id: string;
    action: "merge_into_series" | "create_new_in_series" | "skip";
    target_series_id?: string;
}

export interface VideoTask {
    id: string;
    project_id: string;
    image_url: string;
    prompt: string;
    status: "pending" | "processing" | "completed" | "failed";
    video_url?: string;
    duration: number;
    seed?: number;
    resolution: string;
    generate_audio: boolean;
    audio_url?: string;
    prompt_extend: boolean;
    negative_prompt?: string;
    created_at: number;
    model?: string;
    frame_id?: string;
    generation_mode?: string;
    reference_video_urls?: string[];
    reference_image_urls?: string[];
    ratio?: string;
    /** Failure reason set by pipeline / cancel / orphan recovery. */
    error?: string | null;
    /** User-starred shortlist flag (multi-select per shot) — set via
     *  PATCH /annotate. Optional on the wire so older task records
     *  parse unchanged. */
    is_starred?: boolean;
    /** User-attached short free-text note (≤20 chars, server-truncated). */
    label?: string | null;
    /** Source tab in the Storyboard R2V workbench. Pre-Phase-2 records
     *  parse with null/undefined; CandidatesSection falls back to
     *  generation_mode to bucket them in that case. */
    workbench_tab?: "t2i_i2v" | "direct_r2v" | null;
    /** Provider-side identifiers (Issue 17). Used by TaskQueuePanel to let
     *  users copy IDs into the provider's console (Bailian / 百炼 etc.) for
     *  diagnosis. Different platforms use different naming — these are
     *  normalized canonical fields. provider_request_id may be absent for
     *  platforms that don't expose one (Vidu / PixVerse). */
    provider_name?: string | null;
    provider_task_id?: string | null;
    provider_request_id?: string | null;
}

export type AIServerTaskStatus =
    | "reserved"
    | "queued"
    | "running"
    | "provider_succeeded"
    | "succeeded"
    | "failed"
    | "cancelled"
    | "support_review";

export interface AITaskStatusResponse {
    id: string;
    workspace_id: string;
    project_id?: string | null;
    capability: string;
    status: AIServerTaskStatus;
    status_zh: string;
    quoted_microtickets: string;
    quoted_tickets: string;
    cancellation_requested: boolean;
    support_review: boolean;
    safe_error?: { code?: string | null; message?: string | null } | null;
    media_ids: string[];
    created_at?: string | null;
    updated_at?: string | null;
    started_at?: string | null;
    completed_at?: string | null;
    actual_model?: { display_name: string; model_id: string };
    tokens_per_ticket?: string;
    result_content?: string | Record<string, unknown> | null;
}

export interface AITaskDetailResponse extends AITaskStatusResponse {
    attempts: Array<{
        id: string;
        attempt_number: number;
        status: string;
        model_id?: string | null;
        created_at?: string | null;
        started_at?: string | null;
        completed_at?: string | null;
    }>;
    billing: {
        hold_ids: string[];
        open_hold_ids: string[];
        usage_event_ids: string[];
    };
}

export interface ClientAITask extends Omit<AITaskStatusResponse, "status"> {
    status: "pending" | "processing" | "completed" | "failed";
    raw_status: AIServerTaskStatus;
    media_references: string[];
    image_url?: string;
    video_url?: string;
    audio_url?: string;
    result_url?: string;
    error?: string;
}

export interface AITaskListResponse {
    items: ClientAITask[];
    total: number;
    offset: number;
    limit: number;
}

export type ClientAITaskDetail = ClientAITask &
    Omit<AITaskDetailResponse, keyof AITaskStatusResponse>;

export interface SubmittedAITaskResponse {
    task_id: string;
    attempt_id?: string;
    status: AIServerTaskStatus;
    capability?: string;
    quoted_microtickets?: string;
    quoted_tickets?: string;
    tokens_per_ticket?: string;
    actual_model?: { display_name?: string; model_id?: string };
    reused?: boolean;
    dispatched?: boolean;
}

function normalizeLegacyTaskStatus(
    status: string,
): "pending" | "processing" | "completed" | "failed" {
    if (status === "reserved" || status === "queued" || status === "pending") {
        return "pending";
    }
    if (status === "running" || status === "provider_succeeded" || status === "processing") {
        return "processing";
    }
    if (status === "succeeded" || status === "completed") return "completed";
    return "failed";
}

async function projectAITask(task: AITaskStatusResponse): Promise<ClientAITask> {
    const mediaReferences = (task.media_ids || []).map(toMediaReference);
    await prefetchMediaReferences(mediaReferences);
    const primaryMedia = mediaReferences[0];
    const capability = task.capability || "";
    const error = task.safe_error?.message || undefined;
    return {
        ...task,
        status: normalizeLegacyTaskStatus(task.status),
        raw_status: task.status,
        media_references: mediaReferences,
        ...(primaryMedia ? { result_url: primaryMedia } : {}),
        ...(primaryMedia && capability.startsWith("video.")
            ? { video_url: primaryMedia }
            : {}),
        ...(primaryMedia && (capability.startsWith("speech.") || capability.startsWith("audio."))
            ? { audio_url: primaryMedia }
            : {}),
        ...(primaryMedia && !capability.startsWith("video.") && !capability.startsWith("speech.") && !capability.startsWith("audio.")
            ? { image_url: primaryMedia }
            : {}),
        ...(error ? { error } : {}),
    };
}

export function normalizeSubmittedAITaskResponse<T>(value: T): T {
    if (!value || typeof value !== "object" || !("task_id" in value)) return value;
    const submitted = value as unknown as SubmittedAITaskResponse;
    return {
        ...submitted,
        id: submitted.task_id,
        _task_id: submitted.task_id,
        raw_status: submitted.status,
        status: normalizeLegacyTaskStatus(submitted.status),
    } as T;
}

export const aiTaskApi = {
    list: async (params: { status?: AIServerTaskStatus; offset?: number; limit?: number } = {}): Promise<AITaskListResponse> => {
        const response = await apiClient.get<{
            items: AITaskStatusResponse[];
            total: number;
            offset: number;
            limit: number;
        }>(`${API_URL}/ai/tasks`, { params });
        return {
            ...response.data,
            items: await Promise.all(response.data.items.map(projectAITask)),
        };
    },
    getStatus: async (taskId: string): Promise<ClientAITask> => {
        const response = await apiClient.get<AITaskStatusResponse>(
            `${API_URL}/ai/tasks/${taskId}/status`,
        );
        return projectAITask(response.data);
    },
    getDetail: async (taskId: string): Promise<ClientAITaskDetail> => {
        const response = await apiClient.get<AITaskDetailResponse>(
            `${API_URL}/ai/tasks/${taskId}`,
        );
        return {
            ...response.data,
            ...await projectAITask(response.data),
        } as ClientAITaskDetail;
    },
    cancel: async (taskId: string) => {
        const response = await apiClient.post<AITaskStatusResponse & {
            cancellation_outcome: string;
            message: string;
            released_microtickets: string;
            released_tickets: string;
        }>(`${API_URL}/ai/tasks/${taskId}/cancel`);
        return {
            ...response.data,
            ...await projectAITask(response.data),
        };
    },
};

const TEXT_AI_POLL_INTERVAL_MS = 1_000;
const TEXT_AI_POLL_TIMEOUT_MS = 600_000;

async function waitForTextAITaskResult(taskId: string): Promise<string> {
    const deadline = Date.now() + TEXT_AI_POLL_TIMEOUT_MS;
    while (true) {
        const task = await aiTaskApi.getStatus(taskId);
        if (task.raw_status === "succeeded") {
            const content = typeof task.result_content === "string"
                ? task.result_content.trim()
                : "";
            if (!content) throw new Error("AI 任务未返回可用文本");
            return content;
        }
        if (["failed", "cancelled", "support_review"].includes(task.raw_status)) {
            throw new Error(task.safe_error?.message || "AI 生成失败，请稍后重试");
        }
        if (Date.now() >= deadline) {
            throw new Error("AI 生成等待超时，可稍后在任务记录中查看结果");
        }
        await new Promise((resolve) => setTimeout(resolve, TEXT_AI_POLL_INTERVAL_MS));
    }
}

type ScriptAnalysisPreview = {
    characters: any[];
    scenes: any[];
    props: any[];
};

function parseStructuredAITaskResult(
    content: string | Record<string, unknown> | null | undefined,
    invalidMessage: string,
): Record<string, unknown> {
    let parsed: unknown = content;
    if (typeof content === "string") {
        const normalized = content
            .trim()
            .replace(/^```(?:json)?\s*/i, "")
            .replace(/\s*```$/, "");
        try {
            parsed = JSON.parse(normalized);
        } catch {
            const start = normalized.indexOf("{");
            const end = normalized.lastIndexOf("}");
            if (start >= 0 && end > start) {
                try {
                    parsed = JSON.parse(normalized.slice(start, end + 1));
                } catch {
                    throw new Error(invalidMessage);
                }
            } else {
                throw new Error(invalidMessage);
            }
        }
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error(invalidMessage);
    }
    return parsed as Record<string, unknown>;
}

function parseScriptAnalysisPreview(
    content: string | Record<string, unknown> | null | undefined,
): ScriptAnalysisPreview {
    const result = parseStructuredAITaskResult(
        content,
        "AI 任务返回的剧本分析结果不是有效 JSON",
    );
    if (!("characters" in result) || !("scenes" in result) || !("props" in result)) {
        throw new Error("AI 任务返回的剧本分析结果缺少必要字段");
    }
    return {
        characters: Array.isArray(result.characters) ? result.characters : [],
        scenes: Array.isArray(result.scenes) ? result.scenes : [],
        props: Array.isArray(result.props) ? result.props : [],
    };
}

async function waitForScriptAnalysisPreview(taskId: string): Promise<ScriptAnalysisPreview> {
    const deadline = Date.now() + TEXT_AI_POLL_TIMEOUT_MS;
    while (true) {
        const task = await aiTaskApi.getStatus(taskId);
        if (task.raw_status === "succeeded") {
            return parseScriptAnalysisPreview(task.result_content);
        }
        if (["failed", "cancelled", "support_review"].includes(task.raw_status)) {
            throw new Error(task.safe_error?.message || "AI 剧本分析失败，请稍后重试");
        }
        if (Date.now() >= deadline) {
            throw new Error("AI 剧本分析等待超时，可稍后在任务记录中查看结果");
        }
        await new Promise((resolve) => setTimeout(resolve, TEXT_AI_POLL_INTERVAL_MS));
    }
}

async function waitForStructuredAITaskResult(
    taskId: string,
    invalidMessage: string,
): Promise<Record<string, unknown>> {
    const deadline = Date.now() + TEXT_AI_POLL_TIMEOUT_MS;
    while (true) {
        const task = await aiTaskApi.getStatus(taskId);
        if (task.raw_status === "succeeded") {
            return parseStructuredAITaskResult(task.result_content, invalidMessage);
        }
        if (["failed", "cancelled", "support_review"].includes(task.raw_status)) {
            throw new Error(task.safe_error?.message || "AI 生成失败，请稍后重试");
        }
        if (Date.now() >= deadline) {
            throw new Error("AI 生成等待超时，可稍后在任务记录中查看结果");
        }
        await new Promise((resolve) => setTimeout(resolve, TEXT_AI_POLL_INTERVAL_MS));
    }
}

export interface DialogueAudioBatchStats {
    generated: number;
    skipped: number;
    failed: number;
    no_voice: number;
    total?: number;
}

async function waitForDialogueAudioBatch(
    taskId: string,
): Promise<{ _batch_stats: DialogueAudioBatchStats }> {
    const deadline = Date.now() + TEXT_AI_POLL_TIMEOUT_MS;
    while (true) {
        const task = await aiTaskApi.getStatus(taskId);
        if (task.raw_status === "succeeded") {
            const result = task.result_content;
            if (
                result
                && typeof result === "object"
                && "_batch_stats" in result
                && result._batch_stats
                && typeof result._batch_stats === "object"
            ) {
                return result as unknown as { _batch_stats: DialogueAudioBatchStats };
            }
            throw new Error("对白音频任务缺少批量结果统计");
        }
        if (["failed", "cancelled", "support_review"].includes(task.raw_status)) {
            throw new Error(task.safe_error?.message || "对白音频生成失败，请稍后重试");
        }
        if (Date.now() >= deadline) {
            throw new Error("对白音频生成等待超时，可稍后在任务记录中查看结果");
        }
        await new Promise((resolve) => setTimeout(resolve, TEXT_AI_POLL_INTERVAL_MS));
    }
}

type PolishPromptResponse = Record<string, unknown> & {
    prompt_cn?: string;
    prompt_en?: string;
};

async function resolvePolishPromptResponse(
    response: PolishPromptResponse,
): Promise<PolishPromptResponse> {
    const taskId = typeof response.task_id === "string" ? response.task_id : "";
    if (!taskId) return response;
    return waitForStructuredAITaskResult(
        taskId,
        "AI 任务返回的提示词润色结果不是有效 JSON",
    ) as Promise<PolishPromptResponse>;
}

// ─── Storyboard Schema v2 types ─────────────────────────────────────────────

export interface DialogueStructured {
    speaker: string;
    line: string;
    emotion?: string | null;
    delivery?: string | null;
}

export interface CameraMovementStructured {
    primary: string;
    secondary?: string | null;
    speed: string;
    description?: string | null;
}

export interface BlockingData {
    description?: string | null;
    stage?: Array<{
        ref: string;
        zone?: string | null;
        depth?: string | null;
        height?: string | null;
        facing?: string | null;
        posture?: string | null;
    }> | null;
    camera_relation?: string | null;
}

export interface AudioNoteData {
    sfx?: string | null;
    ambience?: string | null;
    bgm_note?: string | null;
}

export interface LightingData {
    direction?: string | null;
    quality?: string | null;
    color_temp?: string | null;
    description?: string | null;
}

export interface RefineSSEEvent {
    type: "frame_refine_start" | "frame_refine_complete" | "frame_refine_error" | "batch_complete";
    frame_id?: string;
    frame_index?: number;
    total?: number;
    error?: string;
}

async function withProjectVersionRetry<T>(
    projectId: string,
    mutation: () => Promise<T>,
): Promise<T> {
    const maxAttempts = 4;
    for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
        try {
            return await mutation();
        } catch (error) {
            const status = axiosFactory.isAxiosError(error)
                ? error.response?.status
                : undefined;
            if (status !== 409 || attempt === maxAttempts - 1) throw error;

            // Refreshing the document also refreshes the request interceptor's
            // cached If-Match version before this mutation is retried.
            await new Promise((resolve) => window.setTimeout(resolve, 40 * (attempt + 1)));
            await apiClient.get(`${API_URL}/projects/${projectId}`);
        }
    }
    throw new Error("内容版本重试失败");
}

export const api = {
    getEffectiveImageEngine: async (): Promise<EffectiveImageEngine> => {
        const res = await apiClient.get(`${API_URL}/ai/effective-engine`, {
            params: { capability: "image.t2i" },
        });
        return res.data;
    },

    createProject: async (title: string, text: string, skipAnalysis: boolean = false, workflowMode: string = "r2v", seriesId?: string) => {
        const res = await apiClient.post(`${API_URL}/projects`, { title, text, workflow_mode: workflowMode, series_id: seriesId }, {
            params: { skip_analysis: skipAnalysis }
        });
        return { ...res.data, originalText: res.data.original_text };
    },

    getProjects: async () => {
        const res = await apiClient.get(`${API_URL}/projects/`);
        return res.data.map((p: any) => ({ ...p, originalText: p.original_text }));
    },

    getProject: async (scriptId: string) => {
        const res = await apiClient.get(`${API_URL}/projects/${scriptId}`);
        if (!IS_CLOUD_DEPLOYMENT) {
            return { ...res.data, originalText: res.data.original_text };
        }
        const assets = await apiClient.get(`${API_URL}/projects/${scriptId}/assets`);
        return {
            ...res.data,
            ...assets.data,
            originalText: res.data.original_text,
        };
    },

    deleteProject: async (scriptId: string) => {
        const res = await apiClient.delete(`${API_URL}/projects/${scriptId}`);
        return res.data;
    },

    /** Toggle the user-starred (featured) flag on a project. Returns the
     *  updated Script. No request body — the backend flips the current flag. */
    toggleProjectStarred: async (scriptId: string) => {
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/toggle_starred`);
        return res.data;
    },

    reparseProject: async (scriptId: string, text: string) => {
        const res = await apiClient.put(`${API_URL}/projects/${scriptId}/reparse`, { text });
        return { ...res.data, originalText: res.data.original_text };
    },

    extractPreview: async (scriptId: string, text: string) => {
        const res = await apiClient.post<
            ScriptAnalysisPreview | SubmittedAITaskResponse
        >(
            `${API_URL}/projects/${scriptId}/extract_preview`,
            { text },
            // Entity extraction is a synchronous desktop LLM call. Qwen can
            // legitimately take longer than the client's global 60s timeout
            // for long scripts, so keep the progress UI alive while it runs.
            { timeout: 300_000 },
        );
        if ("task_id" in res.data) {
            return waitForScriptAnalysisPreview(res.data.task_id);
        }
        return res.data;
    },

    applyExtraction: async (
        scriptId: string,
        text: string,
        extraction: ScriptAnalysisPreview,
    ) => {
        await apiClient.put(`${API_URL}/projects/${scriptId}/extraction`, {
            text,
            characters: extraction.characters,
            scenes: extraction.scenes,
            props: extraction.props,
        });
        return api.getProject(scriptId);
    },

    /** Persist `original_text` without LLM reparse. Used for textarea
     *  blur-saves so navigation/reload doesn't drop in-progress drafts. */
    updateScriptText: async (scriptId: string, text: string) => {
        const res = await apiClient.put(`${API_URL}/projects/${scriptId}/text`, { text });
        return { ...res.data, originalText: res.data.original_text };
    },

    syncDescriptions: async (scriptId: string) => {
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/sync_descriptions`);
        return res.data;
    },

    generateAssets: async (scriptId: string) => {
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/generate_assets`);
        return res.data;
    },

    createVideoTask: async (
        id: string,
        image_url: string,
        prompt: string,
        duration: number = 5,
        seed?: number,
        resolution: string = "720p",
        generateAudio: boolean = true,
        audioUrl?: string,
        promptExtend: boolean = true,
        negativePrompt?: string,
        batchSize: number = 1,
        model: string = DEFAULT_I2V_MODEL_ID,
        frameId?: string,
        shotType: string = "single",  // 'single' or 'multi' (only for wan2.6-i2v)
        generationMode: string = "i2v",  // 'i2v' or 'r2v'
        referenceVideoUrls: string[] = [],  // Reference videos for R2V (max 3)
        // Kling params
        mode?: string,
        sound?: boolean,
        cfgScale?: number,
        // Vidu params
        viduAudio?: boolean,
        movementAmplitude?: string,
        // HappyHorse params
        referenceImageUrls: string[] = [],  // Reference images for HappyHorse R2V (1-9)
        ratio?: string,  // Aspect ratio: 16:9, 9:16, 1:1, 4:3, 3:4
        // Storyboard R2V workbench tab the user clicked Generate from.
        // Distinct from generationMode (backend dispatcher); workbench_tab
        // lets the candidates panel group takes per UI tab on refresh.
        workbenchTab?: "t2i_i2v" | "direct_r2v",
        // Watermark toggle — supported across wan / kling / vidu / pixverse /
        // happyhorse video. undefined = leave to provider default (typically
        // off); explicit boolean is user's Advanced-section choice.
        watermark?: boolean
    ) => {
        const inputMediaIds = [
            image_url,
            ...referenceVideoUrls,
            ...referenceImageUrls,
        ].map(parseMediaReference).filter((value): value is string => Boolean(value));
        const selectedI2VModel = VIDEO_I2V_MODELS.find((item) => item.id === model);
        const supportsParameter = (name: keyof NonNullable<typeof selectedI2VModel>["params"]): boolean =>
            // The server is authoritative for model routing. Preserve the
            // historical payload for catalog entries that predate the
            // parameter-support metadata, while filtering known models to
            // their declared provider contract.
            !selectedI2VModel || Boolean(selectedI2VModel.params[name]);
        // The local cloud Seedance route currently exposes only the common
        // duration/resolution/seed contract. Keep provider-specific audio and
        // watermark controls out of this PC request until that route schema is
        // versioned with those fields; otherwise the gateway rejects the task
        // before it reaches ARK.
        const cloudSupportsSeedanceAudio = model !== "seedance-2.0-i2v";
        const cloudSupportsSeedanceWatermark = model !== "seedance-2.0-i2v";
        const cloudParameters = {
            model_choice: model,
            duration,
            resolution,
            output_count: 1,
            ...(generationMode !== "r2v" && supportsParameter("ratio")
                ? { ratio: ratio || "16:9" }
                : {}),
            ...(generationMode !== "r2v" && supportsParameter("promptExtend")
                ? { prompt_extend: promptExtend }
                : {}),
            ...(supportsParameter("audio") && cloudSupportsSeedanceAudio
                ? { audio: generateAudio }
                : {}),
            ...(supportsParameter("negativePrompt") && negativePrompt
                ? { negative_prompt: negativePrompt }
                : {}),
            ...(supportsParameter("seed") && seed != null ? { seed } : {}),
            ...(supportsParameter("watermark") && cloudSupportsSeedanceWatermark && watermark != null
                ? { watermark }
                : {}),
        };
        const legacyMediaPayload = IS_CLOUD_DEPLOYMENT
            ? {}
            : {
                image_url,
                audio_url: audioUrl,
                reference_video_urls: referenceVideoUrls,
                reference_image_urls: referenceImageUrls,
            };
        const res = await apiClient.post(`${API_URL}/projects/${id}/video_tasks`, withoutCloudModelOverrides({
            ...legacyMediaPayload,
            prompt,
            duration,
            seed,
            resolution,
            generate_audio: generateAudio,
            prompt_extend: promptExtend,
            negative_prompt: negativePrompt,
            batch_size: batchSize,
            model,
            frame_id: frameId,
            shot_type: shotType,
            generation_mode: generationMode,
            // Kling
            mode,
            sound: sound != null ? (sound ? "on" : "off") : undefined,
            cfg_scale: cfgScale,
            // Vidu
            vidu_audio: viduAudio,
            movement_amplitude: movementAmplitude,
            // HappyHorse
            ratio,
            watermark,
            workbench_tab: workbenchTab,
            ...(IS_CLOUD_DEPLOYMENT ? {
                media_ids: inputMediaIds,
                parameters: cloudParameters,
            } : {}),
        }));
        return normalizeSubmittedAITaskResponse(res.data);
    },

    /** Upload an external image as a T2I首帧 candidate for an I2V flow.
     *  Backend appends to the frame's t2i_image_urls history and auto-
     *  selects the new image (it becomes the active首帧, unlocking
     *  Step 2). Returns the updated frame.
     *
     *  Validation lives on the backend:
     *   - ≤ 8 MB (413 if exceeded)
     *   - jpg/jpeg/png/webp only (415 if not)
     *  The caller does cheap front-side checks first to avoid a
     *  round-trip on obvious rejects (file type / size from the File
     *  object) and surfaces backend errors verbatim otherwise. */
    uploadT2IFrame: async (scriptId: string, frameId: string, file: File) => {
        const formData = new FormData();
        formData.append("file", file);
        const res = await apiClient.post(
            `${API_URL}/projects/${scriptId}/frames/${frameId}/upload_t2i`,
            formData,
            { headers: { "Content-Type": "multipart/form-data" } },
        );
        return res.data;
    },

    /** Persist Storyboard R2V workbench state onto a frame.
     *  Used by StoryboardR2V to write tab/T2I history/active index/
     *  batch-count whenever the user changes them. Server clamps:
     *    t2i_image_urls ≤ 10 FIFO,
     *    t2i_selected_index ∈ [0, len-1],
     *    workbench_generate_count ∈ [1, 6].
     *  Unknown tab_mode returns 400. */
    updateFrameWorkbench: async (
        scriptId: string,
        frameId: string,
        patch: {
            workbench_tab_mode?: "t2i_i2v" | "direct_r2v";
            t2i_image_urls?: string[];
            t2i_selected_index?: number;
            workbench_generate_count?: number;
        },
    ) => {
        const res = await withProjectVersionRetry(scriptId, () => apiClient.patch(
            `${API_URL}/projects/${scriptId}/frames/${frameId}/workbench`,
            patch,
        ));
        return res.data;
    },


    uploadFile: async (file: File) => {
        const formData = new FormData();
        formData.append("file", file);
        const response = await apiFetch(`${API_URL}/upload`, {
            method: "POST",
            body: formData,
        });
        if (!response.ok) throw new Error("文件上传失败");
        const uploaded = await response.json();
        if (IS_CLOUD_DEPLOYMENT && uploaded?.id) {
            const mediaReference = toMediaReference(uploaded.id);
            const accessUrl = await resolveMediaUrl(mediaReference);
            return {
                ...uploaded,
                media_id: uploaded.id,
                media_reference: mediaReference,
                path: mediaReference,
                url: mediaReference,
                access_url: accessUrl,
            };
        }
        return uploaded;
    },

    /** Lightweight liveness probe + log path. Used by the Diagnose UI
     *  on stuck tasks. 5s timeout because it's only meant to confirm
     *  the backend is alive, not to wait through a slow request. */
    healthCheck: async (): Promise<{
        ok: boolean;
        time: number;
        log_file: string;
        log_dir: string;
        studio_projects: number;
    }> => {
        const res = await apiClient.get(`${API_URL}/health`, { timeout: 5000 });
        return res.data;
    },

    /** System dependency report (ffmpeg detection + version) used by
     *  Settings → About. ffmpeg -version can be slightly slow, so a
     *  more generous timeout than healthCheck. */
    checkSystem: async (): Promise<{
        system_info?: Record<string, unknown>;
        dependencies?: {
            ffmpeg?: { available: boolean; message: string; path: string | null };
        };
        status?: string;
    }> => {
        const res = await apiClient.get(`${API_URL}/system/check`, { timeout: 10000 });
        return res.data;
    },

    /** Return last N lines of the backend log + any ERROR-flavored
     *  lines, for the Diagnose UI on stuck tasks. Backend caps at
     *  1000 lines so a runaway client can't drag the server. */
    diagnoseLogTail: async (lines: number = 200): Promise<{
        path: string;
        total_lines?: number;
        returned_lines?: number;
        lines: string[];
        errors: string[];
        missing: boolean;
    }> => {
        const res = await apiClient.get(`${API_URL}/diagnose/log_tail`, {
            params: { lines },
            timeout: 8000,
        });
        return res.data;
    },

    /** Set the user's star + label annotations on a video task. Used
     *  by Storyboard's candidates panel (shortlist + free-text note).
     *  All payload fields optional; pass clear_label=true to remove
     *  the label explicitly (label=null on its own = "don't change"). */
    annotateVideoTask: async (
        scriptId: string,
        taskId: string,
        payload: { is_starred?: boolean; label?: string | null; clear_label?: boolean },
    ) => {
        const res = await apiClient.patch(
            `${API_URL}/projects/${scriptId}/video_tasks/${taskId}/annotate`,
            payload,
        );
        return res.data;
    },

    /** Mark a video task as failed-by-cancel. Provider-side render
     *  keeps going; this just unblocks the local UI. Already-completed
     *  tasks are a 404 no-op. */
    cancelVideoTask: async (scriptId: string, taskId: string) => {
        if (IS_CLOUD_DEPLOYMENT) return aiTaskApi.cancel(taskId);
        const res = await apiClient.post(
            `${API_URL}/projects/${scriptId}/video_tasks/${taskId}/cancel`,
        );
        return res.data;
    },

    /** Continue querying an already-accepted DashScope task. This avoids
     *  creating and charging for a duplicate render after a local SSL or
     *  polling interruption. */
    resumeVideoTask: async (scriptId: string, taskId: string) => {
        const res = await apiClient.post(
            `${API_URL}/projects/${scriptId}/video_tasks/${taskId}/resume`,
        );
        return res.data;
    },

    /**
     * Upload an asset image as a new variant.
     * The uploaded image will be marked as the 'upload source' for reverse generation.
     */
    uploadAsset: async (
        scriptId: string,
        assetType: string,
        assetId: string,
        file: File,
        uploadType: string,
        description?: string
    ) => {
        const formData = new FormData();
        formData.append("file", file);

        const params = new URLSearchParams({
            upload_type: uploadType,
        });
        if (description) {
            params.append("description", description);
        }

        const response = await apiFetch(
            `${API_URL}/projects/${scriptId}/assets/${assetType}/${assetId}/upload?${params.toString()}`,
            {
                method: "POST",
                body: formData,
            }
        );

        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.detail || "Failed to upload asset");
        }

        return response.json();
    },

    generateAsset: async (scriptId: string, assetId: string, assetType: string, stylePreset: string, stylePrompt?: string, generationType: string = "all", prompt: string = "", applyStyle: boolean = true, negativePrompt: string = "", batchSize: number = 1, modelName?: string, aspectRatio?: string) => {
        const sizeByRatio: Record<string, string> = {
            "16:9": "1536x1024",
            "9:16": "1024x1536",
            "1:1": "1024x1024",
            "4:3": "1536x1024",
            "3:4": "1024x1536",
        };
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/assets/generate`, withoutCloudModelOverrides({
            asset_id: assetId,
            asset_type: assetType,
            style_preset: stylePreset,
            style_prompt: stylePrompt,
            generation_type: generationType,
            prompt: prompt,
            apply_style: applyStyle,
            negative_prompt: negativePrompt,
            batch_size: batchSize,
            model_name: modelName,
            aspect_ratio: aspectRatio,
            ...(IS_CLOUD_DEPLOYMENT ? {
                parameters: {
                    count: 1,
                    size: sizeByRatio[aspectRatio || ""] || "1024x1024",
                    quality: "high",
                    output_format: "png",
                    background: "auto",
                },
            } : {}),
        }));
        return normalizeSubmittedAITaskResponse(res.data);
    },

    getTaskStatus: async (taskId: string) => {
        if (IS_CLOUD_DEPLOYMENT) return aiTaskApi.getStatus(taskId);
        const res = await apiClient.get(`${API_URL}/tasks/${taskId}`);
        return res.data;
    },

    getVideoTaskStatus: async (scriptId: string, taskId: string) => {
        if (IS_CLOUD_DEPLOYMENT) return aiTaskApi.getStatus(taskId);
        const res = await apiClient.get(
            `${API_URL}/projects/${scriptId}/video_tasks/${taskId}`,
        );
        return res.data;
    },

    attachGeneratedVideo: async (
        scriptId: string,
        frameId: string,
        data: {
            task_id: string;
            media_id: string;
            prompt: string;
            image_url: string;
            duration: number;
            resolution: string;
            model: string;
            generation_mode: "i2v" | "r2v";
            workbench_tab?: "t2i_i2v" | "direct_r2v";
        },
    ) => {
        const res = await withProjectVersionRetry(scriptId, () => apiClient.post(
            `${API_URL}/projects/${scriptId}/frames/${frameId}/video_candidates`,
            data,
        ));
        return res.data;
    },

    generateAssetVideo: async (scriptId: string, assetType: string, assetId: string, data: { prompt?: string, duration?: number, aspect_ratio?: string }) => {
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/assets/${assetType}/${assetId}/generate_video`, data);
        return normalizeSubmittedAITaskResponse(res.data);
    },

    /**
     * Generate Motion Reference video for an asset (Character Full Body/Headshot, Scene, or Prop).
     * This is part of Asset Activation v2.
     */
    generateMotionRef: async (
        scriptId: string,
        assetId: string,
        assetType: 'full_body' | 'head_shot' | 'scene' | 'prop',
        prompt?: string,
        audioUrl?: string,
        duration: number = 5,
        batchSize: number = 1
    ): Promise<any & { _task_id?: string }> => {
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/assets/generate_motion_ref`, {
            asset_id: assetId,
            asset_type: assetType,
            prompt,
            audio_url: audioUrl,
            duration,
            batch_size: batchSize
        });
        return normalizeSubmittedAITaskResponse(res.data);
    },

    deleteAssetVideo: async (scriptId: string, assetType: string, assetId: string, videoId: string) => {
        const res = await apiClient.delete(`${API_URL}/projects/${scriptId}/assets/${assetType}/${assetId}/videos/${videoId}`);
        return res.data;
    },

    toggleAssetLock: async (scriptId: string, assetId: string, assetType: string) => {
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/assets/toggle_lock`, {
            asset_id: assetId,
            asset_type: assetType
        });
        return res.data;
    },

    toggleAssetStarred: async (scriptId: string, assetId: string, assetType: string) => {
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/assets/toggle_starred`, {
            asset_id: assetId,
            asset_type: assetType
        });
        return res.data;
    },

    toggleSeriesAssetStarred: async (seriesId: string, assetId: string, assetType: string) => {
        const res = await apiClient.post(`${API_URL}/series/${seriesId}/assets/toggle_starred`, {
            asset_id: assetId,
            asset_type: assetType
        });
        return res.data;
    },

    updateAssetImage: async (scriptId: string, assetId: string, assetType: string, imageUrl: string) => {
        const mediaId = parseMediaReference(imageUrl);
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/assets/update_image`, {
            asset_id: assetId,
            asset_type: assetType,
            ...(IS_CLOUD_DEPLOYMENT && mediaId
                ? { media_id: mediaId }
                : { image_url: imageUrl })
        });
        return res.data;
    },

    selectAssetVariant: async (scriptId: string, assetId: string, assetType: string, variantId: string, generationType?: string) => {
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/assets/variant/select`, {
            asset_id: assetId,
            asset_type: assetType,
            variant_id: variantId,
            generation_type: generationType
        });
        return res.data;
    },

    bindAssetVariantProviderId: async (
        scriptId: string,
        assetId: string,
        assetType: string,
        variantId: string,
        providerAssetId?: string,
    ) => {
        const res = await apiClient.put(`${API_URL}/projects/${scriptId}/assets/variant/provider-binding`, {
            asset_id: assetId,
            asset_type: assetType,
            variant_id: variantId,
            provider: "volcengine_ark",
            provider_asset_id: providerAssetId?.trim() || null,
        });
        return res.data;
    },

    deleteAssetVariant: async (scriptId: string, assetId: string, assetType: string, variantId: string) => {
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/assets/variant/delete`, {
            asset_id: assetId,
            asset_type: assetType,
            variant_id: variantId
        });
        return res.data;
    },

    favoriteAssetVariant: async (scriptId: string, assetId: string, assetType: string, variantId: string, isFavorited: boolean, generationType?: string) => {
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/assets/variant/favorite`, {
            asset_id: assetId,
            asset_type: assetType,
            variant_id: variantId,
            is_favorited: isFavorited,
            generation_type: generationType
        });
        return res.data;
    },

    updateModelSettings: async (
        scriptId: string,
        t2iModel?: string,
        i2iModel?: string,
        i2vModel?: string,
        characterAspectRatio?: string,
        sceneAspectRatio?: string,
        propAspectRatio?: string,
        storyboardAspectRatio?: string,
        imageModel?: string,
        r2vModel?: string,
    ) => {
        if (IS_CLOUD_DEPLOYMENT) {
            throw new Error("云端模型由平台统一配置");
        }
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/model_settings`, {
            t2i_model: t2iModel,
            i2i_model: i2iModel,
            i2v_model: i2vModel,
            r2v_model: r2vModel,
            image_model: imageModel,
            character_aspect_ratio: characterAspectRatio,
            scene_aspect_ratio: sceneAspectRatio,
            prop_aspect_ratio: propAspectRatio,
            storyboard_aspect_ratio: storyboardAspectRatio
        });
        return res.data;
    },

    getPromptConfig: async (scriptId: string) => {
        const res = await apiClient.get(`${API_URL}/projects/${scriptId}/prompt_config`);
        return res.data;
    },

    updatePromptConfig: async (scriptId: string, config: { storyboard_polish?: string; video_polish?: string; r2v_polish?: string; entity_extraction?: string; style_analysis?: string; storyboard_extraction?: string }) => {
        const res = await apiClient.put(`${API_URL}/projects/${scriptId}/prompt_config`, withoutCloudModelOverrides(config));
        return res.data;
    },

    /** Phase-2 默认 Prompt — built-in DEFAULT text for all six prompt keys
     *  (storyboard_polish / video_polish / r2v_polish / entity_extraction /
     *  style_analysis / storyboard_extraction). Settings pre-fills fields from
     *  this; on save it stores "" for any field still equal to its default
     *  (delta semantics → backend uses the built-in). */
    fetchPromptDefaults: async (): Promise<Record<string, string>> => {
        const res = await apiClient.get<Record<string, string>>(`${API_URL}/prompt_defaults`);
        return res.data;
    },

    selectVideo: async (scriptId: string, frameId: string, videoId: string) => {
        // Manual pick — sets frame.is_video_pinned=true so future
        // auto_select_latest_video calls (fired by R2V poll completion)
        // skip this frame.
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/frames/${frameId}/select_video`, {
            video_id: videoId
        });
        return res.data;
    },

    autoSelectLatestVideo: async (scriptId: string, frameId: string) => {
        // Fire-and-forget on every R2V poll completion. Backend picks the
        // latest completed task for this frame and updates frame.video_url
        // unless the user has pinned a different take.
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/frames/${frameId}/auto_select_latest_video`);
        return res.data;
    },

    unpinVideo: async (scriptId: string, frameId: string) => {
        // Clear the pin; selected_video_id and video_url stay put until
        // the next auto-select picks a newer completed task.
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/frames/${frameId}/unpin_video`);
        return res.data;
    },

    mergeVideos: async (scriptId: string) => {
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/merge`);
        return res.data;
    },

    // Art Direction APIs
    analyzeScriptForStyles: async (scriptId: string, scriptText: string) => {
        const res = await apiClient.post<{
            recommendations?: any[];
            ai_task?: SubmittedAITaskResponse;
        }>(`${API_URL}/projects/${scriptId}/art_direction/analyze`, {
            script_text: scriptText
        }, {
            // Desktop style analysis is a synchronous LLM call and can exceed
            // the client's global 60s timeout when DashScope is under load.
            // Cloud mode normally returns a submitted task immediately, so
            // this larger ceiling is harmless there as well.
            timeout: 300_000,
        });
        if (res.data.ai_task?.task_id) {
            const result = await waitForStructuredAITaskResult(
                res.data.ai_task.task_id,
                "AI 任务返回的美术风格结果不是有效 JSON",
            );
            if (!Array.isArray(result.recommendations)) {
                throw new Error("AI 任务返回的美术风格结果缺少 recommendations");
            }
            return { recommendations: result.recommendations };
        }
        return res.data;
    },

    saveArtDirection: async (scriptId: string, selectedStyleId: string, styleConfig: any, customStyles: any[] = [], aiRecommendations: any[] = []) => {
        const rawId = typeof selectedStyleId === "string" ? selectedStyleId.trim() : "";
        const configId = styleConfig && typeof styleConfig.id === "string"
            ? styleConfig.id.trim()
            : "";
        const normalizedStyleId = rawId || configId || "custom-style";
        const normalizedStyleConfig = {
            ...(styleConfig && typeof styleConfig === "object" ? styleConfig : {}),
            id: normalizedStyleId,
            is_custom: styleConfig?.is_custom ?? true,
        };
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/art_direction/save`, {
            selected_style_id: normalizedStyleId,
            style_config: normalizedStyleConfig,
            custom_styles: Array.isArray(customStyles) ? customStyles.filter(Boolean) : [],
            ai_recommendations: Array.isArray(aiRecommendations)
                ? aiRecommendations.filter((item) => item && typeof item === "object")
                : [],
        });
        return IS_CLOUD_DEPLOYMENT ? api.getProject(scriptId) : res.data;
    },

    getStylePresets: async () => {
        const res = await apiClient.get(`${API_URL}/art_direction/presets`);
        return res.data;
    },

    // NOTE: polishPrompt removed - use refineFramePrompt for storyboard prompts
    //
    // 后端契约（#117）：
    //   成功 → 200 + { prompt_cn, prompt_en }
    //   失败 → 502 + { detail: { reason, message_zh, message_en, prompt_cn?, prompt_en? } }
    //     reason ∈ is_configured_false | api_error | json_parse_error | missing_keys | model_echo
    //     model_echo 是 warning（带原文），其余是 hard error。
    //
    // prevCn（#119）：迭代时传入上一次 CN 实现双语锚点；首次留空。
    // Issue 13: image_urls + polish_model added.
    //   image_urls: I2V — pass active first frame URL (T2I selection or
    //     storyboard frame); R2V — pass reference image URLs. Empty/omit
    //     for T2I-only / no-frame shots ⇒ backend falls back to text-only.
    //   polishModel: explicit override; "" lets backend resolve from
    //     project/series PromptConfig.polish_model, then default.
    polishVideoPrompt: async (
        draftPrompt: string,
        feedback: string = "",
        scriptId: string = "",
        prevCn: string = "",
        imageUrls: string[] = [],
        polishModel: string = "",
    ) => {
        const res = await apiClient.post(`${API_URL}/video/polish_prompt`, withoutCloudModelOverrides({
            draft_prompt: draftPrompt,
            feedback: feedback,
            script_id: scriptId,
            prev_cn: prevCn,
            image_urls: imageUrls,
            polish_model: polishModel,
        }));
        return resolvePolishPromptResponse(res.data);
    },
    polishR2VPrompt: async (
        draftPrompt: string,
        slots: { description: string }[],
        feedback: string = "",
        scriptId: string = "",
        prevCn: string = "",
        imageUrls: string[] = [],
        polishModel: string = "",
    ) => {
        const res = await apiClient.post(`${API_URL}/video/polish_r2v_prompt`, withoutCloudModelOverrides({
            draft_prompt: draftPrompt,
            slots: slots,
            feedback: feedback,
            script_id: scriptId,
            prev_cn: prevCn,
            image_urls: imageUrls,
            polish_model: polishModel,
        }));
        return resolvePolishPromptResponse(res.data);
    },
    updateAssetDescription: async (scriptId: string, assetId: string, assetType: string, description: string) => {
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/assets/update_description`, {
            asset_id: assetId,
            asset_type: assetType,
            description: description
        });
        return res.data;
    },

    updateAssetAttributes: async (scriptId: string, assetId: string, assetType: string, attributes: any) => {
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/assets/update_attributes`, {
            asset_id: assetId,
            asset_type: assetType,
            attributes: attributes
        });
        return res.data;
    },

    toggleFrameLock: async (scriptId: string, frameId: string) => {
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/frames/toggle_lock`, {
            frame_id: frameId
        });
        return res.data;
    },

    updateFrame: async (scriptId: string, frameId: string, data: {
        image_prompt?: string;
        action_description?: string;
        dialogue?: string;
        camera_angle?: string;
        scene_id?: string;
        character_ids?: string[];
        duration?: number;
        shot_size?: string;
        camera_movement_description?: string;
        transition_hint?: string;
    }) => {
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/frames/update`, {
            frame_id: frameId,
            ...data
        });
        return res.data;
    },

    updateProjectStyle: async (scriptId: string, stylePreset: string, stylePrompt?: string) => {
        const res = await apiClient.patch(`${API_URL}/projects/${scriptId}/style`, {
            style_preset: stylePreset,
            style_prompt: stylePrompt
        });
        return res.data;
    },

    renderFrame: async (scriptId: string, frameId: string, compositionData: any, prompt: string, batchSize: number = 1) => {
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/storyboard/render`, {
            frame_id: frameId,
            composition_data: compositionData,
            prompt: prompt,
            batch_size: batchSize,
            ...(IS_CLOUD_DEPLOYMENT ? {
                parameters: {
                    ...normalizeCloudXlinksT2IParameters({ size: "16:9" }),
                    model_choice: "gpt-image-2",
                },
            } : {}),
        });
        return res.data;
    },

    // === STORYBOARD DRAMATIZATION v2 ===

    /**
     * Analyzes script text and generates storyboard frames using AI.
     * Replaces existing frames with newly generated ones.
     */
    analyzeToStoryboard: async (scriptId: string, text: string, entities?: {
        characters?: any[];
        scenes?: any[];
        props?: any[];
    }, maxClipSeconds: 15 | 30 = 15) => {
        const selectEntityFields = (
            items: any[] | undefined,
            fields: string[],
        ): Record<string, string | number | boolean>[] => (Array.isArray(items) ? items : [])
            .filter((item) => item && typeof item === "object")
            .map((item) => Object.fromEntries(
                fields
                    .filter((field) => ["string", "number", "boolean"].includes(typeof item[field]))
                    .map((field) => [field, item[field]]),
            ));
        const entityPayload = {
            characters: selectEntityFields(entities?.characters, [
                "id", "name", "description", "age", "gender", "clothing",
            ]),
            scenes: selectEntityFields(entities?.scenes, [
                "id", "name", "description", "time_of_day", "lighting_mood",
            ]),
            props: selectEntityFields(entities?.props, [
                "id", "name", "description",
            ]),
        };
        const res = await apiClient.post<SubmittedAITaskResponse | Record<string, any>>(
            `${API_URL}/projects/${scriptId}/storyboard/analyze`, {
            text,
            entities: entityPayload,
            max_clip_seconds: maxClipSeconds,
        });
        if ("task_id" in res.data && typeof res.data.task_id === "string") {
            const result = await waitForStructuredAITaskResult(
                res.data.task_id,
                "AI 任务返回的分镜结果不是有效 JSON",
            );
            if (!Array.isArray(result.frames) || result.frames.length === 0) {
                throw new Error("AI 任务返回的分镜结果缺少 frames");
            }
            const normalizeName = (value: unknown) => String(value || "").trim().toLowerCase();
            const characterIds = new Map<string, string>(
                entityPayload.characters.map((item) => [normalizeName(item.name), String(item.id || "")]),
            );
            const sceneIds = new Map<string, string>(
                entityPayload.scenes.map((item) => [normalizeName(item.name), String(item.id || "")]),
            );
            const propIds = new Map<string, string>(
                entityPayload.props.map((item) => [normalizeName(item.name), String(item.id || "")]),
            );
            const resolveIds = (values: unknown, lookup: Map<string, string>) =>
                (Array.isArray(values) ? values : [])
                    .map((value) => lookup.get(normalizeName(value)))
                    .filter((value): value is string => Boolean(value));
            const frames = result.frames.map((value) => {
                const frame = value && typeof value === "object"
                    ? value as Record<string, any>
                    : {};
                return {
                    scene_id: frame.scene_id || sceneIds.get(normalizeName(frame.scene_ref_name)) || "",
                    character_ids: Array.isArray(frame.character_ids)
                        ? frame.character_ids
                        : resolveIds(frame.character_ref_names, characterIds),
                    prop_ids: Array.isArray(frame.prop_ids)
                        ? frame.prop_ids
                        : resolveIds(frame.prop_ref_names, propIds),
                    action_description: frame.action_description || frame.action_summary || frame.visual_description || "",
                    visual_description: frame.visual_description || frame.action_summary || undefined,
                    shot_size: frame.shot_size || undefined,
                    camera_angle: frame.camera_angle || "平视",
                    camera_movement: typeof frame.camera_movement === "string"
                        ? frame.camera_movement
                        : frame.camera_movement?.description || undefined,
                    dialogue: frame.dialogue || undefined,
                    speaker: frame.speaker || undefined,
                    duration: Number.isFinite(Number(frame.duration)) ? Number(frame.duration) : 5,
                };
            });
            await apiClient.put(`${API_URL}/projects/${scriptId}/frames`, { frames });
            return api.getProject(scriptId);
        }
        return res.data;
    },

    /**
     * Refines a raw prompt into bilingual (CN/EN) prompts using AI.
     * Returns { prompt_cn, prompt_en, frame_updated }.
     */
    refineFramePrompt: async (scriptId: string, frameId: string, rawPrompt: string, assets: any[] = [], feedback: string = "") => {
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/storyboard/refine_prompt`, {
            frame_id: frameId,
            raw_prompt: rawPrompt,
            assets: assets,
            feedback: feedback
        });
        return res.data;
    },

    generateStoryboard: async (scriptId: string) => {
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/generate_storyboard`);
        return res.data;
    },

    getVoices: async (): Promise<VoiceMeta[]> => {
        const response = await apiFetch(`${API_URL}/voices`);
        if (!response.ok) throw new Error("Failed to fetch voices");
        return response.json();
    },

    /**
     * PR-3g #3 · Voice picker modal inline ▶ preview.
     * Backend caches by md5(voice_id|text|speed|pitch|volume|instructions);
     * first call generates, subsequent calls return cached URL instantly.
     * Returns relative URL under /files (e.g. "cache/voice_preview/abc.mp3").
     */
    previewVoice: async (params: {
        voice_id: string;
        text: string;
        speed?: number;
        pitch?: number;
        volume?: number;
        instructions?: string;
    }): Promise<{ url: string; cached: boolean }> => {
        const response = await apiFetch(`${API_URL}/voice/preview`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(withoutCloudModelOverrides({
                voice_id: params.voice_id,
                text: params.text,
                speed: params.speed ?? 1.0,
                pitch: params.pitch ?? 1.0,
                volume: params.volume ?? 50,
                instructions: params.instructions ?? null,
            })),
        });
        if (!response.ok) {
            const detail = await response.text();
            throw new Error(`Voice preview failed: ${response.status} ${detail}`);
        }
        return response.json();
    },

    /**
     * PR-3h · Clone a voice from a reference audio URL.
     * Frontend flow:
     *   1. Upload audio file via /upload → receive URL
     *   2. Call cloneVoice({series_id, audio_url, label}) → CustomVoice
     *   3. Picker modal 我的复刻 tab refreshes via listCustomVoices()
     * Audio requirements: ≤10MB, MP3/WAV/M4A, ≥16kHz, 10-20s recommended.
     */
    cloneVoice: async (params: {
        series_id: string;
        audio_url: string;
        label: string;
        target_model?: string;
    }): Promise<CustomVoice> => {
        const response = await apiFetch(`${API_URL}/voice/clone`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(withoutCloudModelOverrides({
                series_id: params.series_id,
                audio_url: params.audio_url,
                label: params.label,
                target_model: params.target_model ?? "cosyvoice-v3.5-plus",
            })),
        });
        if (!response.ok) {
            const detail = await response.text();
            throw new Error(`Voice clone failed: ${response.status} ${detail}`);
        }
        return response.json();
    },

    /** PR-3h · List custom voices (clones + designs) on a series. */
    listCustomVoices: async (seriesId: string): Promise<CustomVoice[]> => {
        const response = await apiFetch(`${API_URL}/series/${seriesId}/custom_voices`);
        if (!response.ok) throw new Error("Failed to list custom voices");
        return response.json();
    },

    /** PR-3h · Remove a custom voice. Does NOT delete on dashscope side. */
    deleteCustomVoice: async (seriesId: string, voiceId: string): Promise<{ removed: boolean }> => {
        const response = await apiFetch(`${API_URL}/series/${seriesId}/custom_voices/${voiceId}`, {
            method: "DELETE",
        });
        if (!response.ok) throw new Error("Failed to delete custom voice");
        return response.json();
    },

    /**
     * PR-3i · Voice design — mint a new voice from a text prompt + return preview.
     * Iterative: re-call with tweaked voice_prompt; only persist via designVoiceAccept.
     */
    designVoicePreview: async (params: {
        voice_prompt: string;
        preview_text?: string;
        target_model?: string;
    }): Promise<{ voice_id: string; preview_url: string; target_model: string }> => {
        const response = await apiFetch(`${API_URL}/voice/design/preview`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(withoutCloudModelOverrides({
                voice_prompt: params.voice_prompt,
                preview_text: params.preview_text ?? "你好，这是一段音色测试。请仔细听一听是否符合预期。",
                target_model: params.target_model ?? "cosyvoice-v3.5-plus",
            })),
        });
        if (!response.ok) {
            const detail = await response.text();
            throw new Error(`Voice design preview failed: ${response.status} ${detail}`);
        }
        return response.json();
    },

    /** PR-3i · Commit a previewed design voice to series.custom_voices[]. */
    designVoiceAccept: async (params: {
        series_id: string;
        voice_id: string;
        voice_prompt: string;
        label: string;
        target_model?: string;
    }): Promise<CustomVoice> => {
        const response = await apiFetch(`${API_URL}/voice/design/accept`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(withoutCloudModelOverrides({
                series_id: params.series_id,
                voice_id: params.voice_id,
                voice_prompt: params.voice_prompt,
                label: params.label,
                target_model: params.target_model ?? "cosyvoice-v3.5-plus",
            })),
        });
        if (!response.ok) {
            const detail = await response.text();
            throw new Error(`Voice design accept failed: ${response.status} ${detail}`);
        }
        return response.json();
    },

    /** PR-3i · LLM helper — translate character.description → CosyVoice voice_prompt. */
    translateVoicePrompt: async (description: string): Promise<{ voice_prompt: string }> => {
        const response = await apiFetch(`${API_URL}/voice/design/translate`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ description }),
        });
        if (!response.ok) {
            const detail = await response.text();
            throw new Error(`Voice prompt translate failed: ${response.status} ${detail}`);
        }
        return response.json();
    },

    bindVoice: async (scriptId: string, charId: string, voiceId: string, voiceName: string) => {
        const response = await apiFetch(`${API_URL}/projects/${scriptId}/characters/${charId}/voice`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ voice_id: voiceId, voice_name: voiceName }),
        });
        if (!response.ok) throw new Error("Failed to bind voice");
        return response.json();
    },

    generateAudio: async (scriptId: string) => {
        const response = await apiFetch(`${API_URL}/projects/${scriptId}/generate_audio`, {
            method: "POST",
        });
        if (!response.ok) throw new Error("Failed to generate audio");
        return response.json();
    },

    generateLineAudio: async (
        scriptId: string,
        frameId: string,
        speed: number,
        pitch: number,
        volume: number = 50,
        instructions?: string,
    ) => {
        const response = await apiFetch(`${API_URL}/projects/${scriptId}/frames/${frameId}/audio`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ speed, pitch, volume, instructions: instructions || null }),
        });
        if (!response.ok) throw new Error("Failed to generate line audio");
        return response.json();
    },

    /** PR-3j · Generate dialogue audio for every frame with dialogue.
     *  Skips frames whose snapshot hash still matches. */
    generateDialogueAudioBatch: async (scriptId: string): Promise<{ _batch_stats: DialogueAudioBatchStats }> => {
        const response = await apiFetch(`${API_URL}/projects/${scriptId}/dialogue_audio/batch`, {
            method: "POST",
        });
        if (!response.ok) throw new Error("Failed to generate dialogue audio batch");
        const result = await response.json() as
            | { _batch_stats: DialogueAudioBatchStats }
            | SubmittedAITaskResponse;
        if ("task_id" in result) {
            return waitForDialogueAudioBatch(result.task_id);
        }
        return result;
    },

    previewDub: async (scriptId: string, frameId: string, videoTaskId: string, offsetMs: number = 0) => {
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/frames/${frameId}/dub/preview`, {
            video_task_id: videoTaskId,
            offset_ms: offsetMs,
        }, { timeout: 120000 });
        return res.data;
    },

    applyDub: async (scriptId: string, frameId: string) => {
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/frames/${frameId}/dub/apply`);
        return res.data;
    },

    revertDub: async (scriptId: string, frameId: string) => {
        const res = await apiClient.delete(`${API_URL}/projects/${scriptId}/frames/${frameId}/dub`);
        return res.data;
    },

    /** Schema v2 · Refine a single frame (Phase 2 rich fields). */
    refineSingleFrame: async (scriptId: string, frameId: string) => {
        const response = await apiFetch(`${API_URL}/projects/${scriptId}/frames/${frameId}/refine`, {
            method: "POST",
        });
        if (!response.ok) throw new Error("Failed to refine frame");
        return response.json();
    },

    /** Schema v2 · Batch refine all frames via SSE stream. */
    refineBatchFrames: async (
        scriptId: string,
        onEvent: (event: RefineSSEEvent) => void,
    ): Promise<void> => {
        const response = await apiFetch(`${API_URL}/projects/${scriptId}/storyboard/refine_batch`, {
            method: "POST",
        });
        if (!response.ok) throw new Error("Failed to start batch refine");
        const reader = response.body?.getReader();
        if (!reader) return;
        const decoder = new TextDecoder();
        let buffer = "";
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split("\n");
            buffer = lines.pop() || "";
            let currentEventType = "";
            for (const line of lines) {
                if (line.startsWith("event: ")) {
                    currentEventType = line.slice(7).trim();
                } else if (line.startsWith("data: ")) {
                    try {
                        const data = JSON.parse(line.slice(6));
                        onEvent({ type: currentEventType as RefineSSEEvent["type"], ...data });
                    } catch { /* skip malformed lines */ }
                }
            }
        }
    },

    /** PR-3k · BGM preset catalog for Assembly Mix phase. */
    listBgmPresets: async (): Promise<BgmPreset[]> => {
        const response = await apiFetch(`${API_URL}/bgm/presets`);
        if (!response.ok) throw new Error("Failed to list bgm presets");
        return response.json();
    },

    /** PR-3k · Update audio mix (BGM url + per-track volumes). */
    updateAudioMix: async (scriptId: string, payload: {
        bgm_url?: string | null;
        dialogue_volume?: number;
        bgm_volume?: number;
        sfx_volume?: number;
    }) => {
        const response = await apiFetch(`${API_URL}/projects/${scriptId}/audio_mix`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        if (!response.ok) throw new Error("Failed to update audio mix");
        return response.json();
    },

    updateVoiceParams: async (scriptId: string, charId: string, speed: number, pitch: number, volume: number) => {
        const response = await apiFetch(`${API_URL}/projects/${scriptId}/characters/${charId}/voice_params`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ speed, pitch, volume }),
        });
        if (!response.ok) throw new Error("Failed to update voice params");
        return response.json();
    },

    exportProject: async (scriptId: string, options: any) => {
        const response = await apiFetch(`${API_URL}/projects/${scriptId}/export`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(options),
        });
        if (!response.ok) throw new Error("Failed to export project");
        return response.json();
    },

    generateVideo: async (scriptId: string) => {
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/generate_video`);
        return res.data;
    },

    getEnvConfig: async (): Promise<EnvConfigPayload> => {
        const res = await apiClient.get<EnvConfigPayload>(`${API_URL}/config/env`);
        return res.data;
    },

    saveEnvConfig: async (config: EnvConfigPayload) => {
        const res = await apiClient.post(`${API_URL}/config/env`, config, {
            timeout: 60000, // 60 seconds timeout
        });
        return res.data;
    },

    triggerMulerunLogin: async () => {
        const res = await apiClient.post(`${API_URL}/config/mulerun-login`);
        return res.data;
    },

    extractLastFrame: async (scriptId: string, frameId: string, videoTaskId: string) => {
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/frames/${frameId}/extract_last_frame`, {
            video_task_id: videoTaskId,
        });
        return res.data;
    },

    uploadFrameImage: async (scriptId: string, frameId: string, file: File) => {
        const formData = new FormData();
        formData.append("file", file);
        const response = await apiFetch(
            `${API_URL}/projects/${scriptId}/frames/${frameId}/upload_image`,
            { method: "POST", body: formData }
        );
        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.detail || "Failed to upload frame image");
        }
        return response.json();
    },

    // ============================================
    // Series APIs
    // ============================================

    // Series CRUD
    createSeriesV2: async (
        title: string,
        opts: { description?: string; workflow_mode?: string; content_mode?: "scripted" | "freeform"; default_generation_mode?: "r2v" | "i2v" } = {},
    ) => {
        const response = await apiClient.post(`${API_URL}/series`, {
            title,
            description: opts.description ?? "",
            workflow_mode: opts.workflow_mode ?? "r2v",
            content_mode: opts.content_mode ?? "scripted",
            default_generation_mode: opts.default_generation_mode ?? "r2v",
        });
        return response.data;
    },

    createSeries: async (title: string, description?: string, workflowMode?: string) => {
        const response = await apiClient.post(`${API_URL}/series`, { title, description, workflow_mode: workflowMode || "r2v" });
        return response.data;
    },
    listSeries: async () => {
        const response = await apiClient.get(`${API_URL}/series`);
        return response.data;
    },
    /** Core 全局/共享资产池（跨系列/项目聚合）。后端：GET /library/assets → {characters, scenes, props}。 */
    listLibraryAssets: async () => {
        const res = await apiClient.get(`${API_URL}/library/assets`);
        return res.data;
    },
    /** 新建一条全局/共享资产。后端：POST /library/assets。
     *  assetType 为单数（"character"|"scene"|"prop"）。data 可含 name/description/persona/image_url/voice_id。 */
    createLibraryAsset: async (
        assetType: string,
        data: { name: string; description?: string; persona?: string; image_url?: string; voice_id?: string },
    ) => {
        const mediaId = parseMediaReference(data.image_url);
        const payload = { ...data };
        if (IS_CLOUD_DEPLOYMENT && mediaId) delete payload.image_url;
        const res = await apiClient.post(`${API_URL}/library/assets`, {
            asset_type: assetType,
            ...payload,
            ...(IS_CLOUD_DEPLOYMENT && mediaId ? { media_id: mediaId } : {}),
        });
        return res.data;
    },
    /** 上传一张本地图片到全局资产库，返回可被前端加载的 image_url。
     *  后端契约：POST /library/assets/upload，multipart 字段名 "file" → { image_url }。
     *  调用方拿到 image_url 后传给 createLibraryAsset。 */
    uploadLibraryImage: async (file: File): Promise<{ image_url: string; media_id?: string }> => {
        const formData = new FormData();
        formData.append("file", file);
        const res = await apiClient.post<MediaUploadResponse | { image_url: string }>(`${API_URL}/library/assets/upload`, formData, {
            headers: { "Content-Type": "multipart/form-data" },
        });
        if ("id" in res.data) {
            const imageUrl = toMediaReference(res.data.id);
            await resolveMediaUrl(imageUrl);
            return { image_url: imageUrl, media_id: res.data.id };
        }
        return res.data;
    },
    /** 补丁更新全局资产（仅发送的字段生效，PATCH 语义）。后端：PUT /library/assets/{type}/{id}。assetType 单数。 */
    updateLibraryAsset: async (
        assetType: string,
        assetId: string,
        patch: {
            name?: string;
            description?: string;
            persona?: string;
            image_url?: string;
            voice_id?: string;
            starred?: boolean;
            locked?: boolean;
            visual_weight?: number;
        },
    ) => {
        const res = await apiClient.put(`${API_URL}/library/assets/${assetType}/${assetId}`, patch);
        return res.data;
    },
    /** 把项目/系列来源资产 deep-copy 提升进全局共享池。后端：POST /library/assets/promote。
     *  sourceKind: "project"|"series"；assetType 单数。 */
    promoteAssetToLibrary: async (
        sourceKind: "project" | "series",
        sourceId: string,
        assetType: string,
        assetId: string,
    ) => {
        const res = await apiClient.post(`${API_URL}/library/assets/promote`, {
            source_kind: sourceKind,
            source_id: sourceId,
            asset_type: assetType,
            asset_id: assetId,
        });
        return res.data;
    },
    getSeries: async (seriesId: string) => {
        const response = await apiClient.get(`${API_URL}/series/${seriesId}`);
        if (!IS_CLOUD_DEPLOYMENT) return response.data;
        const assets = await apiClient.get(`${API_URL}/series/${seriesId}/assets`);
        return { ...response.data, ...assets.data };
    },
    updateSeries: async (
        seriesId: string,
        data: { title?: string; description?: string; art_direction?: any },
    ) => {
        const response = await apiClient.put(`${API_URL}/series/${seriesId}`, data);
        return response.data;
    },

    /** R2V v2 Phase 3 — fetch previous episode raw snippet + AI summary cache state.
     *  P2-a extended response with last_frames for Storyboard cross-step rail. */
    getPreviousEpisodeSummary: async (scriptId: string): Promise<{
        has_previous: boolean;
        previous_episode_id: string | null;
        previous_episode_title: string | null;
        raw_snippet: string;
        ai_summary: string | null;
        ai_summary_stale: boolean;
        version?: number;
        last_frames?: Array<{
            id: string;
            action_description: string;
            thumbnail_url: string | null;
            video_url: string | null;
        }>;
    }> => {
        const res = await apiClient.get(`${API_URL}/projects/${scriptId}/previous_episode`);
        return res.data;
    },

    /** On-demand generate AI summary of previous episode (user-triggered). */
    generatePreviousEpisodeSummary: async (scriptId: string): Promise<{
        ai_summary: string;
        ai_summary_stale: boolean;
        previous_episode_id: string;
        previous_episode_title: string;
        version?: number;
    }> => {
        if (IS_CLOUD_DEPLOYMENT) {
            const submitted = await apiClient.post<SubmittedAITaskResponse & {
                version: number;
                previous_episode_version: number;
            }>(
                `${API_URL}/projects/${scriptId}/previous_episode/summary`,
                undefined,
                {
                    headers: {
                        "Idempotency-Key": createIdempotencyKey("previous-summary"),
                    },
                },
            );
            const aiSummary = await waitForTextAITaskResult(submitted.data.task_id);
            const saved = await apiClient.put(
                `${API_URL}/projects/${scriptId}/last_episode_summary`,
                {
                    ai_summary: aiSummary,
                    source_previous_version: submitted.data.previous_episode_version,
                },
                { headers: { "If-Match": String(submitted.data.version) } },
            );
            return saved.data;
        }
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/previous_episode/summary`);
        return res.data;
    },

    /** R2V v2 Phase 4 — fetch reconcile suggestions for this episode's
     *  extracted entities vs the parent series's shared library. */
    getReconcileSuggestions: async (scriptId: string): Promise<{
        characters: ReconcileSuggestion[];
        scenes: ReconcileSuggestion[];
        props: ReconcileSuggestion[];
    }> => {
        const res = await apiClient.get(`${API_URL}/projects/${scriptId}/reconcile/suggestions`);
        return res.data;
    },

    /** Apply user-confirmed reconcile decisions. */
    applyReconcile: async (
        scriptId: string,
        decisions: {
            characters?: ReconcileAction[];
            scenes?: ReconcileAction[];
            props?: ReconcileAction[];
        },
    ) => {
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/reconcile/apply`, decisions);
        return res.data;
    },

    /** R2V v2 Phase 5 — series-scope quick-create CRUD for Cast modal. */
    createSeriesAsset: async (
        seriesId: string,
        kind: "characters" | "scenes" | "props",
        data: { name: string; description?: string; persona?: string; image_url?: string; voice_id?: string },
    ) => {
        const mediaId = parseMediaReference(data.image_url);
        const payload = { ...data };
        if (IS_CLOUD_DEPLOYMENT && mediaId) delete payload.image_url;
        const res = await apiClient.post(`${API_URL}/series/${seriesId}/${kind}`, {
            ...payload,
            ...(IS_CLOUD_DEPLOYMENT && mediaId ? { media_id: mediaId } : {}),
        });
        return res.data;
    },

    /** R2V v2 Phase 2 — clear project-level art_direction (return to series inherit). */
    clearProjectArtDirection: async (scriptId: string) => {
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/art_direction/clear`);
        return res.data;
    },

    /** R2V v2 P2-b — next-episode hook prediction state. */
    getNextEpisodeHook: async (scriptId: string): Promise<{
        has_text: boolean;
        hook: string | null;
        stale: boolean;
    }> => {
        const res = await apiClient.get(`${API_URL}/projects/${scriptId}/next_hook`);
        return res.data;
    },

    /** Generate hook prediction (user-triggered). */
    generateNextEpisodeHook: async (scriptId: string): Promise<{
        hook: string;
        stale: boolean;
        version?: number;
    }> => {
        if (IS_CLOUD_DEPLOYMENT) {
            const submitted = await apiClient.post<SubmittedAITaskResponse & {
                version: number;
            }>(
                `${API_URL}/projects/${scriptId}/next_hook`,
                undefined,
                {
                    headers: {
                        "Idempotency-Key": createIdempotencyKey("next-hook"),
                    },
                },
            );
            const hook = await waitForTextAITaskResult(submitted.data.task_id);
            const saved = await apiClient.put(
                `${API_URL}/projects/${scriptId}/next_hook`,
                { hook },
                { headers: { "If-Match": String(submitted.data.version) } },
            );
            return saved.data;
        }
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/next_hook`);
        return res.data;
    },

    /** Manually edit / clear hook cache. */
    updateNextEpisodeHook: async (scriptId: string, hook: string | null) => {
        const res = await apiClient.put(`${API_URL}/projects/${scriptId}/next_hook`, { hook });
        return res.data;
    },

    /** R2V v2 P1-c — cross-episode character appearances (for @ helper). */
    getCharacterAppearances: async (seriesId: string, characterId: string): Promise<{
        character: { id: string; name: string; persona: string; description: string };
        appearances: Array<{ episode_id: string; episode_number: number | null; episode_title: string; frame_count: number }>;
        total_frames: number;
    }> => {
        const res = await apiClient.get(`${API_URL}/series/${seriesId}/characters/${characterId}/appearances`);
        return res.data;
    },

    /** R2V v2 P1-b — manually edit / clear last_episode_summary cache. */
    updateLastEpisodeSummary: async (scriptId: string, aiSummary: string | null) => {
        const res = await apiClient.put(`${API_URL}/projects/${scriptId}/last_episode_summary`, {
            ai_summary: aiSummary,
        });
        return res.data;
    },
    deleteSeries: async (seriesId: string) => {
        const response = await apiClient.delete(`${API_URL}/series/${seriesId}`);
        return response.data;
    },

    // Series Episodes
    getSeriesEpisodes: async (seriesId: string) => {
        const response = await apiClient.get(`${API_URL}/series/${seriesId}/episodes`);
        return response.data;
    },
    addEpisodeToSeries: async (seriesId: string, scriptId: string, episodeNumber?: number) => {
        const response = await apiClient.post(`${API_URL}/series/${seriesId}/episodes`, { script_id: scriptId, episode_number: episodeNumber });
        return response.data;
    },
    removeEpisodeFromSeries: async (seriesId: string, scriptId: string) => {
        const response = await apiClient.delete(`${API_URL}/series/${seriesId}/episodes/${scriptId}`);
        return response.data;
    },

    // Series Assets
    getSeriesAssets: async (seriesId: string) => {
        const response = await apiClient.get(`${API_URL}/series/${seriesId}/assets`);
        return response.data;
    },
    importSeriesAssets: async (seriesId: string, sourceSeriesId: string, assetIds: string[]) => {
        const response = await apiClient.post(`${API_URL}/series/${seriesId}/assets/import`, { source_series_id: sourceSeriesId, asset_ids: assetIds });
        return response.data;
    },

    // Series Prompt Config
    getSeriesPromptConfig: async (seriesId: string) => {
        const response = await apiClient.get(`${API_URL}/series/${seriesId}/prompt_config`);
        return response.data;
    },
    updateSeriesPromptConfig: async (seriesId: string, config: { storyboard_polish?: string; video_polish?: string; r2v_polish?: string; storyboard_extraction?: string }) => {
        const response = await apiClient.put(`${API_URL}/series/${seriesId}/prompt_config`, withoutCloudModelOverrides(config));
        return response.data;
    },
    getSeriesModelSettings: async (seriesId: string) => {
        if (IS_CLOUD_DEPLOYMENT) {
            throw new Error("云端模型由平台统一配置");
        }
        const response = await apiClient.get(`${API_URL}/series/${seriesId}/model_settings`);
        return response.data;
    },
    updateSeriesModelSettings: async (seriesId: string, settings: {
        t2i_model?: string;
        i2i_model?: string;
        image_model?: string;
        i2v_model?: string;
        character_aspect_ratio?: string;
        scene_aspect_ratio?: string;
        prop_aspect_ratio?: string;
        storyboard_aspect_ratio?: string;
    }) => {
        if (IS_CLOUD_DEPLOYMENT) {
            throw new Error("云端模型由平台统一配置");
        }
        const response = await apiClient.put(`${API_URL}/series/${seriesId}/model_settings`, settings);
        return response.data;
    },

    // Helper: create a project and add it as an episode to a series
    createEpisodeForSeries: async (seriesId: string, title: string, episodeNumber: number, workflowMode: string = "r2v") => {
        const project = await api.createProject(title, "", true, workflowMode);
        await api.addEpisodeToSeries(seriesId, project.id, episodeNumber);
        const refreshed = await api.getProject(project.id);
        return refreshed;
    },

    // File Import
    importFilePreview: async (file: File, suggestedEpisodes: number = 3) => {
        const formData = new FormData();
        formData.append('file', file);
        const response = await apiClient.post(`${API_URL}/series/import/preview?suggested_episodes=${suggestedEpisodes}`, formData, {
            headers: { 'Content-Type': 'multipart/form-data' },
            // Multi-episode planning uses an LLM and can legitimately exceed
            // the global 60s timeout when the provider is under load.
            timeout: 300_000,
        });
        return response.data;
    },
    importFileConfirm: async (data: { title: string; description?: string; import_id: string; episodes: any[] }) => {
        const response = await apiClient.post(`${API_URL}/series/import/confirm`, data);
        return response.data;
    },

    /** Generic short-drama Agent endpoints. */
    createShortDramaAgentRun: async (
        projectId: string,
        payload: {
            shots: Array<Record<string, unknown>>;
            profile?: string;
            generation_mode?: "t2v" | "i2v" | "r2v";
            idempotency_key?: string;
            skill?: string;
            skill_alias?: string;
            input_summary?: Record<string, unknown>;
        },
    ): Promise<AgentRunResponse> => {
        const idempotencyKey = payload.idempotency_key || createIdempotencyKey("agent");
        const response = await apiClient.post<AgentRunResponse>(
            `${API_URL}/projects/${projectId}/agent-runs`,
            { ...payload, idempotency_key: idempotencyKey },
            { headers: { "Idempotency-Key": idempotencyKey } },
        );
        return response.data;
    },

    listShortDramaAgentProfiles: async (): Promise<AgentProfileResponse[]> =>
        (await apiClient.get<AgentProfileResponse[]>(`${API_URL}/agent/profiles`)).data,

    getShortDramaAgentRun: async (projectId: string, runId: string): Promise<AgentRunResponse> =>
        (await apiClient.get<AgentRunResponse>(`${API_URL}/projects/${projectId}/agent-runs/${runId}`)).data,

    getShortDramaProductionPackage: async (projectId: string, runId: string) =>
        (await apiClient.get(`${API_URL}/projects/${projectId}/agent-runs/${runId}/production-package`)).data,

    approveShortDramaAgentRun: async (projectId: string, runId: string, reason?: string) =>
        (await apiClient.post<AgentRunResponse>(`${API_URL}/projects/${projectId}/agent-runs/${runId}/approve`, { reason })).data,

    rejectShortDramaAgentRun: async (projectId: string, runId: string, reason: string) =>
        (await apiClient.post<AgentRunResponse>(`${API_URL}/projects/${projectId}/agent-runs/${runId}/reject`, { reason })).data,

    resumeShortDramaAgentRun: async (projectId: string, runId: string) =>
        (await apiClient.post<AgentRunResponse>(`${API_URL}/projects/${projectId}/agent-runs/${runId}/resume`)).data,

    cancelShortDramaAgentRun: async (projectId: string, runId: string) =>
        (await apiClient.post<AgentRunResponse>(`${API_URL}/projects/${projectId}/agent-runs/${runId}/cancel`)).data,
};

// ============================================
// CRUD APIs for Assets and Frames
// ============================================

export const crudApi = {
    // Character CRUD
    createCharacter: async (scriptId: string, data: {
        name: string;
        description?: string;
        age?: string;
        gender?: string;
        clothing?: string;
    }) => {
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/characters`, data);
        return res.data;
    },

    deleteCharacter: async (scriptId: string, characterId: string) => {
        const res = await apiClient.delete(`${API_URL}/projects/${scriptId}/characters/${characterId}`);
        return res.data;
    },

    // Scene CRUD
    createScene: async (scriptId: string, data: {
        name: string;
        description?: string;
        time_of_day?: string;
        lighting_mood?: string;
    }) => {
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/scenes`, data);
        return res.data;
    },

    deleteScene: async (scriptId: string, sceneId: string) => {
        const res = await apiClient.delete(`${API_URL}/projects/${scriptId}/scenes/${sceneId}`);
        return res.data;
    },

    // Prop CRUD
    createProp: async (scriptId: string, data: {
        name: string;
        description?: string;
    }) => {
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/props`, data);
        return res.data;
    },

    deleteProp: async (scriptId: string, propId: string) => {
        const res = await apiClient.delete(`${API_URL}/projects/${scriptId}/props/${propId}`);
        return res.data;
    },

    // Frame CRUD
    createFrame: async (scriptId: string, data: {
        scene_id: string;
        action_description: string;
        character_ids?: string[];
        prop_ids?: string[];
        dialogue?: string;
        speaker?: string;
        camera_angle?: string;
        insert_at?: number;
    }) => {
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/frames`, data);
        return res.data;
    },

    deleteFrame: async (scriptId: string, frameId: string) => {
        const res = await apiClient.delete(`${API_URL}/projects/${scriptId}/frames/${frameId}`);
        return res.data;
    },

    copyFrame: async (scriptId: string, frameId: string, insertAt?: number) => {
        const res = await apiClient.post(`${API_URL}/projects/${scriptId}/frames/copy`, {
            frame_id: frameId,
            insert_at: insertAt
        });
        return res.data;
    },

    reorderFrames: async (scriptId: string, frameIds: string[]) => {
        const res = await apiClient.put(`${API_URL}/projects/${scriptId}/frames/reorder`, {
            frame_ids: frameIds
        });
        return res.data;
    }
};

// ─── Playground API ─────────────────────────────────────────────────────────

export interface CanvasPromptInputRequest {
  node_id: string;
  title: string;
  content: string;
}

export interface CanvasPromptCompositionResponse {
  prompt_cn: string;
  prompt_en: string;
}

export const canvasApi = {
  composePrompt: async (data: {
    inputs: CanvasPromptInputRequest[];
    target: "image" | "video";
    instruction?: string;
  }): Promise<CanvasPromptCompositionResponse> => {
    const response = await apiClient.post<CanvasPromptCompositionResponse>(
      `${API_URL}/canvas/compose_prompt`,
      data,
    );
    return response.data;
  },
};

export interface PlaygroundGenerateRequest {
  mode: string;
  model_id?: string;
  prompt: string;
  negative_prompt?: string;
  input_media?: string[];
  parameters?: Record<string, unknown>;
  batch_size?: number;
  idempotency_key?: string;
}

export interface PlaygroundOutputResponse {
  id: string;
  media_id?: string;
  media_path?: string;
  media_reference: string;
  media_url?: string;
  media_type: string;
  thumbnail_path?: string;
  thumbnail_media_id?: string;
  saved_asset_id?: string | null;
  saved_to_library: boolean;
}

export interface PlaygroundGenerationResponse {
  id: string;
  mode: string;
  model_id?: string;
  actual_model_name?: string;
  actual_model_id?: string;
  prompt: string;
  negative_prompt?: string;
  input_media: string[];
  input_media_ids?: string[];
  parameters: Record<string, unknown>;
  batch_size: number;
  outputs: PlaygroundOutputResponse[];
  status: string;
  raw_status?: string;
  status_zh?: string;
  cancellation_requested?: boolean;
  support_review?: boolean;
  support_review_reason?: string | null;
  error_code?: string;
  error?: string;
  provider_name?: string | null;
  provider_task_id?: string | null;
  provider_request_id?: string | null;
  created_at: string;
  updated_at?: string;
  quoted_microtickets?: string;
  quoted_tickets?: string;
  tokens_per_ticket?: string;
}

export interface PlaygroundTemplateResponse {
  id: string;
  name: string;
  category: string;
  prompt: string;
  negative_prompt?: string;
  default_mode?: string;
  default_model_id?: string;
  default_parameters: Record<string, unknown>;
  version: number;
  created_at: string;
  updated_at: string;
}

interface SubmittedPlaygroundTaskResponse {
  task_id: string;
  status: string;
  quoted_microtickets: string;
  quoted_tickets: string;
  tokens_per_ticket: string;
  actual_model?: { display_name?: string; model_id?: string };
}

function normalizePlaygroundStatus(status: string): string {
  if (["reserved", "queued"].includes(status)) return "pending";
  if (["running", "provider_succeeded"].includes(status)) return "processing";
  if (status === "succeeded") return "completed";
  if (["cancelled", "support_review"].includes(status)) return "failed";
  return status;
}

async function normalizePlaygroundGeneration(
  source: Omit<PlaygroundGenerationResponse, "outputs" | "input_media"> & {
    outputs?: Array<Partial<PlaygroundOutputResponse> & { id: string; media_type: string }>;
    input_media?: string[];
    input_media_ids?: string[];
  },
): Promise<PlaygroundGenerationResponse> {
  const inputMedia = source.input_media ??
    (source.input_media_ids || []).map(toMediaReference);
  const outputs = await Promise.all(
    (source.outputs || []).map(async (output): Promise<PlaygroundOutputResponse> => {
      const mediaReference = output.media_id
        ? toMediaReference(output.media_id)
        : output.media_reference || output.media_path || "";
      const mediaUrl = parseMediaReference(mediaReference)
        ? await resolveMediaUrl(mediaReference).catch(() => undefined)
        : undefined;
      return {
        id: output.id,
        media_id: output.media_id,
        media_path: output.media_path,
        media_reference: mediaReference,
        media_url: mediaUrl,
        media_type: output.media_type,
        thumbnail_path: output.thumbnail_path,
        thumbnail_media_id: output.thumbnail_media_id,
        saved_asset_id: output.saved_asset_id,
        saved_to_library:
          output.saved_to_library ?? Boolean(output.saved_asset_id),
      };
    }),
  );
  return {
    ...source,
    model_id: source.model_id || source.actual_model_id,
    input_media: inputMedia,
    parameters: source.parameters || {},
    batch_size: source.batch_size || 1,
    outputs,
    status: normalizePlaygroundStatus(source.status),
    raw_status: source.raw_status || source.status,
  };
}

function requireCloudMediaIds(references: string[]): string[] {
  const mediaIds = references.map(parseMediaReference);
  if (mediaIds.some((mediaId) => mediaId === null)) {
    throw new APIRequestError("云端创作只允许使用已上传的媒体", 422, "AI_MEDIA_ID_REQUIRED");
  }
  return mediaIds as string[];
}

export const playgroundApi = {
  generate: async (data: PlaygroundGenerateRequest) => {
    const payload = IS_CLOUD_DEPLOYMENT
      ? {
          mode: data.mode,
          prompt: data.prompt,
          negative_prompt: data.negative_prompt,
          media_ids: requireCloudMediaIds(data.input_media || []),
          parameters: data.mode === "t2i"
            ? normalizeCloudXlinksT2IParameters(data.parameters)
            : data.parameters,
          batch_size: data.mode === "t2i" ? 1 : data.batch_size,
          idempotency_key: data.idempotency_key || createIdempotencyKey("playground"),
        }
      : data;
    const response = await apiClient.post<
      PlaygroundGenerationResponse | SubmittedPlaygroundTaskResponse
    >(API_URL + "/playground/generate", withoutCloudModelOverrides(payload));
    if ("task_id" in response.data) {
      const submitted = response.data;
      return normalizePlaygroundGeneration({
        id: submitted.task_id,
        mode: data.mode,
        model_id: submitted.actual_model?.model_id,
        actual_model_name: submitted.actual_model?.display_name,
        actual_model_id: submitted.actual_model?.model_id,
        prompt: data.prompt,
        negative_prompt: data.negative_prompt,
        input_media_ids: requireCloudMediaIds(data.input_media || []),
        parameters: data.parameters || {},
        batch_size: data.batch_size || 1,
        outputs: [],
        status: submitted.status,
        raw_status: submitted.status,
        created_at: new Date().toISOString(),
        quoted_microtickets: submitted.quoted_microtickets,
        quoted_tickets: submitted.quoted_tickets,
        tokens_per_ticket: submitted.tokens_per_ticket,
      });
    }
    return normalizePlaygroundGeneration(response.data);
  },

  getHistory: async (limit = 50, offset = 0) => {
    const response = await apiClient.get<PlaygroundGenerationResponse[]>(API_URL + "/playground/history", { params: { limit, offset } });
    return Promise.all(response.data.map(normalizePlaygroundGeneration));
  },

  getGeneration: async (id: string) => {
    const response = await apiClient.get<PlaygroundGenerationResponse>(API_URL + "/playground/history/" + id);
    return normalizePlaygroundGeneration(response.data);
  },

  getGenerationStatus: async (id: string) => {
    const response = await apiClient.get<{
      id: string;
      status: string;
      raw_status?: string;
      status_zh?: string;
      outputs: PlaygroundOutputResponse[];
      error_code?: string;
      error?: string;
      quoted_microtickets?: string;
      quoted_tickets?: string;
      tokens_per_ticket?: string;
      cancellation_requested?: boolean;
      support_review?: boolean;
      provider_name?: string | null;
      provider_task_id?: string | null;
      provider_request_id?: string | null;
    }>(API_URL + "/playground/history/" + id + "/status");
    return {
      ...response.data,
      raw_status: response.data.raw_status || response.data.status,
      status: normalizePlaygroundStatus(response.data.status),
    };
  },

  resumeGeneration: async (id: string) => {
    const response = await apiClient.post<PlaygroundGenerationResponse>(
      API_URL + "/playground/history/" + id + "/resume",
    );
    return normalizePlaygroundGeneration(response.data);
  },

  cancelGeneration: (id: string) => aiTaskApi.cancel(id),

  deleteGeneration: (id: string) =>
    apiClient.delete(API_URL + "/playground/history/" + id).then(r => r.data),

  saveToLibrary: (generationId: string, outputId: string, category?: string) =>
    apiClient.post(API_URL + "/playground/history/" + generationId + "/outputs/" + outputId + "/save-to-library", { category: category || "general" }).then(r => r.data),

  getTemplates: () =>
    apiClient.get<PlaygroundTemplateResponse[]>(API_URL + "/playground/templates").then(r => r.data),

  createTemplate: (data: { name: string; category?: string; prompt: string; negative_prompt?: string; default_mode?: string; default_model_id?: string; default_parameters?: Record<string, unknown> }) =>
    apiClient.post<PlaygroundTemplateResponse>(API_URL + "/playground/templates", withoutCloudModelOverrides(data)).then(r => r.data),

  updateTemplate: (id: string, version: number, data: Partial<{ name: string; category: string; prompt: string; negative_prompt: string; default_mode: string; default_model_id: string; default_parameters: Record<string, unknown> }>) =>
    apiClient.put<PlaygroundTemplateResponse>(API_URL + "/playground/templates/" + id, withoutCloudModelOverrides(data), { headers: { "If-Match": String(version) } }).then(r => r.data),

  deleteTemplate: (id: string, version: number) =>
    apiClient.delete(API_URL + "/playground/templates/" + id, { headers: { "If-Match": String(version) } }).then(r => r.data),

  uploadMedia: async (file: File) => {
    const formData = new FormData();
    formData.append("file", file);
    const response = await apiClient.post<MediaUploadResponse | { path: string }>(API_URL + "/playground/upload", formData, {
      headers: { "Content-Type": "multipart/form-data" },
    });
    if ("id" in response.data) {
      const mediaReference = toMediaReference(response.data.id);
      return {
        ...response.data,
        media_reference: mediaReference,
        media_url: await resolveMediaUrl(mediaReference),
      };
    }
    return {
      ...response.data,
      media_reference: response.data.path,
    };
  },

};
