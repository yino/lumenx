export type DeploymentMode = "desktop" | "cloud";

export const DEPLOYMENT_MODE: DeploymentMode =
  process.env.NEXT_PUBLIC_DEPLOYMENT_MODE === "cloud" ? "cloud" : "desktop";

export const IS_CLOUD_DEPLOYMENT = DEPLOYMENT_MODE === "cloud";

const CLOUD_MODEL_OVERRIDE_FIELDS = new Set([
  "base_url",
  "endpoint",
  "endpoint_url",
  "model",
  "model_id",
  "model_name",
  "model_settings",
  "polish_model",
  "provider",
  "provider_model_id",
  "price",
  "pricing",
  "secret_ref",
  "target_model",
  "default_model_id",
]);

const CLOUD_CREDENTIAL_FIELDS = new Set([
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
  "secret",
  "secret_key",
]);

function canonicalFieldName(name: string): string {
  return name
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .replace(/[^a-zA-Z0-9]+/g, "_")
    .toLowerCase();
}

function isCloudControlledField(name: string): boolean {
  const canonical = canonicalFieldName(name);
  return (
    CLOUD_MODEL_OVERRIDE_FIELDS.has(canonical) ||
    CLOUD_CREDENTIAL_FIELDS.has(canonical) ||
    canonical.endsWith("_api_key") ||
    canonical.endsWith("_secret_key") ||
    canonical.endsWith("_access_token")
  );
}

/** Keep desktop payloads intact while ensuring cloud requests cannot carry
 * browser-selected routing fields, including fields nested in parameters. */
export function withoutCloudModelOverrides<T>(payload: T): T {
  if (!IS_CLOUD_DEPLOYMENT) return payload;

  const strip = (value: unknown): unknown => {
    if (Array.isArray(value)) return value.map(strip);
    if (value && typeof value === "object") {
      return Object.fromEntries(
        Object.entries(value as Record<string, unknown>)
          .filter(([key]) => !isCloudControlledField(key))
          .map(([key, item]) => [key, strip(item)]),
      );
    }
    return value;
  };

  return strip(payload) as T;
}
