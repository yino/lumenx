export interface AssetTagReference {
    slot: number;
    name: string;
    tag: string;
}

export interface AssetTagTextSegment {
    text: string;
    isTag: boolean;
}

export interface CanonicalAssetReference {
    alias: string;
    type: "image" | "video" | "audio" | "text";
    purpose: string;
    media_id?: string;
}

// R2V sends reference images by positional characterN slots. The name may
// refer to a character, scene, or prop, so all three UI entry points share
// the same canonical tag shape.
const ASSET_TAG_PATTERN = /\[(character(\d+)?|scene|prop):([^\]]+)\]/gi;

/** Split prompt text into plain and asset-tag segments for editor highlighting. */
export function splitAssetTagText(prompt: string): AssetTagTextSegment[] {
    const segments: AssetTagTextSegment[] = [];
    const pattern = new RegExp(ASSET_TAG_PATTERN.source, "gi");
    let lastIndex = 0;
    let match: RegExpExecArray | null;

    while ((match = pattern.exec(prompt)) !== null) {
        if (match.index > lastIndex) {
            segments.push({ text: prompt.slice(lastIndex, match.index), isTag: false });
        }
        segments.push({ text: match[0], isTag: true });
        lastIndex = match.index + match[0].length;
    }

    if (lastIndex < prompt.length) {
        segments.push({ text: prompt.slice(lastIndex), isTag: false });
    }
    return segments;
}

function nextSlot(usedSlots: Set<number>): number {
    if (usedSlots.size === 0) return 1;
    return Math.max(...Array.from(usedSlots)) + 1;
}

/** Read legacy and canonical asset tags while preserving their slot order. */
export function extractAssetTags(prompt: string): AssetTagReference[] {
    const references: AssetTagReference[] = [];
    const usedSlots = new Set<number>();
    const slotByName = new Map<string, number>();
    let match: RegExpExecArray | null;

    const pattern = new RegExp(ASSET_TAG_PATTERN.source, "gi");
    while ((match = pattern.exec(prompt)) !== null) {
        const kind = match[1].toLowerCase();
        const name = match[3].trim();
        if (!name) continue;

        const explicitSlot = kind.startsWith("character") && match[2]
            ? Number.parseInt(match[2], 10)
            : undefined;
        const slot = explicitSlot && explicitSlot > 0
            ? explicitSlot
            : slotByName.get(name) ?? nextSlot(usedSlots);
        const key = `${slot}:${name}`;
        if (references.some((reference) => `${reference.slot}:${reference.name}` === key)) {
            continue;
        }

        usedSlots.add(slot);
        slotByName.set(name, slot);
        references.push({
            slot,
            name,
            tag: `[character${slot}:${name}]`,
        });
    }

    return references.sort((a, b) => a.slot - b.slot);
}

/** Return the canonical tag to insert for an asset name. */
export function assetTagForName(prompt: string, name: string): string {
    const normalizedName = name.trim();
    if (!normalizedName) return "";
    const existing = extractAssetTags(prompt).find(
        (reference) => reference.name === normalizedName,
    );
    const usedSlots = new Set(extractAssetTags(prompt).map((reference) => reference.slot));
    const slot = existing?.slot ?? nextSlot(usedSlots);
    return `[character${slot}:${normalizedName}]`;
}

/** Build the stable reference consumed by the generic short-drama Agent. */
export function canonicalAssetReference(
    kind: "character" | "scene" | "prop" | "image" | "video" | "audio" | "text",
    slot: number | string,
    name: string,
    mediaId?: string,
): CanonicalAssetReference {
    const normalizedKind = kind === "character" || kind === "scene" || kind === "prop" ? "image" : kind;
    return {
        alias: (kind === "character" || kind === "scene" || kind === "prop")
            ? `${kind}${slot}:${name.trim()}`
            : `${kind}:${slot}`,
        type: normalizedKind,
        purpose: kind === "character" ? "角色外观" : kind === "scene" ? "场景外观" : kind === "prop" ? "道具外观" : "素材参考",
        ...(mediaId ? { media_id: mediaId } : {}),
    };
}

/** Keep reference tags when a generated polish result replaces the prompt. */
export function preserveAssetTags(originalPrompt: string, nextPrompt: string): string {
    const originalTags = extractAssetTags(originalPrompt);
    if (originalTags.length === 0) return nextPrompt.trim();

    const result = nextPrompt.trim();
    const resultTags = extractAssetTags(result);
    const resultKeys = new Set(resultTags.map((reference) => `${reference.slot}:${reference.name}`));
    const resultNames = new Set(resultTags.map((reference) => reference.name));
    const missing = originalTags.filter((reference) =>
        !resultKeys.has(`${reference.slot}:${reference.name}`) &&
        !resultNames.has(reference.name),
    );

    if (missing.length === 0) return result;
    return [result, ...missing.map((reference) => reference.tag)].filter(Boolean).join(" ");
}
