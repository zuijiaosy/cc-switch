/**
 * OpenCode 预设供应商配置模板
 * OpenCode 使用 AI SDK npm 包，配置结构与其他应用不同
 */
import type { ProviderCategory, OpenCodeProviderConfig } from "../types";
import type { PresetTheme, TemplateValueConfig } from "./claudeProviderPresets";

export interface OpenCodeProviderPreset {
  name: string;
  websiteUrl: string;
  apiKeyUrl?: string;
  /** OpenCode settings_config 结构 */
  settingsConfig: OpenCodeProviderConfig;
  isOfficial?: boolean;
  isPartner?: boolean;
  partnerPromotionKey?: string;
  category?: ProviderCategory;
  /** 模板变量定义 */
  templateValues?: Record<string, TemplateValueConfig>;
  /** 视觉主题配置 */
  theme?: PresetTheme;
  /** 图标名称 */
  icon?: string;
  /** 图标颜色 */
  iconColor?: string;
  /** 标记为自定义模板（用于 UI 区分） */
  isCustomTemplate?: boolean;
}

/**
 * OpenCode 模型预设变体映射
 * 按 npm 包名索引，为各模型提供预设 variants/options/限制信息
 */
export interface OpenCodePresetModel {
  id: string;
  variants?: Record<string, unknown>;
  options?: Record<string, unknown>;
  contextLimit?: number;
  outputLimit?: number;
}

export const OPENCODE_PRESET_MODEL_VARIANTS: Record<
  string,
  OpenCodePresetModel[]
> = {};

/**
 * OpenCode npm 包选项（AI SDK 生态）
 */
export const opencodeNpmPackages = [
  { value: "@ai-sdk/openai", label: "OpenAI" },
  { value: "@ai-sdk/openai-compatible", label: "OpenAI Compatible" },
  { value: "@ai-sdk/anthropic", label: "Anthropic" },
  { value: "@ai-sdk/google", label: "Google (Gemini)" },
] as const;

/**
 * OpenCode 供应商预设列表
 * 按需求：仅保留 api.codexzh.com
 */
export const opencodeProviderPresets: OpenCodeProviderPreset[] = [
  {
    name: "ai-hub-api",
    websiteUrl: "https://xbai.top",
    settingsConfig: {
      npm: "@ai-sdk/openai-compatible",
      options: {
        baseURL: "https://api.xbai.top/v1",
        apiKey: "",
      },
      models: {},
    },
    category: "aggregator",
    templateValues: {
      apiKey: {
        label: "API Key",
        placeholder: "sk-...",
        editorValue: "",
      },
    },
    theme: {
      icon: "codex",
      backgroundColor: "#111827",
      textColor: "#FFFFFF",
    },
    icon: "openai",
    iconColor: "#00A67E",
  },
  {
    name: "CodexZh",
    websiteUrl: "https://codexzh.com",
    settingsConfig: {
      npm: "@ai-sdk/openai-compatible",
      options: {
        baseURL: "https://api.codexzh.com/v1",
        apiKey: "",
      },
      models: {},
    },
    isOfficial: true,
    category: "official",
    templateValues: {
      apiKey: {
        label: "API Key",
        placeholder: "sk-...",
        editorValue: "",
      },
    },
    theme: {
      icon: "codex",
      backgroundColor: "#1F2937",
      textColor: "#FFFFFF",
    },
    icon: "openai",
    iconColor: "#00A67E",
  },
];
