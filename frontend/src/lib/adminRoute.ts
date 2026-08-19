export const ADMIN_SECTIONS = [
  "dashboard",
  "users",
  "invitations",
  "recharge-orders",
  "system-scenes",
  "resources",
  "exceptions",
  "tasks",
  "usage",
  "tickets",
  "configuration",
  "audit-events",
  "import-batches",
] as const;

export type AdminSection = (typeof ADMIN_SECTIONS)[number];

export interface AdminRoute {
  section: AdminSection;
  resourceId: string | null;
  queryString: string;
}

const LEGACY_SECTION_ALIASES: Record<string, AdminSection> = {
  finance: "recharge-orders",
  scenes: "system-scenes",
  assets: "resources",
  operations: "exceptions",
};

export function parseAdminRoute(hash: string): AdminRoute | null {
  const [path, queryString = ""] = hash.split("?", 2);
  const match = path.match(/^#\/admin(?:\/([^/]+))?(?:\/([^/]+))?$/);
  if (!match) return null;
  const requested = match[1] || "dashboard";
  const section = ADMIN_SECTIONS.includes(requested as AdminSection)
    ? (requested as AdminSection)
    : LEGACY_SECTION_ALIASES[requested] || "dashboard";
  const resourceId = /^\d+$/.test(match[2] || "") ? match[2] : null;
  return { section, resourceId, queryString };
}

export function parseAdminSection(hash: string): AdminSection | null {
  return parseAdminRoute(hash)?.section || null;
}

export type AdminFilterValue = string | number | boolean | null | undefined;

export function buildAdminHash(
  section: AdminSection,
  filters: Record<string, AdminFilterValue> = {},
  resourceId?: string | null,
): string {
  const params = new URLSearchParams();
  Object.entries(filters)
    .sort(([left], [right]) => left.localeCompare(right))
    .forEach(([key, value]) => {
      if (value === undefined || value === null || value === "") return;
      params.set(key, String(value));
    });
  const suffix = params.toString();
  return `#/admin/${section}${resourceId ? `/${resourceId}` : ""}${suffix ? `?${suffix}` : ""}`;
}

export function replaceAdminFilters(
  section: AdminSection,
  filters: Record<string, AdminFilterValue>,
  resourceId?: string | null,
): void {
  if (typeof window === "undefined") return;
  window.history.replaceState(null, "", buildAdminHash(section, filters, resourceId));
}

export function adminFilter(queryString: string, key: string): string {
  return new URLSearchParams(queryString).get(key) || "";
}
