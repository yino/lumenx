export type SettingsCategory =
  | "account"
  | "general"
  | "models"
  | "prompts"
  | "apikeys"
  | "storage"
  | "about";

const DESKTOP_SETTINGS_CATEGORIES: SettingsCategory[] = [
  "general",
  "models",
  "prompts",
  "apikeys",
  "storage",
  "about",
];

const CLOUD_SETTINGS_CATEGORIES: SettingsCategory[] = ["account", "general", "prompts"];

export function visibleSettingsCategories(isCloudDeployment: boolean): SettingsCategory[] {
  return [
    ...(isCloudDeployment ? CLOUD_SETTINGS_CATEGORIES : DESKTOP_SETTINGS_CATEGORIES),
  ];
}

export function normalizeSettingsCategory(
  category: SettingsCategory,
  isCloudDeployment: boolean,
): SettingsCategory {
  const visible = visibleSettingsCategories(isCloudDeployment);
  return visible.includes(category) ? category : visible[0];
}
