// @vitest-environment happy-dom

import { describe, expect, it } from "vitest";

import {
  auditRenderedChineseCopy,
  findUnapprovedEnglish,
  REVIEWED_FILE_FORMATS,
  REVIEWED_PROVIDER_BRANDS,
  REVIEWED_TECHNICAL_TERMS,
} from "@/lib/visibleCopyPolicy";

describe("中文界面技术英文允许列表", () => {
  it("允许经过审查的品牌、缩写、格式、分辨率和标识", () => {
    expect(REVIEWED_PROVIDER_BRANDS).toContain("Wan");
    expect(REVIEWED_TECHNICAL_TERMS).toEqual(expect.arrayContaining(["R2V", "TTS", "API", "ID"]));
    expect(REVIEWED_FILE_FORMATS).toEqual(expect.arrayContaining(["PNG", "MP4", "WAV"]));
    expect(findUnapprovedEnglish(
      "Wan 2.7 · R2V · 1080p · 模型 wan2.7-r2v-turbo · 文件 final-cut.mp4 · API",
    )).toEqual([]);
  });

  it("只在显式声明后允许用户创作内容和无固定格式的技术值", () => {
    const value = "项目 English Story · 模型 qwenmax";
    expect(findUnapprovedEnglish(value)).toEqual(expect.arrayContaining(["English Story", "qwenmax"]));
    expect(findUnapprovedEnglish(value, {
      userAuthoredValues: ["English Story"],
      technicalValues: ["qwenmax"],
    })).toEqual([]);
  });

  it("拒绝不在允许列表中的英文产品命令和状态", () => {
    expect(findUnapprovedEnglish("Save project")).toEqual(["Save project"]);
    expect(findUnapprovedEnglish("Queued")).toEqual(["Queued"]);
    expect(findUnapprovedEnglish("Playground")).toEqual(["Playground"]);
  });

  it("只审计渲染文案和可访问属性，并允许标记用户内容", () => {
    const root = document.createElement("main");
    root.innerHTML = `
      <button aria-label="Save project">保存</button>
      <input placeholder="输入 API 地址" />
      <p data-visible-copy-source="user">English Story</p>
      <script>const prompt = "internal English prompt";</script>
    `;

    expect(auditRenderedChineseCopy(root)).toEqual([
      { source: "无障碍标签", text: "Save project", english: ["Save project"] },
    ]);
  });
});
