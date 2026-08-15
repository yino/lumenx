export const ADMIN_SECTIONS = [
  "users",
  "invitations",
  "tasks",
  "usage",
  "tickets",
  "configuration",
  "audit-events",
  "import-batches",
] as const;

export type AdminSection = (typeof ADMIN_SECTIONS)[number];

export function parseAdminSection(hash: string): AdminSection | null {
  const match = hash.match(/^#\/admin(?:\/([^/]+))?$/);
  if (!match) return null;
  const requested = match[1] || "users";
  return ADMIN_SECTIONS.includes(requested as AdminSection)
    ? (requested as AdminSection)
    : "users";
}

export function canAccessAdminRoute(isPlatformAdmin: boolean): boolean {
  return isPlatformAdmin;
}
