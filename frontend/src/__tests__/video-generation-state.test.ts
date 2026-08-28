import { describe, expect, it } from "vitest";

import { hasActiveVideoGeneration } from "@/components/modules/storyboard-r2v/videoGenerationState";

describe("storyboard video generation state", () => {
    it.each(["pending", "processing"] as const)(
        "treats local %s state as active before task polling catches up",
        (status) => {
            expect(hasActiveVideoGeneration(status, 0)).toBe(true);
        },
    );

    it("stays active while any persisted task is still in flight", () => {
        expect(hasActiveVideoGeneration("completed", 1)).toBe(true);
        expect(hasActiveVideoGeneration("failed", 2)).toBe(true);
    });

    it("unlocks only after local and persisted state are terminal", () => {
        expect(hasActiveVideoGeneration("completed", 0)).toBe(false);
        expect(hasActiveVideoGeneration("failed", 0)).toBe(false);
        expect(hasActiveVideoGeneration(undefined, 0)).toBe(false);
    });
});
