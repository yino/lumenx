export const REVIEWED_PROVIDER_BRANDS = [
  "Aliyun",
  "DashScope",
  "Kling",
  "LumenX",
  "MuleRouter",
  "PixVerse",
  "Qwen",
  "Seedance",
  "Vidu",
  "Wan",
] as const;

export const REVIEWED_TECHNICAL_TERMS = [
  "AI",
  "API",
  "FPS",
  "HTTP",
  "HTTPS",
  "ID",
  "I2I",
  "I2V",
  "JSON",
  "OSS",
  "R2V",
  "T2I",
  "T2V",
  "TTS",
  "URL",
  "UUID",
  "V2V",
] as const;

export const REVIEWED_FILE_FORMATS = [
  "AVI",
  "CSV",
  "GIF",
  "JPEG",
  "JPG",
  "JSON",
  "M4A",
  "MKV",
  "MOV",
  "MP3",
  "MP4",
  "PNG",
  "SRT",
  "TSV",
  "WAV",
  "WEBM",
  "WEBP",
  "ZIP",
] as const;

export type VisibleCopySource =
  | "文本"
  | "替代文本"
  | "无障碍标签"
  | "标题"
  | "占位符";

export interface VisibleCopyViolation {
  source: VisibleCopySource;
  text: string;
  english: string[];
}

export interface VisibleCopyAuditOptions {
  /** Exact values originating from users, such as project and workspace names. */
  userAuthoredValues?: Iterable<string>;
  /** Exact canonical model IDs or other reviewed values supplied by a fixture. */
  technicalValues?: Iterable<string>;
}

const COPY_ATTRIBUTES: ReadonlyArray<{
  name: "alt" | "aria-label" | "title" | "placeholder";
  source: Exclude<VisibleCopySource, "文本">;
}> = [
  { name: "alt", source: "替代文本" },
  { name: "aria-label", source: "无障碍标签" },
  { name: "title", source: "标题" },
  { name: "placeholder", source: "占位符" },
];

const LATIN_SEQUENCE = /[A-Za-z][A-Za-z0-9]*(?:[ '\u2019-]+[A-Za-z][A-Za-z0-9]*)*/g;

const STRICT_TECHNICAL_PATTERNS = [
  /\b(?:\d{3,4}[xX\u00d7]\d{3,4}|(?:480|720|1080|1440|2160)[pP])\b/g,
  /\b[a-zA-Z0-9]+(?:[-_.:/][a-zA-Z0-9]+)+\b/g,
  /\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b/g,
  /\b[a-zA-Z0-9._-]+\.(?:avi|csv|gif|jpe?g|json|m4a|mkv|mov|mp3|mp4|png|srt|tsv|wav|webm|webp|zip)\b/gi,
  /\b(?:https?:\/\/|media:)[^\s]+/gi,
] as const;

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function maskExactValues(text: string, values: Iterable<string> | undefined): string {
  let masked = text;
  for (const value of Array.from(values ?? [])) {
    const normalized = value.trim();
    if (!normalized) continue;
    masked = masked.replace(new RegExp(escapeRegExp(normalized), "g"), " ");
  }
  return masked;
}

function maskReviewedTerms(text: string): string {
  const terms = [
    ...REVIEWED_PROVIDER_BRANDS,
    ...REVIEWED_TECHNICAL_TERMS,
    ...REVIEWED_FILE_FORMATS,
  ].sort((left, right) => right.length - left.length);
  let masked = text;
  for (const term of terms) {
    masked = masked.replace(new RegExp(`\\b${escapeRegExp(term)}\\b`, "gi"), " ");
  }
  return masked;
}

export function findUnapprovedEnglish(
  value: string,
  options: VisibleCopyAuditOptions = {},
): string[] {
  let candidate = maskExactValues(value, options.userAuthoredValues);
  for (const pattern of STRICT_TECHNICAL_PATTERNS) {
    candidate = candidate.replace(pattern, " ");
  }
  candidate = maskReviewedTerms(candidate);
  candidate = maskExactValues(candidate, options.technicalValues);
  return Array.from(new Set(candidate.match(LATIN_SEQUENCE) ?? []));
}

function isIgnoredTextNode(node: Node): boolean {
  const parent = node.parentElement;
  if (!parent) return true;
  return Boolean(parent.closest("script, style, svg, [aria-hidden='true'], [data-visible-copy-source='user']"));
}

export function auditRenderedChineseCopy(
  root: ParentNode,
  options: VisibleCopyAuditOptions = {},
): VisibleCopyViolation[] {
  const violations: VisibleCopyViolation[] = [];
  const ownerDocument = root instanceof Document ? root : root.ownerDocument;
  if (!ownerDocument) return violations;

  const walker = ownerDocument.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let current = walker.nextNode();
  while (current) {
    const text = current.textContent?.replace(/\s+/g, " ").trim() ?? "";
    if (text && !isIgnoredTextNode(current)) {
      const english = findUnapprovedEnglish(text, options);
      if (english.length) violations.push({ source: "文本", text, english });
    }
    current = walker.nextNode();
  }

  const elements = root instanceof Element
    ? [root, ...Array.from(root.querySelectorAll("*"))]
    : Array.from(root.querySelectorAll("*"));
  for (const element of elements) {
    if (element.closest("[data-visible-copy-source='user']")) continue;
    for (const attribute of COPY_ATTRIBUTES) {
      const text = element.getAttribute(attribute.name)?.replace(/\s+/g, " ").trim() ?? "";
      if (!text) continue;
      const english = findUnapprovedEnglish(text, options);
      if (english.length) violations.push({ source: attribute.source, text, english });
    }
  }

  return violations;
}

export function formatVisibleCopyViolations(violations: VisibleCopyViolation[]): string {
  return violations
    .map(({ source, text, english }) => `${source}: ${JSON.stringify(text)} -> ${english.join(", ")}`)
    .join("\n");
}
