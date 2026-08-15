import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { getMessages } from "@/lib/i18n";

const readFrontendSource = (path: string) =>
  readFileSync(resolve(process.cwd(), path), "utf8");

describe("中文可见文案", () => {
  it("关键创作流程使用中文产品术语", () => {
    const messages = getMessages("zh");

    expect(messages.project.styleSettingsSub).toBe("项目风格设置");
    expect(messages.pipeline.stepIndex).toBe("步骤 0{number}");
    expect(messages.pipeline.beta).toBe("测试版");
    expect(messages.library.variantsSection).toBe("变体");
    expect(messages.library.metadataSection).toBe("元数据");
    expect(messages.library.promptSection).toBe("生成提示词");
    expect(messages.storyboardR2V.polishCnLabel).toBe("中文版");
    expect(messages.storyboardR2V.polishEnLabel).toBe("英文版");
    expect(messages.storyboardR2V.queueJumpToShot).toBe("跳转到该镜头");
  });

  it("关键组件不再包含已清理的英文界面文案", () => {
    const sources = [
      "src/components/library/AssetLibraryPage.tsx",
      "src/components/modules/PromptBuilder.tsx",
      "src/components/modules/StoryboardComposer.tsx",
      "src/components/modules/VideoCreator.tsx",
      "src/components/modules/storyboard-r2v/ShotCard.tsx",
      "src/components/modules/storyboard-r2v/shot-panel/CandidateThumb.tsx",
      "src/components/modules/storyboard-r2v/shot-panel/T2ISubsection.tsx",
    ].map(readFrontendSource).join("\n");

    for (const productCopyLiteral of [
      '"ASSET LIBRARY',
      '"Basic Movement',
      '"Pan Left',
      '"No Image"',
      '"Select Frame"',
      '"Queued"',
      '"Generating"',
      '"Star candidate"',
      '"Unstar candidate"',
      '"Project Style Settings"',
    ]) {
      expect(sources).not.toContain(productCopyLiteral);
    }
  });
});
