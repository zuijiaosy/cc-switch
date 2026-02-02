import type { ProviderCategory } from "@/types";

/**
 * Gemini 预设供应商的视觉主题配置
 */
export interface GeminiPresetTheme {
  /** 图标类型：'gemini' | 'generic' */
  icon?: "gemini" | "generic";
  /** 背景色（选中状态），支持 hex 颜色 */
  backgroundColor?: string;
  /** 文字色（选中状态），支持 hex 颜色 */
  textColor?: string;
}

export interface GeminiProviderPreset {
  name: string;
  websiteUrl: string;
  apiKeyUrl?: string;
  settingsConfig: object;
  baseURL?: string;
  model?: string;
  description?: string;
  category?: ProviderCategory;
  isPartner?: boolean;
  partnerPromotionKey?: string;
  endpointCandidates?: string[];
  theme?: GeminiPresetTheme;
  // 图标配置
  icon?: string; // 图标名称
  iconColor?: string; // 图标颜色
}

export const geminiProviderPresets: GeminiProviderPreset[] = [
  {
    name: "ai-hub-api",
    websiteUrl: "https://xbai.top",
    settingsConfig: {
      env: {
        GOOGLE_GEMINI_BASE_URL: "https://api.xbai.top",
        GEMINI_MODEL: "gemini-3-pro",
      },
    },
    baseURL: "https://api.xbai.top",
    model: "gemini-3-pro",
    description: "ai-hub-api Gemini API",
    category: "aggregator",
    endpointCandidates: ["https://api.xbai.top"],
    theme: {
      icon: "gemini",
      backgroundColor: "#111827",
      textColor: "#FFFFFF",
    },
    icon: "gemini",
    iconColor: "#4285F4",
  },
  {
    name: "XBai",
    websiteUrl: "https://xbai.top",
    settingsConfig: {
      env: {
        GOOGLE_GEMINI_BASE_URL: "https://api.xbai.top",
        GEMINI_MODEL: "gemini-3-pro",
      },
    },
    baseURL: "https://api.xbai.top",
    model: "gemini-3-pro",
    description: "XBai Gemini API",
    category: "official",
    endpointCandidates: ["https://api.xbai.top"],
    theme: {
      icon: "gemini",
      backgroundColor: "#4285F4",
      textColor: "#FFFFFF",
    },
    icon: "gemini",
    iconColor: "#4285F4",
  },
];

export function getGeminiPresetByName(
  name: string,
): GeminiProviderPreset | undefined {
  return geminiProviderPresets.find((preset) => preset.name === name);
}

export function getGeminiPresetByUrl(
  url: string,
): GeminiProviderPreset | undefined {
  if (!url) return undefined;
  return geminiProviderPresets.find(
    (preset) =>
      preset.baseURL &&
      url.toLowerCase().includes(preset.baseURL.toLowerCase()),
  );
}
