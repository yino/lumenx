export type VideoGenerationStatus = "pending" | "processing" | "completed" | "failed";

export function hasActiveVideoGeneration(
    status: VideoGenerationStatus | undefined,
    inFlightCount = 0,
): boolean {
    return status === "pending" || status === "processing" || inFlightCount > 0;
}
