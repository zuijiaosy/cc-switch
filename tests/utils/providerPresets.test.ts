import { describe, expect, it } from "vitest";

import { providerPresets } from "@/config/claudeProviderPresets";
import { codexProviderPresets } from "@/config/codexProviderPresets";
import { geminiProviderPresets } from "@/config/geminiProviderPresets";
import { opencodeProviderPresets } from "@/config/opencodeProviderPresets";

describe("默认预设供应商", () => {
  it("Claude Code(cc) 新增时包含 ai-hub-api / https://api.xbai.top", () => {
    const preset = providerPresets.find((p) => p.name === "ai-hub-api");
    expect(preset).toBeTruthy();
    expect((preset as any).settingsConfig?.env?.ANTHROPIC_BASE_URL).toBe(
      "https://api.xbai.top",
    );
  });

  it("Codex 新增时包含 ai-hub-api / https://api.xbai.top/v1", () => {
    const preset = codexProviderPresets.find((p) => p.name === "ai-hub-api");
    expect(preset).toBeTruthy();
    expect((preset as any).config).toContain(
      'base_url = "https://api.xbai.top/v1"',
    );
  });

  it("Gemini 新增时包含 ai-hub-api / https://api.xbai.top", () => {
    const preset = geminiProviderPresets.find((p) => p.name === "ai-hub-api");
    expect(preset).toBeTruthy();
    expect((preset as any).baseURL).toBe("https://api.xbai.top");
    expect((preset as any).settingsConfig?.env?.GOOGLE_GEMINI_BASE_URL).toBe(
      "https://api.xbai.top",
    );
  });

  it("OpenCode 新增时包含 ai-hub-api / https://api.xbai.top/v1", () => {
    const preset = opencodeProviderPresets.find((p) => p.name === "ai-hub-api");
    expect(preset).toBeTruthy();
    expect((preset as any).settingsConfig?.options?.baseURL).toBe(
      "https://api.xbai.top/v1",
    );
  });
});
