import { IS_CLOUD_DEPLOYMENT } from "./deployment";

const DEFAULT_CLOUD_STATIC_ASSET_BASE_URL =
  "https://yino-drama.oss-cn-beijing.aliyuncs.com";

function getCloudStaticAssetBaseUrl(): string {
  const configuredBaseUrl = process.env.NEXT_PUBLIC_STATIC_ASSET_BASE_URL?.trim();
  return (configuredBaseUrl || DEFAULT_CLOUD_STATIC_ASSET_BASE_URL).replace(/\/+$/, "");
}

/** Resolve bundled public assets through OSS in cloud builds while preserving
 * local paths for desktop builds and non-static media references. */
export function getStaticAssetUrl(path: string | null | undefined): string {
  if (!path) return "";
  if (/^(?:https?:|blob:|data:)/i.test(path)) return path;
  if (!IS_CLOUD_DEPLOYMENT || !path.startsWith("/assets/")) return path;
  return `${getCloudStaticAssetBaseUrl()}${path}`;
}
