import { describe, expect, it } from "vitest";
import { assetTagForName, extractAssetTags, preserveAssetTags, splitAssetTagText } from "@/lib/assetTags";

describe("asset tag helpers", () => {
  it("uses canonical character slots for characters, scenes, and props", () => {
    const prompt = "角色在办公室里拿起手机";

    expect(assetTagForName(prompt, "张成")).toBe("[character1:张成]");
    expect(assetTagForName("[character1:张成]", "办公室")).toBe("[character2:办公室]");
    expect(assetTagForName("[character1:张成] [character2:办公室]", "张成")).toBe("[character1:张成]");
  });

  it("reads legacy scene and prop tags as positional references", () => {
    expect(extractAssetTags("[character1:张成] [scene:办公室] [prop:手机]")).toEqual([
      { slot: 1, name: "张成", tag: "[character1:张成]" },
      { slot: 2, name: "办公室", tag: "[character2:办公室]" },
      { slot: 3, name: "手机", tag: "[character3:手机]" },
    ]);
  });

  it("keeps asset tags when applying a polished prompt", () => {
    expect(
      preserveAssetTags(
        "特写张成拿起手机 [character1:张成] [prop:手机]",
        "Close-up of a man picking up a phone.",
      ),
    ).toBe("Close-up of a man picking up a phone. [character1:张成] [character2:手机]");
  });

  it("does not duplicate tags already returned by the model", () => {
    expect(
      preserveAssetTags(
        "张成走进办公室 [character1:张成] [character2:办公室]",
        "张成走进办公室 [character1:张成] [character2:办公室]",
      ),
    ).toBe("张成走进办公室 [character1:张成] [character2:办公室]");
  });

  it("splits prompt text so the editor can highlight tags without changing the value", () => {
    expect(splitAssetTagText("特写 [character1:张成] 拿起手机 [prop:手机]")).toEqual([
      { text: "特写 ", isTag: false },
      { text: "[character1:张成]", isTag: true },
      { text: " 拿起手机 ", isTag: false },
      { text: "[prop:手机]", isTag: true },
    ]);
  });
});
